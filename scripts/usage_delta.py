#!/usr/bin/env python3
"""Read billed bytes for one service before and after a run .

GET https://api.trueproxies.com/v1/services/{id}/usage returns bytes_in
(customer upload) and bytes_out (customer download) from the node counters
billing uses. Needs TP_API_KEY (bearer) and TP_SERVICE_ID in the environment.

  usage_delta.py before   -> writes usage_before.json
  usage_delta.py after    -> writes usage_after.json and prints the delta

The billed-overhead factor is delta_bytes / client-measured bytes for the same
window (summary.json, config C subset).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import httpx

API = os.environ.get("TP_API_BASE", "https://api.trueproxies.com")


def read_usage() -> dict:
    key = os.environ.get("TP_API_KEY")
    sid = os.environ.get("TP_SERVICE_ID")
    if not key or not sid:
        print("TP_API_KEY and TP_SERVICE_ID must be set", file=sys.stderr)
        sys.exit(2)
    r = httpx.get(f"{API}/v1/services/{sid}/usage", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=20.0)
    r.raise_for_status()
    u = r.json()
    return {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "bytes_in": int(u.get("bytes_in", 0)), "bytes_out": int(u.get("bytes_out", 0)),
            "usage_source": u.get("usage_source"), "last_updated_at": u.get("last_updated_at")}


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    if stage not in ("before", "after"):
        print("usage: usage_delta.py before|after", file=sys.stderr)
        return 2
    snap = read_usage()
    json.dump(snap, open(f"usage_{stage}.json", "w"), indent=2)
    print(json.dumps(snap))
    if stage == "after" and os.path.exists("usage_before.json"):
        before = json.load(open("usage_before.json"))
        delta = (snap["bytes_in"] - before["bytes_in"]) + (snap["bytes_out"] - before["bytes_out"])
        print(f"billed delta: {delta} bytes ({delta / 1e6:.2f} MB) between {before['at']} and {snap['at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
