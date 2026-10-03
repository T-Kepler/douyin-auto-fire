r"""按“时间槽”决定今天要不要发送，作为计划任务的统一入口。

背景
----
上游自带的用法是「计划任务到点直接跑 run.py」，一天只有一次机会，而且需要
「电脑开着 + 有人处于交互登录会话」两个条件同时成立。电脑一旦在这个时刻
关机或没有会话，就整天漏发，错过就没有第二次。

做法
----
把「当前所处的发送时间槽」当作判定单位（默认 20:30，可用 .env 的 SLOT_TIME 修改）：

    slot = 今天 HH:MM（若现在已过该时刻）否则 昨天 HH:MM

只要上一次**完全成功**的发送时间 >= slot，就说明本槽已经发过了，直接跳过；
否则执行一次真实发送。计划任务在「开机登录 / HH:MM / 23:00」三个时机调用本脚本，
于是：

* 到点时电脑开着       -> 正常发送
* 到点时电脑关着       -> 下次开机登录后立刻补发，不会整天漏掉
* 某次发送失败         -> 下一个时机自动重试
* 同一天多次触发       -> 后续触发直接跳过，不会重复发
* 次日早上登录         -> 因为昨天那槽已发过，不会误发（不会漂移到每天早上）

关于 DoH 代理（可选功能）
-----------------------
有些机器的系统 DNS 会把抖音域名解析到 0.0.0.0，例如装了 WebLimit 这类
自我约束 / 网站屏蔽工具。这类机器可以让 .env 配置

    DOUYIN_BROWSER_ARGS=--proxy-server=http://127.0.0.1:17890|...

指向本地 DoH 代理 doh_proxy.py，本脚本会自动拉起它、用完自动关闭，
从而不动那个屏蔽工具、也不影响系统其它程序。

普通机器既没有 doh_proxy.py 也没有这段配置，本脚本会自动跳过代理逻辑，
直接使用系统 DNS —— 同一份代码两边都能跑。

用法：
    .\.venv\Scripts\python.exe run-daily.py            # 按槽判定
    .\.venv\Scripts\python.exe run-daily.py --dry-run  # 只验证，不发送
    .\.venv\Scripts\python.exe run-daily.py --force    # 忽略判定，强制执行
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
# 注意：下面从 datetime 导入了 time 类，所以标准库 time 模块必须改名导入，
# 否则 time.monotonic 会变成 datetime.time 的属性而报错。
import time as time_module
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ARTIFACTS = PROJECT_ROOT / "artifacts"
RESULT_PATH = ARTIFACTS / "result.json"
# 本包装脚本自己记录的上次成功发送时间。
# 必须单独存一份：result.json 只保留最近一次运行，dry run 或失败会把它覆盖掉，
# 仅靠 result.json 会丢失「上次真实发送成功」的事实，导致重复发送。
STATE_PATH = ARTIFACTS / "last-success.json"
WRAPPER_LOG = ARTIFACTS / "run-daily.log"

# 先加载 .env，才能在下面读到 SLOT_TIME / DOUYIN_BROWSER_ARGS。
# run.py 自己也会加载 .env，但父进程需要在解析时间槽之前就拿到这些值。
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001 - 没有 dotenv 时退回默认值继续跑
    pass

DEFAULT_SLOT = (20, 30)


def _slot_from_env() -> tuple[int, int]:
    """发送时间槽，取自 .env 的 SLOT_TIME（形如 20:30），非法或缺失则用默认值。"""
    raw = (os.getenv("SLOT_TIME") or "").strip()
    if raw:
        try:
            hour_text, _, minute_text = raw.partition(":")
            hour, minute = int(hour_text), int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (TypeError, ValueError):
            pass
    return DEFAULT_SLOT


SLOT_HOUR, SLOT_MINUTE = _slot_from_env()

# 本地 DoH 代理（可选）：只有 .env 里配了 --proxy-server 才会启用。
PROXY_SCRIPT = PROJECT_ROOT / "doh_proxy.py"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 17890
PROXY_START_TIMEOUT_SECONDS = 15.0


def _port_open(host: str = PROXY_HOST, port: int = PROXY_PORT, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_needed() -> bool:
    """.env 里指定了 --proxy-server 才说明这台机器需要本地 DoH 代理。"""
    return "proxy-server" in (os.getenv("DOUYIN_BROWSER_ARGS") or "")


@contextmanager
def doh_proxy():
    """（可选）确保本地 DoH 代理在运行。

    不需要代理的机器直接放行；需要时若代理已在跑就复用，
    退出时只关闭自己启动的那个进程，不误杀别人的。
    """
    if not _proxy_needed():
        yield
        return

    if not PROXY_SCRIPT.is_file():
        log("提示：.env 指定了代理，但找不到 doh_proxy.py，本次直接使用系统 DNS")
        yield
        return

    if _port_open():
        log(f"DoH 代理已在 {PROXY_HOST}:{PROXY_PORT} 运行，直接复用")
        yield
        return

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    proxy_log = ARTIFACTS / "doh-proxy.log"
    with proxy_log.open("a", encoding="utf-8") as sink:
        sink.write(f"\n=== 启动 {datetime.now().isoformat(timespec='seconds')} ===\n")
        sink.flush()
        process = subprocess.Popen(
            [sys.executable, str(PROXY_SCRIPT), "--port", str(PROXY_PORT)],
            cwd=str(PROJECT_ROOT),
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time_module.monotonic() + PROXY_START_TIMEOUT_SECONDS
            while time_module.monotonic() < deadline:
                if _port_open():
                    break
                if process.poll() is not None:
                    raise RuntimeError(
                        f"DoH 代理启动即退出（exit={process.returncode}），"
                        f"详见 {proxy_log}"
                    )
                time_module.sleep(0.2)
            else:
                raise RuntimeError(f"DoH 代理 {PROXY_START_TIMEOUT_SECONDS:.0f}s 内未就绪")
            log(f"DoH 代理已启动 (pid={process.pid}, {PROXY_HOST}:{PROXY_PORT})")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                log("DoH 代理已关闭")


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    try:
        WRAPPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with WRAPPER_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def current_slot(now: datetime) -> datetime:
    """返回当前所属的发送时间槽。"""
    boundary = datetime.combine(now.date(), time(SLOT_HOUR, SLOT_MINUTE)).astimezone()
    if now >= boundary:
        return boundary
    return boundary - timedelta(days=1)


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def last_successful_send() -> datetime | None:
    """上一次「全部目标都成功」的真实发送时间，取两个来源里较新的一个。

    1. artifacts/last-success.json  —— 本包装脚本自己写的，最可靠
    2. artifacts/result.json        —— 项目自身产物，兼容手工直接跑 run.py

    dry run 不计入；任一目标失败也不计入（这样失败会被下一个时机自动重试）。
    """
    candidates: list[datetime] = []

    try:
        # utf-8-sig：同时兼容带 BOM（记事本/PowerShell 写出的）和不带 BOM 的 JSON
        state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(state, dict):
            parsed = _parse_datetime(state.get("finished_at"))
            if parsed:
                candidates.append(parsed)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    try:
        data = json.loads(RESULT_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and not data.get("dry_run"):
            results = data.get("results")
            if (
                isinstance(results, list)
                and results
                and all(
                    item.get("status") == "success"
                    for item in results
                    if isinstance(item, dict)
                )
                and len(results) == sum(1 for i in results if isinstance(i, dict))
            ):
                parsed = _parse_datetime(data.get("finished_at"))
                if parsed:
                    candidates.append(parsed)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return max(candidates) if candidates else None


def record_success() -> None:
    """记录本次真实发送成功的时间。"""
    try:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        payload = {"finished_at": datetime.now().astimezone().isoformat()}
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(STATE_PATH)
    except OSError as exc:
        log(f"警告: 无法写入 {STATE_PATH.name}: {exc}")


def run_project(dry_run: bool) -> int:
    command = [sys.executable, str(PROJECT_ROOT / "run.py")]
    if dry_run:
        command.append("--dry-run")
    # 复用项目自己的入口，保持与原 run.py 行为完全一致（含多账号支持）
    return subprocess.run(command, cwd=str(PROJECT_ROOT)).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="按时间槽运行抖音续火花任务")
    parser.add_argument("--dry-run", action="store_true", help="只验证，不真实发送")
    parser.add_argument("--force", action="store_true", help="忽略时间槽判定，强制执行")
    args = parser.parse_args()

    now = datetime.now().astimezone()
    slot = current_slot(now)
    previous = last_successful_send()

    if previous is None:
        log("未找到可用的成功发送记录，将继续执行")
    else:
        log(f"上次成功发送: {previous.isoformat(timespec='seconds')}")

    if not args.force and previous is not None and previous >= slot:
        log(f"当前时间槽 {slot.isoformat(timespec='seconds')} 已发送过，跳过")
        return 0

    log(f"需要发送（当前槽 {slot.isoformat(timespec='seconds')}）"
        f"{' [dry-run]' if args.dry_run else ''}")
    try:
        with doh_proxy():
            code = run_project(args.dry_run)
    except Exception as exc:  # noqa: BLE001 - 代理起不来时给出明确原因
        log(f"启动 DoH 代理失败: {type(exc).__name__}: {exc}")
        return 4

    log(f"run.py 退出码: {code}")
    if code == 0 and not args.dry_run:
        record_success()
        log("已记录本次成功发送时间")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
