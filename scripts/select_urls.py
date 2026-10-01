#!/usr/bin/env python3
"""Build urls.csv for the AI proxies bandwidth study .

Reads candidates.csv (url,type,pattern). A row with a pattern is a seed page:
the first same-site link matching the pattern that passes the checks is kept.
A row without a pattern is checked as-is.

Checks, all on a direct connection (no proxy):
  * robots.txt allows the path for a generic agent ("*")
  * GET returns 200 with an HTML body of at least MIN_HTML_BYTES
  * one URL per registrable domain, at most PER_TYPE per content type

Writes urls.csv (url,type,domain,robots_ok,checked_at) and select_log.txt.
"""
from __future__ import annotations

import csv
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

PER_TYPE = 10
MIN_HTML_BYTES = 8_000
MAX_LINKS_TRIED = 8
TIMEOUT = 20.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}

_robots: dict[str, robotparser.RobotFileParser | None] = {}


def registrable(host: str) -> str:
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def robots_ok(client: httpx.Client, url: str) -> bool:
    s = urlsplit(url)
    origin = f"{s.scheme}://{s.netloc}"
    if origin not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = client.get(origin + "/robots.txt")
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif r.status_code >= 500:
                rp = None  # unreachable robots: treat as disallowed, be conservative
            else:
                rp.parse([])  # 4xx: no robots, everything allowed
        except httpx.HTTPError:
            rp = None
        _robots[origin] = rp
    rp = _robots[origin]
    if rp is None:
        return False
    return rp.can_fetch("*", url)


def fetch_html(client: httpx.Client, url: str) -> tuple[int, str, str]:
    r = client.get(url)
    ctype = r.headers.get("content-type", "")
    return r.status_code, ctype, r.text if "html" in ctype else ""


def discover(client: httpx.Client, seed: str, pattern: str, log) -> list[str]:
    status, ctype, html = fetch_html(client, seed)
    if status != 200 or not html:
        log(f"seed {seed}: status {status} {ctype}")
        return []
    rx = re.compile(pattern)
    seed_dom = registrable(urlsplit(seed).netloc)
    out: list[str] = []
    for m in re.finditer(r'href=["\']([^"\'#?]+)(?:[?#][^"\']*)?["\']', html):
        href = unescape(m.group(1))
        absu = urljoin(seed, href)
        s = urlsplit(absu)
        if s.scheme not in ("http", "https") or registrable(s.netloc) != seed_dom:
            continue
        if not rx.search(absu):
            continue
        if absu not in out:
            out.append(absu)
        if len(out) >= MAX_LINKS_TRIED:
            break
    return out


def main() -> int:
    rows = list(csv.DictReader(open("candidates.csv", newline="")))
    kept: list[dict] = []
    per_type: dict[str, int] = {}
    seen_domains: set[str] = set()
    logf = open("select_log.txt", "w")

    def log(msg: str) -> None:
        print(msg)
        logf.write(msg + "\n")

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=TIMEOUT, http2=True) as client:
        for row in rows:
            ctype = row["type"].strip()
            if per_type.get(ctype, 0) >= PER_TYPE:
                continue
            seed = row["url"].strip()
            pattern = (row.get("pattern") or "").strip()
            dom = registrable(urlsplit(seed).netloc)
            if dom in seen_domains:
                log(f"skip {seed}: domain already used")
                continue
            candidates = [seed]
            if pattern:
                try:
                    candidates = discover(client, seed, pattern, log)
                except httpx.HTTPError as e:
                    log(f"seed {seed}: {e.__class__.__name__}")
                    candidates = []
            chosen = None
            for url in candidates:
                try:
                    if not robots_ok(client, url):
                        log(f"reject {url}: robots disallows '*'")
                        continue
                    status, ct, html = fetch_html(client, url)
                except httpx.HTTPError as e:
                    log(f"reject {url}: {e.__class__.__name__}")
                    continue
                if status != 200 or len(html.encode()) < MIN_HTML_BYTES:
                    log(f"reject {url}: status {status}, {len(html.encode())} bytes")
                    continue
                chosen = url
                break
            if chosen:
                seen_domains.add(dom)
                per_type[ctype] = per_type.get(ctype, 0) + 1
                kept.append({
                    "url": chosen, "type": ctype, "domain": dom, "robots_ok": "true",
                    "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                log(f"keep [{ctype} {per_type[ctype]}/{PER_TYPE}] {chosen}")
            time.sleep(0.5)

    with open("urls.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["url", "type", "domain", "robots_ok", "checked_at"])
        w.writeheader()
        w.writerows(kept)
    log(f"total kept: {len(kept)} " + ", ".join(f"{k}={v}" for k, v in sorted(per_type.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
