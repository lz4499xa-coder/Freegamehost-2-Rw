import os
import re
import sys
import time
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PANEL_URL = os.getenv("FGH_PANEL_URL", "https://panel.freegamehost.xyz").rstrip("/")
USERNAME = os.getenv("FGH_USERNAME", "").strip()
PASSWORD = os.getenv("FGH_PASSWORD", "").strip()
# 可选：指定服务器 URL 或 ID（留空则自动进入首页第一个服务器，支持多个逗号分隔）
SERVER_IDS = [s.strip() for s in os.getenv("FGH_SERVER_IDS", "").split(",") if s.strip()]
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


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

    # 兜底：移除常见 CMP 浮层防止拦截点击
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

    # 填写账号
    user_input = page.locator(
        'input[name="username"], input[name="user"], input[type="email"], input[placeholder*="example.com"]'
    ).first
    user_input.wait_for(state="visible", timeout=20000)
    user_input.fill(USERNAME)

    # 填写密码
    pass_input = page.locator('input[name="password"], input[type="password"]').first
    pass_input.fill(PASSWORD)

    # 点击登录
    submit_btn = page.locator('button[type="submit"], button:has-text("SIGN IN"), button:has-text("Sign In")').first
    submit_btn.click()

    # 等待跳转到仪表盘（URL 不再包含 /auth/login）
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
    """进入单个服务器页面并尝试续费，返回 (状态摘要, 截图路径)"""
    server_id = server_url.rstrip("/").split("/")[-1]
    print(f"[INFO] 正在检查服务器: {server_url}")
    page.goto(server_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    dismiss_cookie_banners(page)

    # 滚动到底部，确保 EXTEND SERVER 卡片渲染并进入视口
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)
    dismiss_cookie_banners(page)

    before_timer = extract_timer_text(page)
    print(f"[INFO] [{server_id}] 当前剩余时间: {before_timer}")

    # 定位 EXTEND SERVER 区域
    extend_card = page.locator("text=EXTEND SERVER").first
    if extend_card.count() > 0:
        try:
            extend_card.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    # 1. 检查是否处于冷却期 (RENEWAL COOLDOWN)
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

    # 2. 查找可点击的 Extend / Renew 按钮
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
                    # 排除“Upgrade / Premium”广告按钮
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

    # 等待续费请求完成
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

    launch_kwargs = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if PROXY_URL:
        launch_kwargs["proxy"] = {"server": PROXY_URL}
        print("[INFO] 已启用代理模式")

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
        # 隐藏 webdriver 特征
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
    # 仅在成功续费或出现警告时推送（若每次冷却都想收到，可去掉此判断）
    if any("✅" in r or "⚠️" in r for r in results) or os.getenv("TG_ALWAYS_NOTIFY") == "1":
        send_telegram(summary, last_shot)


if __name__ == "__main__":
    main()
