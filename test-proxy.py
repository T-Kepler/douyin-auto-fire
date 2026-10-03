"""测试：本地 DoH 代理 + Chromium 走代理，能否真正打开抖音私信页。

会临时启动 doh_proxy.py，测试结束自动关闭。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright  # noqa: E402

PORT = 17890
CHAT_URL = "https://www.douyin.com/chat"
SEARCH_SELECTOR = 'input[placeholder*="搜索"]'

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def main() -> int:
    log_file = open("doh-proxy-test.log", "w", encoding="utf-8")  # noqa: SIM115
    proxy = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "doh_proxy.py"), "--port", str(PORT)],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(2.0)
        if proxy.poll() is not None:
            print("代理启动失败，日志：")
            log_file.flush()
            print(Path("doh-proxy-test.log").read_text(encoding="utf-8"))
            return 1
        print(f"代理已启动 (pid={proxy.pid})")

        failures: Counter[str] = Counter()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    f"--proxy-server=http://127.0.0.1:{PORT}",
                    "--proxy-bypass-list=<-loopback>",
                ],
            )
            context = await browser.new_context(
                storage_state="storage-state.json",
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()
            page.on(
                "requestfailed",
                lambda r: failures.update([f"{r.url.split('/')[2] if '://' in r.url else r.url} ({r.failure})"]),
            )

            try:
                await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:
                print(f"导航失败: {type(exc).__name__}: {str(exc).splitlines()[0]}")
                await browser.close()
                return 2

            found = False
            try:
                await page.locator(SEARCH_SELECTOR).first.wait_for(state="visible", timeout=25_000)
                found = True
            except Exception:
                pass

            print(f"页面标题: {await page.title()}")
            print(f"当前 URL: {page.url}")
            print(f"找到好友搜索框: {found}")

            if not found:
                await page.screenshot(path="doh-proxy-fail.png")
                print("已截图 doh-proxy-fail.png")

            await browser.close()

        if failures:
            print("\n未成功的请求（前 12）：")
            for url, count in failures.most_common(12):
                print(f"  {count:3d}x {url[:120]}")
        else:
            print("\n所有请求均成功")
        return 0 if found else 3
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
        log_file.close()
        text = Path("doh-proxy-test.log").read_text(encoding="utf-8", errors="replace")
        print("\n=== 代理日志 ===")
        print("\n".join(text.splitlines()[:25]))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
