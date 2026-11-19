import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
import speech_recognition as sr
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PANEL_URL = os.getenv("FGH_PANEL_URL", "https://panel.freegamehost.xyz").rstrip("/")
USERNAME = os.getenv("FGH_USERNAME", "").strip()
PASSWORD = os.getenv("FGH_PASSWORD", "").strip()
FGH_COOKIES = os.getenv("FGH_COOKIES", "").strip()
SERVER_IDS = [s.strip() for s in os.getenv("FGH_SERVER_IDS", "").split(",") if s.strip()]
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

LOCAL_PROXY_PORT = 10808
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
SCREENSHOT_DIR = "screenshots"
STATE_DIR = "state"
COOKIE_FILE = os.path.join(STATE_DIR, "cookies.json")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

# 持久拦截 Google CMP / Funding Choices 遮罩层
BLOCK_CMP_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
(() => {
    const injectStyle = () => {
        if (document.getElementById('fgh-anti-cmp-style')) return;
        const style = document.createElement('style');
        style.id = 'fgh-anti-cmp-style';
        style.textContent = `
            .fc-consent-root,
            .fc-dialog-overlay,
            .fc-dialog-container,
            .fc-monetization-dialog-container,
            [id*="sp_message_container"] {
                display: none !important;
                visibility: hidden !important;
                pointer-events: none !important;
                z-index: -9999 !important;
            }
            body, html {
                overflow: auto !important;
                pointer-events: auto !important;
            }
        `;
        (document.head || document.documentElement).appendChild(style);
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectStyle);
    } else {
        injectStyle();
    }
})();
"""


def parse_hy2_to_singbox_config(hy2_url: str, local_port: int = LOCAL_PROXY_PORT) -> dict:
    """解析 hy2:// 或 hysteria2:// 链接并生成 sing-box 配置"""
    parsed = urlparse(hy2_url)
    host = parsed.hostname
    if not host:
        raise ValueError("hy2 链接缺少服务器地址")
    port = parsed.port or 443

    if parsed.username and parsed.password:
        password = f"{unquote(parsed.username)}:{unquote(parsed.password)}"
    elif parsed.username:
        password = unquote(parsed.username)
    else:
        password = ""

    params = parse_qs(parsed.query)
    sni = params.get("sni", params.get("peer", [host]))[0]
    insecure = params.get("insecure", ["0"])[0].lower() in ("1", "true", "yes")
    obfs_type = params.get("obfs", [""])[0]
    obfs_password = params.get("obfs-password", [""])[0]

    outbound = {
        "type": "hysteria2",
        "tag": "hy2-out",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
        },
    }

    if obfs_type and obfs_type.lower() != "none":
        outbound["obfs"] = {
            "type": obfs_type,
            "password": obfs_password,
        }

    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": local_port,
            }
        ],
        "outbounds": [outbound],
    }


def start_hy2_bridge(hy2_url: str) -> tuple[str, subprocess.Popen, list[str]]:
    """启动 sing-box 将 hy2 转为本地 HTTP/SOCKS5 代理"""
    singbox_bin = shutil.which("sing-box")
    if not singbox_bin:
        raise RuntimeError("未找到 sing-box，请检查 workflow 配置")

    config = parse_hy2_to_singbox_config(hy2_url, LOCAL_PROXY_PORT)
    fd_cfg, config_path = tempfile.mkstemp(prefix="singbox_hy2_", suffix=".json")
    with os.fdopen(fd_cfg, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    fd_log, log_path = tempfile.mkstemp(prefix="singbox_hy2_", suffix=".log")
    os.close(fd_log)
    log_file = open(log_path, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [singbox_bin, "run", "-c", config_path],
        stdout=subprocess.DEVNULL,
        stderr=log_file,
    )
    log_file.close()

    ready = False
    for _ in range(30):
        if proc.poll() is not None:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                err_text = lf.read().strip()
            raise RuntimeError(f"sing-box 启动失败并退出:\n{err_text}")
        try:
            with socket.create_connection(("127.0.0.1", LOCAL_PROXY_PORT), timeout=0.5):
                ready = True
                break
        except OSError:
            time.sleep(0.3)

    if not ready:
        proc.terminate()
        raise RuntimeError(f"sing-box 本地端口 {LOCAL_PROXY_PORT} 启动超时")

    local_proxy = f"http://127.0.0.1:{LOCAL_PROXY_PORT}"
    print(f"[INFO] hy2 代理已桥接至 {local_proxy}")

    try:
        resp = requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": local_proxy, "https": local_proxy},
            timeout=12,
        )
        print(f"[INFO] hy2 代理连通成功，当前出口 IP: {resp.json().get('ip')}")
    except Exception as e:
        print(f"[WARN] 出口 IP 检测失败（仍将继续尝试）: {e}")

    return local_proxy, proc, [config_path, log_path]


def send_telegram(message: str, image_path: str | None = None):
    """发送 Telegram 通知（可选）"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        if image_path and os.path.exists(image_path):
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(image_path, "rb") as f:
                requests.post(
                    url,
                    data={"chat_id": TG_CHAT_ID, "caption": message[:1024], "parse_mode": "HTML"},
                    files={"photo": f},
                    timeout=30,
                )
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            requests.post(
                url,
                data={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
                timeout=30,
            )
    except Exception as e:
        print(f"[WARN] Telegram 推送失败: {e}")


def parse_cookies_input(raw: str) -> list[dict]:
    """兼容 JSON 数组（Cookie-Editor 导出）和普通 Cookie 请求头字符串"""
    raw = raw.strip()
    if not raw:
        return []
    host = urlparse(PANEL_URL).hostname
    cookies = []

    if raw.startswith("["):
        data = json.loads(raw)
        same_site_map = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
        }
        for item in data:
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            c = {
                "name": str(name),
                "value": str(value),
                "domain": item.get("domain") or host,
                "path": item.get("path") or "/",
                "secure": bool(item.get("secure", True)),
                "httpOnly": bool(item.get("httpOnly", False)),
            }
            raw_ss = str(item.get("sameSite") or "").lower()
            if raw_ss in same_site_map:
                c["sameSite"] = same_site_map[raw_ss]
            exp = item.get("expirationDate") or item.get("expires")
            if isinstance(exp, (int, float)) and exp > 0:
                c["expires"] = float(exp)
            cookies.append(c)
    else:
        for pair in raw.split(";"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            if k:
                cookies.append(
                    {
                        "name": k,
                        "value": v.strip(),
                        "domain": host,
                        "path": "/",
                        "secure": True,
                    }
                )
    return cookies


def load_cookies_to_context(context) -> bool:
    """优先加载缓存中的最新 Cookie，其次加载 Secret 中的 FGH_COOKIES"""
    cookies = []
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if cookies:
                print(f"[INFO] 已从缓存加载 {len(cookies)} 条 Cookie")
        except Exception as e:
            print(f"[WARN] 读取缓存 Cookie 失败: {e}")

    if not cookies and FGH_COOKIES:
        try:
            cookies = parse_cookies_input(FGH_COOKIES)
            if cookies:
                print(f"[INFO] 已从 FGH_COOKIES 加载 {len(cookies)} 条 Cookie")
        except Exception as e:
            print(f"[WARN] 解析 FGH_COOKIES 失败: {e}")

    if cookies:
        context.add_cookies(cookies)
        return True
    return False


def save_cookies_from_context(context):
    """保存刷新后的 Cookie 供下次复用"""
    try:
        cookies = context.cookies()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 已保存最新 {len(cookies)} 条 Cookie 至 {COOKIE_FILE}")
    except Exception as e:
        print(f"[WARN] 保存 Cookie 失败: {e}")


def dismiss_cookie_banners(page):
    """彻底移除 Google CMP / Cookie 遮挡弹窗及遮罩层"""
    try:
        page.evaluate(
            """() => {
                const selectors = [
                    '.fc-consent-root',
                    '.fc-dialog-overlay',
                    '.fc-dialog-container',
                    '.fc-monetization-dialog-container',
                    '[id*="sp_message"]',
                    'iframe[title*="Consent"]'
                ];
                document.querySelectorAll(selectors.join(',')).forEach(el => el.remove());
                if (document.body) {
                    document.body.style.overflow = 'auto';
                    document.body.style.pointerEvents = 'auto';
                }
            }"""
        )
    except Exception:
        pass


def solve_recaptcha_audio(page, proxy_url: str | None = None) -> bool:
    """尝试通过语音验证自动破解 reCAPTCHA v2 弹窗"""
    bframe_selector = 'iframe[src*="recaptcha/api2/bframe"], iframe[title*="recaptcha challenge"]'
    try:
        page.wait_for_selector(bframe_selector, state="visible", timeout=8000)
    except PlaywrightTimeoutError:
        return False

    if not shutil.which("ffmpeg"):
        print("[WARN] 未检测到 ffmpeg，无法执行语音验证")
        return False

    print("[INFO] 检测到 reCAPTCHA 挑战弹窗，尝试切换语音验证...")
    frame = page.frame_locator(bframe_selector).first

    mp3_path = os.path.join(tempfile.gettempdir(), "recaptcha_audio.mp3")
    wav_path = os.path.join(tempfile.gettempdir(), "recaptcha_audio.wav")

    try:
        audio_btn = frame.locator("#recaptcha-audio-button")
        audio_btn.wait_for(state="visible", timeout=8000)
        audio_btn.click()
        page.wait_for_timeout(2500)

        if frame.locator(".rc-doscaptcha-header-text").is_visible(timeout=1500):
            print("[WARN] 当前代理 IP 被 Google 拒绝语音验证（Try again later），建议配置 FGH_COOKIES 跳过登录")
            return False

        audio_src = frame.locator("audio#audio-source").get_attribute("src", timeout=10000)
        if not audio_src:
            print("[WARN] 未获取到语音验证音频链接")
            return False

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        r = requests.get(audio_src, headers={"User-Agent": USER_AGENT}, proxies=proxies, timeout=20)
        r.raise_for_status()
        with open(mp3_path, "wb") as f:
            f.write(r.content)

        subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-ar", "16000", "-ac", "1", wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        answer = recognizer.recognize_google(audio_data, language="en-US")
        print(f"[INFO] 语音识别结果: {answer}")

        frame.locator("#audio-response").fill(answer)
        page.wait_for_timeout(800)
        frame.locator("#recaptcha-verify-button").click()
        page.wait_for_timeout(3000)
        return True
    except Exception as e:
        print(f"[WARN] reCAPTCHA 语音识别失败: {e}")
        return False
    finally:
        for tmp_f in (mp3_path, wav_path):
            if os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except OSError:
                    pass


def ensure_logged_in(page, context, proxy_url: str | None = None):
    """优先验证 Cookie 是否已登录，失效则降级为账密 + 语音验证登录"""
    has_cookie = load_cookies_to_context(context)
    if has_cookie:
        print(f"[INFO] 尝试使用 Cookie 直接访问仪表盘: {PANEL_URL}")
        page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if "/auth/login" not in page.url:
            print(f"[INFO] Cookie 有效，已免验证码登录！当前页面: {page.url}")
            return
        print("[WARN] 缓存 Cookie 已过期，检查 FGH_COOKIES...")
        if os.path.exists(COOKIE_FILE) and FGH_COOKIES:
            context.clear_cookies()
            secret_cookies = parse_cookies_input(FGH_COOKIES)
            if secret_cookies:
                context.add_cookies(secret_cookies)
                page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                if "/auth/login" not in page.url:
                    print("[INFO] Secret 中的 FGH_COOKIES 有效，已免验证码登录！")
                    return
        print("[WARN] Cookie 均已失效，准备回退到账密登录...")

    if not USERNAME or not PASSWORD:
        raise RuntimeError("Cookie 不可用且未配置 FGH_USERNAME / FGH_PASSWORD")

    print(f"[INFO] 打开登录页: {PANEL_URL}/auth/login")
    page.goto(f"{PANEL_URL}/auth/login", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    dismiss_cookie_banners(page)

    user_input = page.locator(
        'input[name="username"], input[name="user"], input[type="email"], input[placeholder*="example.com"]'
    ).first
    user_input.wait_for(state="visible", timeout=20000)
    user_input.fill(USERNAME)

    pass_input = page.locator('input[name="password"], input[type="password"]').first
    pass_input.fill(PASSWORD)

    submit_btn = page.locator('button[type="submit"], button:has-text("SIGN IN"), button:has-text("Sign In")').first
    submit_btn.click()

    for attempt in range(2):
        wait_ms = 12000 if attempt == 0 else 20000
        try:
            page.wait_for_function(
                "() => !window.location.pathname.includes('/auth/login')",
                timeout=wait_ms,
            )
            page.wait_for_timeout(3000)
            print(f"[INFO] 登录成功，当前页面: {page.url}")
            return
        except PlaywrightTimeoutError:
            if attempt == 0 and solve_recaptcha_audio(page, proxy_url):
                continue
            break

    err_shot = os.path.join(SCREENSHOT_DIR, "login_failed.png")
    page.screenshot(path=err_shot, full_page=True)
    raise RuntimeError(
        f"登录被 reCAPTCHA 拦截或账密错误，当前 URL: {page.url}（建议配置 FGH_COOKIES 免验证码登录）"
    )


def discover_server_urls(page) -> list[str]:
    """获取需要续费的服务器页面 URL 列表"""
    if SERVER_IDS:
        return [
            sid if sid.startswith("http") else f"{PANEL_URL}/server/{sid}"
            for sid in SERVER_IDS
        ]

    if page.url.rstrip("/") != PANEL_URL:
        page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
    dismiss_cookie_banners(page)

    hrefs = page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a[href*="/server/"]'));
            return [...new Set(links.map(a => a.href))];
        }"""
    )
    if not hrefs:
        shot = os.path.join(SCREENSHOT_DIR, "no_servers.png")
        page.screenshot(path=shot, full_page=True)
        raise RuntimeError("仪表盘未检测到任何服务器链接，请检查账号或手动配置 FGH_SERVER_IDS")

    print(f"[INFO] 自动发现 {len(hrefs)} 台服务器: {hrefs}")
    return hrefs


def extract_timer_text(page) -> str:
    """提取剩余时间文本"""
    body_text = page.inner_text("body")
    m = re.search(r"TIME\s+REMAINING\s*[\r\n]+\s*([0-9]{1,2}\s*:\s*[0-9]{2}\s*:\s*[0-9]{2})", body_text, re.I)
    if m:
        return m.group(1).replace(" ", "")
    m2 = re.search(r"Time\s+left\s+([0-9]+h\s*[0-9]+m|[0-9]+m)", body_text, re.I)
    if m2:
        return m2.group(1)
    return "未知"


def renew_server(page, server_url: str) -> tuple[str, str]:
    """进入单个服务器页面并尝试续费"""
    server_id = server_url.rstrip("/").split("/")[-1]
    print(f"[INFO] 正在检查服务器: {server_url}")
    page.goto(server_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    dismiss_cookie_banners(page)

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)
    dismiss_cookie_banners(page)

    before_timer = extract_timer_text(page)
    print(f"[INFO] [{server_id}] 当前剩余时间: {before_timer}")

    extend_card = page.locator("text=EXTEND SERVER").first
    if extend_card.count() > 0:
        try:
            extend_card.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    cooldown_el = page.locator("text=/RENEWAL\\s*COOLDOWN/i").first
    if cooldown_el.count() > 0 and cooldown_el.is_visible():
        body_text = page.inner_text("body")
        cd_match = re.search(
            r"([0-9]{1,2}:[0-9]{2}:[0-9]{2})\s*[\r\n]*\s*RENEWAL\s*COOLDOWN",
            body_text,
            re.I,
        )
        cd_time = cd_match.group(1) if cd_match else "冷却中"
        shot = os.path.join(SCREENSHOT_DIR, f"{server_id}_cooldown.png")
        page.screenshot(path=shot, full_page=False)
        msg = f"⏳ [{server_id}] 处于续费冷却期 ({cd_time})，当前剩余: {before_timer}"
        print(f"[INFO] {msg}")
        return msg, shot

    btn_candidates = [
        page.locator("button:has-text('EXTEND FREE SERVER')"),
        page.locator("button[class*='RenewBtn']"),
        page.locator("button:has-text('Extend'), [role='button']:has-text('Extend')"),
        page.locator("button:has-text('Renew'), [role='button']:has-text('Renew')"),
    ]

    clicked = False
    for group in btn_candidates:
        count = group.count()
        for i in range(count):
            btn = group.nth(i)
            try:
                if btn.is_visible() and btn.is_enabled():
                    text = " ".join(btn.inner_text().split())
                    if re.search(r"upgrade|premium", text, re.I):
                        continue
                    print(f"[INFO] [{server_id}] 找到续费按钮: '{text}'，准备点击...")
                    dismiss_cookie_banners(page)
                    btn.scroll_into_view_if_needed()
                    try:
                        btn.click(timeout=6000)
                    except Exception as click_err:
                        print(f"[WARN] 常规点击被拦截，清理遮罩后强制点击: {click_err}")
                        dismiss_cookie_banners(page)
                        btn.click(force=True, timeout=6000)
                    clicked = True
                    break
            except Exception as e:
                print(f"[WARN] 按钮尝试点击异常: {e}")
        if clicked:
            break

    if not clicked:
        shot = os.path.join(SCREENSHOT_DIR, f"{server_id}_no_button.png")
        page.screenshot(path=shot, full_page=True)
        msg = f"⚠️ [{server_id}] 未找到可用的续费按钮（可能未到可续期时间或页面结构变化），当前剩余: {before_timer}"
        print(f"[WARN] {msg}")
        return msg, shot

    page.wait_for_timeout(5000)
    dismiss_cookie_banners(page)
    after_timer = extract_timer_text(page)
    in_cooldown = page.locator("text=/RENEWAL\\s*COOLDOWN/i").count() > 0
    shot = os.path.join(SCREENSHOT_DIR, f"{server_id}_renewed.png")
    page.screenshot(path=shot, full_page=False)

    status_note = "（已进入冷却期）" if in_cooldown else ""
    msg = f"✅ [{server_id}] 续费成功！剩余时间: {before_timer} -> {after_timer}{status_note}"
    print(f"[INFO] {msg}")
    return msg, shot


def main():
    if not FGH_COOKIES and (not USERNAME or not PASSWORD):
        print("[ERROR] 请至少配置 FGH_COOKIES 或 FGH_USERNAME + FGH_PASSWORD")
        sys.exit(1)

    singbox_proc = None
    temp_files: list[str] = []
    effective_proxy = PROXY_URL

    try:
        if PROXY_URL.lower().startswith(("hy2://", "hysteria2://")):
            effective_proxy, singbox_proc, temp_files = start_hy2_bridge(PROXY_URL)

        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        }
        if effective_proxy:
            launch_kwargs["proxy"] = {"server": effective_proxy}
            print("[INFO] 浏览器已启用代理")

        results = []
        last_shot = None

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=USER_AGENT,
            )
            context.add_init_script(BLOCK_CMP_INIT_SCRIPT)
            page = context.new_page()

            try:
                ensure_logged_in(page, context, effective_proxy or None)
                server_urls = discover_server_urls(page)
                for url in server_urls:
                    msg, shot = renew_server(page, url)
                    results.append(msg)
                    last_shot = shot
                save_cookies_from_context(context)
            except Exception as e:
                err_msg = f"❌ FreeGameHost 自动续费异常:\n{html.escape(str(e))}"
                print(f"[ERROR] {e}")
                fail_shot = os.path.join(SCREENSHOT_DIR, "error.png")
                try:
                    page.screenshot(path=fail_shot, full_page=True)
                    last_shot = fail_shot
                except Exception:
                    pass
                send_telegram(err_msg, last_shot)
                browser.close()
                sys.exit(1)

            browser.close()

        summary = "<b>FreeGameHost 自动保活报告</b>\n" + "\n".join(html.escape(r) for r in results)
        print("\n" + "\n".join(results))
        if any("✅" in r or "⚠️" in r for r in results) or os.getenv("TG_ALWAYS_NOTIFY") == "1":
            send_telegram(summary, last_shot)

    finally:
        if singbox_proc and singbox_proc.poll() is None:
            singbox_proc.terminate()
            try:
                singbox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                singbox_proc.kill()
        for fpath in temp_files:
            if fpath and os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
