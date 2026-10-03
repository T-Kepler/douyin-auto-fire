"""只给自动化用的本地 CONNECT 代理，自己做 DoH 域名解析。

为什么需要它
------------
本机装有 WebLimit（NRPT + hosts），把 douyin.com 等域名的系统 DNS 解析
黑洞成 0.0.0.0。Chromium 自己的 DoH 参数无法覆盖 Windows 的 NRPT 策略，
而 --host-resolver-rules 又需要预先知道每个子域的 IP（抖音子域很多，会失效）。

本代理的思路：
  Chromium --proxy-server=127.0.0.1:<port>
      -> 所有请求以 "CONNECT host:port" 交给本代理
      -> 本代理用 DoH 解析主机名（不经过 Windows DNS 客户端）
      -> 建 TCP 隧道转发

因此它**完全不改动 WebLimit**：系统 DNS 依旧屏蔽抖音，
只有连到这个本地代理的进程（即自动化自己的浏览器）能解析到真实 IP。
代理默认只监听 127.0.0.1，随任务启动、任务结束即退出。

用法：
    python doh_proxy.py --port 17890
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DOH_ENDPOINTS = (
    "https://doh.pub/dns-query",
    "https://dns.alidns.com/resolve",
)
CACHE_TTL_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 20.0

_executor = ThreadPoolExecutor(max_workers=8)
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = asyncio.Lock()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(message: str) -> None:
    print(f"[doh-proxy] {message}", flush=True)


def _doh_query_sync(host: str) -> str:
    last_error: Exception | None = None
    for endpoint in DOH_ENDPOINTS:
        url = f"{endpoint}?name={urllib.parse.quote(host)}&type=A"
        try:
            request = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            answers = [a["data"] for a in payload.get("Answer", []) if a.get("type") == 1]
            if answers:
                return answers[0]
        except Exception as exc:  # noqa: BLE001 - 换下一个 DoH 端点
            last_error = exc
    raise RuntimeError(f"DoH 无法解析 {host}: {last_error}")


async def resolve(host: str) -> str:
    # 已经是 IP 就直接用
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    if _is_ip_literal(host):
        return host

    now = time.monotonic()
    cached = _cache.get(host)
    if cached and cached[0] > now:
        return cached[1]

    async with _cache_lock:
        cached = _cache.get(host)
        if cached and cached[0] > now:
            return cached[1]
        loop = asyncio.get_running_loop()
        address = await loop.run_in_executor(_executor, _doh_query_sync, host)
        _cache[host] = (time.monotonic() + CACHE_TTL_SECONDS, address)
        return address


def _is_ip_literal(host: str) -> bool:
    if host.replace(".", "").isdigit():
        return True
    return ":" in host


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    """读到空行为止，返回剩余头部原文（CONNECT 场景直接丢弃即可）。"""
    buffered = b""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT_SECONDS)
        if line in (b"\r\n", b"\n", b""):
            return buffered
        buffered += line
        if len(buffered) > 65536:
            return buffered


async def _handle_connect(host: str, port: int, reader, writer) -> None:
    address = await resolve(host)
    remote_reader, remote_writer = await asyncio.wait_for(
        asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT_SECONDS
    )
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    # 注意：CONNECT 的请求头到此为止，属于「代理和客户端之间」的 HTTP 报文，
    # 绝不能转发给 origin —— 否则 origin 会在 TLS ClientHello 之前收到
    # "Host: ..." 之类的明文，直接导致 ERR_SSL_PROTOCOL_ERROR。
    await asyncio.gather(
        _pipe(reader, remote_writer),
        _pipe(remote_reader, writer),
        return_exceptions=True,
    )


async def _handle_plain_http(method: str, target: str, version: str, reader, writer, leftover: bytes) -> None:
    """处理明文 HTTP（抖音全站 HTTPS，这里只是兜底）。"""
    parsed = urllib.parse.urlsplit(target)
    if not parsed.hostname:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return
    host = parsed.hostname
    port = parsed.port or 80
    address = await resolve(host)

    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    request_line = f"{method} {path} {version}\r\n".encode("latin1")

    remote_reader, remote_writer = await asyncio.wait_for(
        asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT_SECONDS
    )
    remote_writer.write(request_line)
    remote_writer.write(f"Host: {parsed.netloc}\r\n".encode("latin1"))
    remote_writer.write(leftover)
    remote_writer.write(b"\r\n")
    await remote_writer.drain()
    await asyncio.gather(
        _pipe(reader, remote_writer),
        _pipe(remote_reader, writer),
        return_exceptions=True,
    )


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(
            reader.readline(), timeout=CONNECT_TIMEOUT_SECONDS
        )
        if not request_line:
            return
        parts = request_line.decode("latin1").split()
        if len(parts) < 2:
            return
        method, target = parts[0].upper(), parts[1]
        version = parts[2] if len(parts) > 2 else "HTTP/1.1"
        leftover = await _read_headers(reader)

        if method == "CONNECT":
            host, _, port_text = target.rpartition(":")
            port = int(port_text) if port_text.isdigit() else 443
            await _handle_connect(host, port, reader, writer)
        else:
            await _handle_plain_http(method, target, version, reader, writer, leftover)
    except Exception as exc:  # noqa: BLE001 - 单连接失败不影响其它连接
        try:
            writer.write(
                f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n".encode("ascii")
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        log(f"连接处理失败: {type(exc).__name__}: {str(exc)[:120]}")
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(port: int, host: str) -> None:
    server = await asyncio.start_server(_handle, host, port)
    log(f"监听 http://{host}:{port}  (DoH: {', '.join(DOH_ENDPOINTS)})")
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 DoH 解析代理")
    parser.add_argument("--port", type=int, default=17890)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.port, args.host))
    except KeyboardInterrupt:
        log("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
