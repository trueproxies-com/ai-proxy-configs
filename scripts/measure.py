#!/usr/bin/env python3
"""Bytes per page for AI workloads .

Configs:
  A  httpx, HTML only, Accept-Encoding gzip/br; bytes = response.num_bytes_downloaded
  B  Playwright Chromium headless, images/fonts/media aborted; bytes = sum of CDP
     Network.loadingFinished.encodedDataLength
  C  Playwright Chromium headless, everything; same measurement as B

Proxy: TP_PROXY_URL (http://USER:PASS@host:port). Pass --direct to run without a
proxy (dry runs). Pass --relay 127.0.0.1:PORT to route the browser through
relay.py and record the peak concurrent tunnels per page (configs B and C).

Outputs: results.csv (appended) and summary.json (rewritten).
Credentials are read from the environment only and never written anywhere.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def pkg_version(name: str) -> str:
    """Version string, or the TP_<NAME>_VERSION env override, or "unknown" when the metadata is missing (the Playwright image ships without it)."""
    try:
        return _pkg_version(name)
    except PackageNotFoundError:
        return os.environ.get(f"TP_{name.upper()}_VERSION", "unknown")
from urllib.parse import urlsplit

import httpx
import tiktoken
import trafilatura

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # httpx-only runs
    class PlaywrightTimeoutError(Exception):
        pass

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
BLOCKED_TYPES = {"image", "font", "media"}
LOAD_TIMEOUT_MS = 30_000
IDLE_TIMEOUT_MS = 3_000
FIELDS = ["url", "type", "config", "status", "ok", "partial", "bytes", "tokens", "ms", "peak_conns", "error"]

enc = tiktoken.get_encoding("o200k_base")


def main_text(html: str) -> str:
    if not html:
        return ""
    return trafilatura.extract(html, include_comments=False, include_tables=False) or ""


def proxy_parts(proxy_url: str) -> dict:
    s = urlsplit(proxy_url)
    out = {"server": f"{s.scheme}://{s.hostname}:{s.port}"}
    if s.username:
        out["username"] = s.username
        # Playwright needs both fields; an empty password is how a trusted-IP client authenticates.
        out["password"] = s.password or ""
    return out


SECRETS: list[str] = []


def scrub(text: str) -> str:
    for sec in SECRETS:
        if sec:
            text = text.replace(sec, "***")
    return text


def run_httpx(url: str, proxy: str | None) -> dict:
    t0 = time.perf_counter()
    try:
        with httpx.Client(
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "gzip, br", "Accept-Language": "en-US,en;q=0.9"},
            proxy=proxy, follow_redirects=True, timeout=30.0, http2=False,
        ) as client:
            with client.stream("GET", url) as r:
                body = r.read()
                nbytes = r.num_bytes_downloaded
                status = r.status_code
                html = body.decode(r.encoding or "utf-8", errors="replace") if "html" in r.headers.get("content-type", "") else ""
        text = main_text(html)
        return {"status": status, "ok": status == 200 and len(text) > 0, "partial": False, "bytes": nbytes,
                "tokens": len(enc.encode(text)) if text else 0, "ms": int((time.perf_counter() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001 - we record every failure class
        return {"status": 0, "ok": False, "partial": False, "bytes": 0, "tokens": 0, "ms": int((time.perf_counter() - t0) * 1000), "error": scrub(f"{e.__class__.__name__}: {str(e)[:160]}")}


def relay_call(relay: str | None, path: str) -> dict:
    if not relay:
        return {}
    host, port = relay.split(":")
    try:
        return httpx.get(f"http://{host}:{int(port) + 1}{path}", timeout=5.0).json()
    except Exception:  # noqa: BLE001
        return {}


def run_browser(pw, url: str, config: str, proxy: str | None, relay: str | None) -> dict:
    browser = None
    t0 = time.perf_counter()
    launch = {"headless": True}
    if relay:
        launch["proxy"] = {"server": f"http://{relay}"}
    elif proxy:
        launch["proxy"] = proxy_parts(proxy)
    try:
        browser = pw.chromium.launch(**launch)
        context = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 768}, locale="en-US")
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        total = {"bytes": 0, "requests": 0}
        cdp.on("Network.loadingFinished", lambda e: total.__setitem__("bytes", total["bytes"] + int(e.get("encodedDataLength", 0))))
        cdp.on("Network.requestWillBeSent", lambda e: total.__setitem__("requests", total["requests"] + 1))
        if config == "B":
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED_TYPES else route.continue_())
        main = {"status": 0}

        def on_response(r):
            if r.request.is_navigation_request() and r.frame == page.main_frame and main["status"] == 0:
                main["status"] = r.status

        page.on("response", on_response)
        relay_call(relay, "/reset")
        partial = False
        error = ""
        try:
            resp = page.goto(url, wait_until="load", timeout=LOAD_TIMEOUT_MS)
            if resp:
                main["status"] = resp.status
            try:
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - idle is best effort, the 3 s cap is the rule
                pass
        except PlaywrightTimeoutError as e:
            # The 30 s cap: the document may be there even though `load` never fired.
            # Keep what the browser transferred; the customer paid for it.
            partial = True
            error = scrub(str(e).splitlines()[0][:120])
        html = ""
        try:
            html = page.content()
        except Exception:  # noqa: BLE001
            pass
        text = main_text(html)
        peak = relay_call(relay, "/stats").get("peak", "")
        status = main["status"]
        out = {"status": status, "ok": status == 200 and len(text) > 0, "partial": partial, "bytes": total["bytes"],
               "tokens": len(enc.encode(text)) if text else 0, "ms": int((time.perf_counter() - t0) * 1000), "peak_conns": peak, "error": error}
        context.close()
        return out
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "ok": False, "partial": False, "bytes": 0, "tokens": 0, "ms": int((time.perf_counter() - t0) * 1000), "peak_conns": "",
                "error": scrub(f"{e.__class__.__name__}: {str(e)[:160]}")}
    finally:
        if browser:
            browser.close()


def summarize(rows: list[dict], versions: dict) -> dict:
    out = {"test_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "versions": versions, "configs": {}}
    for cfg in sorted({r["config"] for r in rows}):
        sub = [r for r in rows if r["config"] == cfg]
        okr = [r for r in sub if r["ok"] in ("True", True)]
        complete = [r for r in okr if r.get("partial") not in ("True", True)]
        b = sorted(int(r["bytes"]) for r in okr)
        tk = [int(r["tokens"]) for r in okr if int(r["tokens"]) > 0]
        ms = [int(r["ms"]) for r in okr]
        peaks = [int(r["peak_conns"]) for r in okr if str(r.get("peak_conns", "")).isdigit()]
        q = lambda xs, p: xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))] if xs else None
        out["configs"][cfg] = {
            "n": len(sub), "ok": len(okr), "complete": len(complete), "partial": len(okr) - len(complete),
            "success_rate": round(len(okr) / len(sub), 3) if sub else None,
            "median_bytes": int(statistics.median(b)) if b else None, "p75_bytes": q(b, 0.75),
            "median_mb": round(statistics.median(b) / 1e6, 3) if b else None,
            "median_tokens": int(statistics.median(tk)) if tk else None,
            "median_ms": int(statistics.median(ms)) if ms else None,
            "peak_conns_max": max(peaks) if peaks else None, "peak_conns_median": int(statistics.median(peaks)) if peaks else None,
            "errors": sorted({r["error"] for r in sub if r["error"]}),
        }
    return out


WATCHDOG_S = 90  # hard limit per page load, including browser start-up; a hung Chromium is killed with its process group


def measure_one(url: str, typ: str, cfg: str, proxy: str | None, relay: str | None) -> dict:
    """One page load in this process. Used by the child processes the main loop spawns."""
    if cfg == "A":
        res = run_httpx(url, proxy)
        res["peak_conns"] = ""
        res.setdefault("partial", False)
        return {"url": url, "type": typ, "config": cfg, **res}
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        res = run_browser(pw, url, cfg, proxy, relay)
    return {"url": url, "type": typ, "config": cfg, **res}


def run_child(url: str, typ: str, cfg: str, direct: bool, relay: str | None) -> dict:
    """Spawn measure_one in a child process with a hard timeout; kill the whole process group on expiry."""
    cmd = [sys.executable, os.path.abspath(__file__), "--one-url", url, "--one-type", typ, "--one-config", cfg]
    if direct:
        cmd.append("--direct")
    if relay:
        cmd += ["--relay", relay]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=WATCHDOG_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return {"url": url, "type": typ, "config": cfg, "status": 0, "ok": False, "partial": False, "bytes": 0, "tokens": 0,
                "ms": int((time.perf_counter() - t0) * 1000), "peak_conns": "", "error": f"watchdog: killed after {WATCHDOG_S} s"}
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"url": url, "type": typ, "config": cfg, "status": 0, "ok": False, "partial": False, "bytes": 0, "tokens": 0,
                "ms": int((time.perf_counter() - t0) * 1000), "peak_conns": "", "error": scrub(f"child failed: {(err or out)[-160:]}")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default="urls.csv")
    ap.add_argument("--configs", default="A,B,C")
    ap.add_argument("--limit", type=int, default=0, help="first N URLs only (dry runs)")
    ap.add_argument("--direct", action="store_true", help="no proxy")
    ap.add_argument("--relay", default="", help="127.0.0.1:PORT of relay.py for peak-connection counting")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--summary", default="summary.json")
    ap.add_argument("--budget-mb", type=float, default=2000.0, help="stop when measured bytes exceed this")
    ap.add_argument("--one-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--one-type", default="", help=argparse.SUPPRESS)
    ap.add_argument("--one-config", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    proxy = None if args.direct else os.environ.get("TP_PROXY_URL")
    if proxy:
        ps = urlsplit(proxy)
        SECRETS.extend([x for x in (ps.password, ps.username, proxy) if x])
    if not args.direct and not proxy:
        print("TP_PROXY_URL is not set; pass --direct for a dry run", file=sys.stderr)
        return 2

    if args.one_url:
        rec = measure_one(args.one_url, args.one_type, args.one_config.upper(), proxy, args.relay or None)
        print(json.dumps(rec))
        return 0

    urls = list(csv.DictReader(open(args.urls, newline="")))
    if args.limit:
        urls = urls[: args.limit]
    configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]

    new_file = not os.path.exists(args.out)
    outf = open(args.out, "a", newline="")
    w = csv.DictWriter(outf, fieldnames=FIELDS)
    if new_file:
        w.writeheader()
    spent = 0
    versions = {"httpx": pkg_version("httpx"), "playwright": pkg_version("playwright"), "trafilatura": pkg_version("trafilatura"), "tiktoken": pkg_version("tiktoken"),
                "mode": "direct" if args.direct else "proxy", "watchdog_s": WATCHDOG_S}
    if any(c in ("B", "C") for c in configs):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            versions["chromium"] = b.version
            b.close()

    try:
        for cfg in configs:
            for row in urls:
                rec = run_child(row["url"], row["type"], cfg, args.direct, args.relay or None)
                w.writerow({k: rec.get(k, "") for k in FIELDS})
                outf.flush()
                spent += int(rec.get("bytes", 0) or 0)
                print(f"[{cfg}] {rec.get('status', 0):>3} {int(rec.get('bytes', 0) or 0):>9} B {rec.get('ms', 0):>6} ms {row['type']:<8} {row['url'][:70]} {rec.get('error', '')}", flush=True)
                if spent / 1e6 > args.budget_mb:
                    print(f"budget reached: {spent / 1e6:.1f} MB measured; stopping", file=sys.stderr)
                    raise SystemExit(3)
    finally:
        outf.close()

    all_rows = list(csv.DictReader(open(args.out, newline="")))
    json.dump(summarize(all_rows, versions), open(args.summary, "w"), indent=2)
    print(f"measured {spent / 1e6:.1f} MB this run; summary -> {args.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
