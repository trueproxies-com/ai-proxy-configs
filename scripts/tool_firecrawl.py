#!/usr/bin/env python3
"""Self-hosted Firecrawl through the TrueProxies gateway .

Start the self-hosted stack yourself with PROXY_SERVER / PROXY_USERNAME /
PROXY_PASSWORD and USE_DB_AUTHENTICATION=false in its .env (from the pinned
release's .env.example). This script only talks to the local API:

  * /v1/scrape on api.ipify.org to prove the route and read the exit IP
  * /v1/scrape on the first N URLs of urls.csv (markdown only)

Outputs: tool_firecrawl_results.csv, tool_firecrawl_summary.json
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
import sys
import time

import httpx

API = os.environ.get("FIRECRAWL_URL", "http://127.0.0.1:3002")
N = int(os.environ.get("TOOL_URLS", "20"))
VERSION = os.environ.get("FIRECRAWL_VERSION", "unknown")


def scrape(client: httpx.Client, url: str) -> dict:
    r = client.post(f"{API}/v1/scrape", json={"url": url, "formats": ["markdown"], "timeout": 30000})
    try:
        body = r.json()
    except ValueError:
        body = {"success": False, "error": r.text[:160]}
    return {"status": r.status_code, **body}


def main() -> int:
    urls = list(csv.DictReader(open("urls.csv", newline="")))[:N]
    fields = ["url", "type", "ok", "status", "markdown_len", "ms", "error"]
    rows = []
    with httpx.Client(timeout=60.0) as client, open("tool_firecrawl_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        probe = scrape(client, "https://api.ipify.org?format=json")
        text = json.dumps(probe.get("data", {}))
        m = re.search(r'\\?"ip\\?"\s*:\s*\\?"([^"\\]+)', text)
        print(f"[firecrawl] exit ip via gateway: {m.group(1) if m else '(no ip: ' + str(probe.get('error', probe.get('status'))) + ')'}")
        for row in urls:
            t0 = time.perf_counter()
            try:
                res = scrape(client, row["url"])
                md = (res.get("data") or {}).get("markdown") or ""
                rec = {"url": row["url"], "type": row["type"], "ok": bool(res.get("success")) and len(md) > 0, "status": res.get("status"),
                       "markdown_len": len(md), "ms": int((time.perf_counter() - t0) * 1000), "error": str(res.get("error") or "")[:160]}
            except Exception as e:  # noqa: BLE001
                rec = {"url": row["url"], "type": row["type"], "ok": False, "status": 0, "markdown_len": 0,
                       "ms": int((time.perf_counter() - t0) * 1000), "error": f"{e.__class__.__name__}: {str(e)[:140]}"}
            rows.append(rec)
            w.writerow(rec)
            print(f"[firecrawl] {rec['status']} ok={rec['ok']} {rec['ms']:>6} ms md={rec['markdown_len']:>6} {row['url'][:70]} {rec['error']}")
    okr = [r for r in rows if r["ok"]]
    summary = {"tool": "firecrawl", "version": VERSION, "n": len(rows), "ok": len(okr), "date": time.strftime("%Y-%m-%d"),
               "median_ms": int(statistics.median(r["ms"] for r in okr)) if okr else None, "errors": sorted({r["error"] for r in rows if r["error"]})}
    json.dump(summary, open("tool_firecrawl_summary.json", "w"), indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
