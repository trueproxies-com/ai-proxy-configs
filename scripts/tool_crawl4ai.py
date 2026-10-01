#!/usr/bin/env python3
"""Crawl4AI through the TrueProxies gateway .

Two tested configurations, each over the first N URLs of urls.csv:
  basic    one ProxyConfig; the gateway rotates the exit IP per request
  sticky   one ProxyConfig per crawl with "-session-<id>-lifetime-600" on the
           username, so every request of that crawl keeps one exit IP

Before the crawl, each configuration fetches api.ipify.org through the proxy to
prove the route. Credentials come from TP_PROXY_URL only; a trusted-IP client
sends the username with an empty password. Nothing here prints the username.

Outputs: tool_crawl4ai_results.csv, tool_crawl4ai_summary.json
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import statistics
import sys
import time
from importlib.metadata import version as pkg_version
from urllib.parse import urlsplit

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, ProxyConfig

N = int(os.environ.get("TOOL_URLS", "20"))
LIFETIME = 600


def proxy_for(session: str | None) -> ProxyConfig:
    s = urlsplit(os.environ["TP_PROXY_URL"])
    user = s.username or ""
    if session and user:
        user = f"{user}-session-{session}-lifetime-{LIFETIME}"
    return ProxyConfig(server=f"{s.scheme}://{s.hostname}:{s.port}", username=user, password=s.password or "")


async def exit_ip(crawler: AsyncWebCrawler, cfg: CrawlerRunConfig) -> str:
    r = await crawler.arun(url="https://api.ipify.org?format=json", config=cfg)
    m = re.search(r'"ip"\s*:\s*"([^"]+)"', r.html or "")
    return m.group(1) if m else f"(no ip: {r.error_message or r.status_code})"


async def run(mode: str, urls: list[dict], writer: csv.DictWriter) -> list[dict]:
    rows = []
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        probe = CrawlerRunConfig(proxy_config=proxy_for("probe0" if mode == "sticky" else None), cache_mode=CacheMode.BYPASS, page_timeout=30_000)
        ip = await exit_ip(crawler, probe)
        print(f"[{mode}] exit ip via gateway: {ip}")
        for i, row in enumerate(urls, 1):
            cfg = CrawlerRunConfig(proxy_config=proxy_for(f"crawl{i:02d}" if mode == "sticky" else None), cache_mode=CacheMode.BYPASS, page_timeout=30_000)
            t0 = time.perf_counter()
            try:
                r = await crawler.arun(url=row["url"], config=cfg)
                rec = {"mode": mode, "url": row["url"], "type": row["type"], "ok": bool(r.success), "status": r.status_code or 0,
                       "html_len": len(r.html or ""), "markdown_len": len(str(r.markdown or "")), "ms": int((time.perf_counter() - t0) * 1000),
                       "error": (r.error_message or "")[:160]}
            except Exception as e:  # noqa: BLE001
                rec = {"mode": mode, "url": row["url"], "type": row["type"], "ok": False, "status": 0, "html_len": 0, "markdown_len": 0,
                       "ms": int((time.perf_counter() - t0) * 1000), "error": f"{e.__class__.__name__}: {str(e)[:140]}"}
            rows.append(rec)
            writer.writerow(rec)
            print(f"[{mode}] {rec['status']:>3} ok={rec['ok']} {rec['ms']:>6} ms md={rec['markdown_len']:>6} {row['url'][:70]} {rec['error']}")
    return rows


async def main() -> int:
    if not os.environ.get("TP_PROXY_URL"):
        print("TP_PROXY_URL is not set", file=sys.stderr)
        return 2
    urls = list(csv.DictReader(open("urls.csv", newline="")))[:N]
    fields = ["mode", "url", "type", "ok", "status", "html_len", "markdown_len", "ms", "error"]
    with open("tool_crawl4ai_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        basic = await run("basic", urls, w)
        sticky = await run("sticky", urls, w)
    summary = {"tool": "crawl4ai", "version": pkg_version("crawl4ai"), "n": N, "date": time.strftime("%Y-%m-%d")}
    for name, rows in (("basic", basic), ("sticky", sticky)):
        okr = [r for r in rows if r["ok"]]
        summary[name] = {"ok": len(okr), "n": len(rows), "median_ms": int(statistics.median(r["ms"] for r in okr)) if okr else None,
                         "errors": sorted({r["error"] for r in rows if r["error"]})}
    json.dump(summary, open("tool_crawl4ai_summary.json", "w"), indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
