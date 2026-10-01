#!/usr/bin/env python3
"""Browser Use through the TrueProxies gateway .

Layers:
  browser   Browser(proxy=ProxySettings(...)) with no agent: open each URL,
            read the title, confirm the exit IP via api.ipify.org. Runs without
            any LLM key. Labelled "browser layer only" on the page.
  agent     5 tasks ("open URL, return the title and first paragraph") with an
            LLM, only when LLM_API_KEY (OpenAI-compatible) is set.

One sticky session per browser: the username carries "-session-<id>-lifetime-600".
Credentials come from TP_PROXY_URL only; nothing here prints the username.

Outputs: tool_browser_use_results.csv, tool_browser_use_summary.json
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

from browser_use import Browser
from browser_use.browser import ProxySettings

N = int(os.environ.get("TOOL_URLS", "5"))
LIFETIME = 600


def proxy_for(session: str) -> ProxySettings:
    s = urlsplit(os.environ["TP_PROXY_URL"])
    user = s.username or ""
    if user:
        user = f"{user}-session-{session}-lifetime-{LIFETIME}"
    return ProxySettings(server=f"{s.scheme}://{s.hostname}:{s.port}", username=user, password=s.password or "")


async def page_text(browser: Browser, url: str) -> tuple[str, str]:
    """Navigate and return (title, page text) with the 0.13 Browser API."""
    await browser.navigate_to(url)
    await asyncio.sleep(2)
    title = await browser.get_current_page_title()
    body = ""
    try:
        body = await browser.get_state_as_text()
    except Exception:  # noqa: BLE001
        summary = await browser.get_browser_state_summary(include_screenshot=False)
        body = str(summary)
    return str(title or ""), str(body or "")


async def browser_layer(urls: list[dict], writer: csv.DictWriter) -> list[dict]:
    rows = []
    for i, row in enumerate(urls, 1):
        browser = Browser(headless=True, proxy=proxy_for(f"agent{i:02d}"))
        t0 = time.perf_counter()
        try:
            await browser.start()
            _, ipbody = await page_text(browser, "https://api.ipify.org?format=json")
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', ipbody)
            exit_ip = m.group(1) if m else "(no ip)"
            title, body = await page_text(browser, row["url"])
            rec = {"layer": "browser", "url": row["url"], "type": row["type"], "ok": bool(title), "exit_ip": exit_ip, "title": title[:80],
                   "ms": int((time.perf_counter() - t0) * 1000), "error": ""}
        except Exception as e:  # noqa: BLE001
            rec = {"layer": "browser", "url": row["url"], "type": row["type"], "ok": False, "exit_ip": "", "title": "",
                   "ms": int((time.perf_counter() - t0) * 1000), "error": f"{e.__class__.__name__}: {str(e)[:140]}"}
        finally:
            try:
                await browser.stop()
            except Exception:  # noqa: BLE001
                pass
        rows.append(rec)
        writer.writerow(rec)
        print(f"[browser] ok={rec['ok']} exit={rec['exit_ip']} {rec['ms']:>6} ms {row['url'][:70]} {rec['error']}")
    return rows


async def agent_layer(urls: list[dict], writer: csv.DictWriter) -> list[dict]:
    from browser_use import Agent  # noqa: PLC0415
    from browser_use.llm import ChatOpenAI  # noqa: PLC0415

    llm = ChatOpenAI(model=os.environ.get("LLM_MODEL", "gpt-4o-mini"), api_key=os.environ["LLM_API_KEY"])
    rows = []
    for i, row in enumerate(urls, 1):
        browser = Browser(headless=True, proxy=proxy_for(f"agent{i:02d}"))
        t0 = time.perf_counter()
        try:
            agent = Agent(task=f"Open {row['url']} and return the page title and its first paragraph.", llm=llm, browser=browser)
            history = await agent.run(max_steps=8)
            final = history.final_result() if hasattr(history, "final_result") else str(history)
            rec = {"layer": "agent", "url": row["url"], "type": row["type"], "ok": bool(final), "exit_ip": "", "title": str(final or "")[:80],
                   "ms": int((time.perf_counter() - t0) * 1000), "error": ""}
        except Exception as e:  # noqa: BLE001
            rec = {"layer": "agent", "url": row["url"], "type": row["type"], "ok": False, "exit_ip": "", "title": "",
                   "ms": int((time.perf_counter() - t0) * 1000), "error": f"{e.__class__.__name__}: {str(e)[:140]}"}
        rows.append(rec)
        writer.writerow(rec)
        print(f"[agent] ok={rec['ok']} {rec['ms']:>6} ms {row['url'][:70]} {rec['error']}")
    return rows


async def main() -> int:
    if not os.environ.get("TP_PROXY_URL"):
        print("TP_PROXY_URL is not set", file=sys.stderr)
        return 2
    urls = list(csv.DictReader(open("urls.csv", newline="")))[:N]
    fields = ["layer", "url", "type", "ok", "exit_ip", "title", "ms", "error"]
    with open("tool_browser_use_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        rows = await browser_layer(urls, w)
        agent_rows = await agent_layer(urls, w) if os.environ.get("LLM_API_KEY") else []
    summary = {"tool": "browser-use", "version": pkg_version("browser-use"), "n": N, "date": time.strftime("%Y-%m-%d"),
               "layers": "browser+agent" if agent_rows else "browser layer only"}
    for name, rs in (("browser", rows), ("agent", agent_rows)):
        okr = [r for r in rs if r["ok"]]
        summary[name] = {"ok": len(okr), "n": len(rs), "median_ms": int(statistics.median(r["ms"] for r in okr)) if okr else None,
                         "exit_ips_distinct": len({r["exit_ip"] for r in okr if r.get("exit_ip")}), "errors": sorted({r["error"] for r in rs if r["error"]})}
    json.dump(summary, open("tool_browser_use_summary.json", "w"), indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
