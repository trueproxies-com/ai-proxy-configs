#!/usr/bin/env python3
"""Counting relay for peak concurrent connections .

Chromium connects to this relay as its HTTP proxy. Every CONNECT tunnel and
plain HTTP request is forwarded to the upstream proxy in TP_PROXY_URL (with
Proxy-Authorization), or straight to the target with --direct. The relay
counts open tunnels and keeps the peak since the last /reset.

Control endpoint on PORT+1:  GET /stats -> {"open": n, "peak": n}
                             GET /reset -> {"peak": 0}

Usage:  relay.py --port 18080 [--direct]
Credentials come from the environment and are never logged.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from urllib.parse import urlsplit

state = {"open": 0, "peak": 0}
UPSTREAM: dict | None = None


def bump(delta: int) -> None:
    state["open"] += delta
    if state["open"] > state["peak"]:
        state["peak"] = state["open"]


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def open_upstream(host: str, port: int, connect: bool, first_line: bytes, headers: bytes):
    """Return (reader, writer) to the target, via the upstream proxy or direct."""
    if UPSTREAM:
        r, w = await asyncio.open_connection(UPSTREAM["host"], UPSTREAM["port"])
        auth = b""
        if UPSTREAM.get("auth"):
            auth = b"Proxy-Authorization: Basic " + UPSTREAM["auth"] + b"\r\n"
        if connect:
            w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n".encode() + auth + b"\r\n")
            await w.drain()
            line = await r.readline()
            while True:  # drain upstream response headers
                h = await r.readline()
                if h in (b"\r\n", b""):
                    break
            if not line.startswith(b"HTTP/1.1 200") and not line.startswith(b"HTTP/1.0 200"):
                w.close()
                raise ConnectionError(f"upstream refused CONNECT: {line.decode(errors='replace').strip()}")
        else:
            w.write(first_line + auth + headers)
            await w.drain()
        return r, w
    r, w = await asyncio.open_connection(host, port)
    if not connect:
        w.write(first_line + headers)
        await w.drain()
    return r, w


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        request_line = await client_r.readline()
        if not request_line:
            client_w.close()
            return
        headers = b""
        while True:
            h = await client_r.readline()
            if h in (b"\r\n", b""):
                break
            if not h.lower().startswith(b"proxy-authorization:") and not h.lower().startswith(b"proxy-connection:"):
                headers += h
        headers += b"\r\n"
        method, target, _ = request_line.split(b" ", 2)
        if method == b"CONNECT":
            host, _, port = target.decode().partition(":")
            bump(1)
            try:
                up_r, up_w = await open_upstream(host, int(port or 443), True, request_line, headers)
            except Exception as e:  # noqa: BLE001
                bump(-1)
                client_w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await client_w.drain()
                client_w.close()
                return
            client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_w.drain()
            try:
                await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))
            finally:
                bump(-1)
        else:
            s = urlsplit(target.decode())
            host, port = s.hostname or "", s.port or 80
            bump(1)
            try:
                if UPSTREAM:
                    up_r, up_w = await open_upstream(host, port, False, request_line, headers)
                else:
                    path = (s.path or "/") + (f"?{s.query}" if s.query else "")
                    up_r, up_w = await open_upstream(host, port, False, f"{method.decode()} {path} HTTP/1.1\r\n".encode(), headers)
                await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))
            except Exception:  # noqa: BLE001
                client_w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await client_w.drain()
                client_w.close()
            finally:
                bump(-1)
    except Exception:  # noqa: BLE001
        try:
            client_w.close()
        except Exception:  # noqa: BLE001
            pass


async def control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    line = await reader.readline()
    while (await reader.readline()) not in (b"\r\n", b""):
        pass
    path = line.split(b" ")[1].decode() if b" " in line else "/"
    if path.startswith("/reset"):
        state["peak"] = state["open"]
        body = json.dumps({"peak": state["peak"]})
    else:
        body = json.dumps(state)
    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
    await writer.drain()
    writer.close()


async def main() -> int:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18750)
    ap.add_argument("--direct", action="store_true")
    args = ap.parse_args()
    if not args.direct:
        url = os.environ.get("TP_PROXY_URL")
        if not url:
            print("TP_PROXY_URL is not set; pass --direct", file=sys.stderr)
            return 2
        s = urlsplit(url)
        UPSTREAM = {"host": s.hostname, "port": s.port or 8080}
        if s.username:
            UPSTREAM["auth"] = base64.b64encode(f"{s.username}:{s.password or ''}".encode())
    srv = await asyncio.start_server(handle, "127.0.0.1", args.port)
    ctl = await asyncio.start_server(control, "127.0.0.1", args.port + 1)
    print(f"relay on 127.0.0.1:{args.port} ({'direct' if args.direct else 'upstream proxy'}), control on {args.port + 1}", flush=True)
    async with srv, ctl:
        await asyncio.gather(srv.serve_forever(), ctl.serve_forever())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
