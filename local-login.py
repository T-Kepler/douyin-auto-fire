r"""扫码登录并生成 storage-state.json。

与项目自带的 scripts/login.py 等价，但不需要在终端按 Enter。

关键点：等待扫码期间**绝不导航**。抖音的登录二维码挂在当前页面上，
一旦 goto 到别的页面，二维码就会重新刷新，导致根本扫不上。
因此这里改为轮询浏览器上下文的 sessionid cookie 来判断登录是否完成，
登录成功后才跳转到私信页做一次确认，然后保存登录状态。

用法：
    .\.venv\Scripts\python.exe local-login.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright  # noqa: E402

DOUYIN_URL = "https://www.douyin.com/"
CHAT_URL = "https://www.douyin.com/chat"
SEARCH_SELECTOR = 'input[placeholder*="搜索"]'
SESSION_COOKIES = {"sessionid", "sessionid_ss"}
POLL_INTERVAL_MS = 2_000
MAX_WAIT_SECONDS = 600

# 控制台可能是 GBK，避免打印中文/emoji 时抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(message: str) -> None:
    print(message, flush=True)


async def _open_login(page) -> None:
    """点开登录弹窗并切到扫码登录 Tab（失败也不影响，页面可能已自动弹出）。"""
    for label, timeout in (("登录", 8_000), ("扫码登录", 5_000)):
        locator = page.get_by_text(label, exact=True)
        try:
            if await locator.count():
                await locator.first.click(timeout=timeout)
        except Exception:
            pass


async def _session_cookie_present(context) -> bool:
    """登录成功的判据：上下文里出现了 sessionid cookie。

    注意：这里只读取 cookie，不做任何页面导航，避免刷新二维码。
    """
    try:
        cookies = await context.cookies()
    except Exception:
        return False
    return any(
        c.get("name") in SESSION_COOKIES and c.get("value") for c in cookies
    )


async def _verify_chat_access(page) -> bool:
    """登录后确认真的能进私信页（能找到好友搜索框）。"""
    for attempt in range(1, 4):
        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
            await page.locator(SEARCH_SELECTOR).first.wait_for(
                state="visible", timeout=20_000
            )
            log(f"  私信页确认成功（第 {attempt} 次尝试）")
            return True
        except Exception as exc:
            log(f"  私信页确认失败（第 {attempt}/3 次）：{type(exc).__name__}")
    return False


async def login() -> int:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()

        await page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        await _open_login(page)

        log("=" * 64)
        log("浏览器已打开，请用抖音 App 扫码登录。")
        log(f"等待期间不会跳转页面，二维码不会被刷新。最长等 {MAX_WAIT_SECONDS // 60} 分钟。")
        log("=" * 64)

        waited = 0
        ok = False
        while waited < MAX_WAIT_SECONDS:
            await page.wait_for_timeout(POLL_INTERVAL_MS)
            waited += POLL_INTERVAL_MS // 1000
            if await _session_cookie_present(context):
                ok = True
                log(f"检测到 sessionid，扫码成功（用时约 {waited}s）")
                break
            if waited % 20 == 0:
                log(f"  ...等待扫码 ({waited}s / {MAX_WAIT_SECONDS}s)")

        if not ok:
            await browser.close()
            log("登录超时：未检测到 sessionid。请重新运行本脚本。")
            return 1

        # 一旦拿到登录态就先落盘，避免后续确认失败还要重新扫码
        await context.storage_state(path="storage-state.json.tmp")
        Path("storage-state.json.tmp").replace("storage-state.json")
        log("登录状态已保存到 storage-state.json")

        if not await _verify_chat_access(page):
            await browser.close()
            log("已登录并保存，但未能进入私信页；请直接运行 Dry Run 复查。")
            return 2

        await browser.close()
        log("登录成功。")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(login()))
