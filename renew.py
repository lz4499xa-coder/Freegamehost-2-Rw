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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PANEL_URL = os.getenv("FGH_PANEL_URL", "https://panel.freegamehost.xyz").rstrip("/")
USERNAME = os.getenv("FGH_USERNAME", "").strip()
PASSWORD = os.getenv("FGH_PASSWORD", "").strip()
SERVER_IDS = [s.strip() for s in os.getenv("FGH_SERVER_IDS", "").split(",") if s.strip()]
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

LOCAL_PROXY_PORT = 10808
SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def parse_hy2_to_singbox_config(hy2_url: str, local_port: int = LOCAL_PROXY_PORT) -> dict:
    """解析 hy2:// 或 hysteria2:// 链接并生成 sing-box 配置"""
    parsed = urlparse(hy2_url)
    host = parsed.hostname
    if not host:
        raise ValueError("hy2 链接缺少服务器地址")
    port = parsed.port or 443

    # Hysteria2 规范中 user:pass 整体作为密码
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


def start_hy2_bridge(hy2_url: str) -> tuple[str, subprocess.Popen, str]:
    """启动 sing-box 将 hy2 转为本地 HTTP/SOCKS5 代理"""
    singbox_bin = shutil.which("sing-box")
    if not singbox_bin:
        raise RuntimeError("未找到 sing-box，请先安装 sing-box 或检查 workflow 配置")

    config = parse_hy2_to_singbox_config(hy2_url, LOCAL_PROXY_PORT)
    fd, config_path = tempfile.mkstemp(prefix="singbox_hy2_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    proc = subprocess.Popen(
        [singbox_bin, "run", "-c", config_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # 等待本地端口就绪
    ready = False
    for _ in range(30):
        if proc.poll() is not None:
            stderr_out = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"sing-box 启动失败并退出:\n{stderr_out}")
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

    # 验证代理出口连通性
    try:
        resp = requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": local_proxy, "https": local_proxy},
            timeout=12,
        )
        print(f"[INFO] hy2 代理连通成功，当前出口 IP: {resp.json().get('ip')}")
    except Exception as e:
        print(f"[WARN] 出口 IP 检测失败（仍将继续尝试访问面板）: {e}")

    return local_proxy, proc, config_path


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


def dismiss_cookie_banners(page):
    """关闭或隐藏 Google CMP / Cookie 遮挡弹窗"""
    selectors = [
        "button:has-text('Consent')",
        "button:has-text('Accept')",
        "button:has-text('Agree')",
        "button:has-text('同意')",
        "button[aria-label='Close']",
        ".fc-cta-consent",
        ".fc-close",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=2000)
                time.sleep(0.5)
        except Exception:
            pass

    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('.fc-consent-root, [id*="sp_message"], [class*="cookie"], iframe[title*="Consent"]').forEach(el => {
                    el.style.display = 'none';
                });
            }"""
        )
    except Exception:
        pass


def login(page):
    """登录 Pterodactyl 面板"""
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

    try:
        page.wait_for_function(
            "() => !window.location.pathname.includes('/auth/login')",
            timeout=30000,
        )
    except PlaywrightTimeoutError:
        err_shot = os.path.join(SCREENSHOT_DIR, "login_failed.png")
        page.screenshot(path=err_shot, full_page=True)
        raise RuntimeError(f"登录超时或触发验证码/账密错误，当前 URL: {page.url}")

    page.wait_for_timeout(3000)
    print(f"[INFO] 登录成功，当前页面: {page.url}")


def discover_server_urls(page) -> list[str]:
    """获取需要续费的服务器页面 URL 列表"""
    if SERVER_IDS:
        return [
            sid if sid.startswith("http") else f"{PANEL_URL}/server/{sid}"
            for sid in SERVER_IDS
        ]

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
        page.locator("button:has-text('Extend')"),
        page.locator("button:has-text('EXTEND')"),
        page.locator("button:has-text('Renew')"),
        page.locator("button:has-text('RENEW')"),
        page.locator("button:has-text('Add Time')"),
    ]

    clicked = False
    for group in btn_candidates:
        count = group.count()
        for i in range(count):
            btn = group.nth(i)
            try:
                if btn.is_visible() and btn.is_enabled():
                    text = btn.inner_text().strip()
                    if re.search(r"upgrade|premium", text, re.I):
                        continue
                    print(f"[INFO] [{server_id}] 找到续费按钮: '{text}'，准备点击...")
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=10000)
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
    shot = os.path.join(SCREENSHOT_DIR, f"{server_id}_renewed.png")
    page.screenshot(path=shot, full_page=False)

    msg = f"✅ [{server_id}] 续费已触发！剩余时间: {before_timer} -> {after_timer}"
    print(f"[INFO] {msg}")
    return msg, shot


def main():
    if not USERNAME or not PASSWORD:
        print("[ERROR] 缺少环境变量 FGH_USERNAME 或 FGH_PASSWORD")
        sys.exit(1)

    singbox_proc = None
    singbox_cfg = None
    effective_proxy = PROXY_URL

    try:
        if PROXY_URL.lower().startswith(("hy2://", "hysteria2://")):
            effective_proxy, singbox_proc, singbox_cfg = start_hy2_bridge(PROXY_URL)

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
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()

            try:
                login(page)
                server_urls = discover_server_urls(page)
                for url in server_urls:
                    msg, shot = renew_server(page, url)
                    results.append(msg)
                    last_shot = shot
            except Exception as e:
                err_msg = f"❌ FreeGameHost 自动续费异常:\n{e}"
                print(f"[ERROR] {err_msg}")
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

        summary = "<b>FreeGameHost 自动保活报告</b>\n" + "\n".join(results)
        print("\n" + summary)
        if any("✅" in r or "⚠️" in r for r in results) or os.getenv("TG_ALWAYS_NOTIFY") == "1":
            send_telegram(summary, last_shot)

    finally:
        if singbox_proc and singbox_proc.poll() is None:
            singbox_proc.terminate()
            try:
                singbox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                singbox_proc.kill()
        if singbox_cfg and os.path.exists(singbox_cfg):
            try:
                os.remove(singbox_cfg)
            except OSError:
                pass


if __name__ == "__main__":
    main()
