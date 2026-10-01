#!/usr/bin/env python3
"""Turn summary.json (+ usage_delta.json, summary_relay.json) into the plan's
MEASURE values and the two page tables .

  compute_tables.py [--summary summary.json] [--relay summary_relay.json]
                    [--usage usage_delta.json] [--out tables.md] [--keys measure_keys.json]

Prices are the Traffic pack display tiers. Rounding: 2 decimals, half up.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from decimal import ROUND_HALF_UP, Decimal

TIERS = [("1 GB tier", Decimal("1.64")), ("100 GB tier", Decimal("0.9746")), ("1,000 GB tier", Decimal("0.8105"))]
CONCURRENCY_RESIDENTIAL = 3000


def money(x: Decimal) -> str:
    return "$" + str(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default="summary.json")
    ap.add_argument("--results", default="results.csv")
    ap.add_argument("--relay", default="summary_relay.json")
    ap.add_argument("--usage", default="usage_delta.json")
    ap.add_argument("--out", default="tables.md")
    ap.add_argument("--keys", default="measure_keys.json")
    args = ap.parse_args()

    s = json.load(open(args.summary))
    cfg = s["configs"]
    # Billed factor: gateway bytes over the whole run ÷ client-measured bytes over the same run.
    factor = Decimal(1)
    factor_note = "not measured; client-side bytes used as-is"
    # Prefer the dedicated factor pass (config C, 20 URLs, its own usage window); fall back to the whole run.
    pairs = [("usage_factor.json", "results_factor.csv", "the config C factor pass (20 URLs)"), (args.usage, args.results, "the whole run")]
    for ufile, rfile, label in pairs:
        if os.path.exists(ufile) and os.path.exists(rfile):
            u = json.load(open(ufile))
            client_total = sum(int(r["bytes"]) for r in csv.DictReader(open(rfile)))
            if u.get("delta", 0) > 0 and client_total > 0:
                factor = (Decimal(u["delta"]) / Decimal(client_total)).quantize(Decimal("0.001"))
                factor_note = f"gateway counted {u['delta']} bytes against {client_total} client-measured bytes over {label}"
                break
    mb = {k: (Decimal(cfg[k]["median_bytes"]) / Decimal(1_000_000) * factor).quantize(Decimal("0.001")) for k in ("A", "B", "C")}
    tokens = Decimal(cfg["A"]["median_tokens"])
    gb_per_1m = {k: (Decimal(1_000_000) / tokens * mb[k] / Decimal(1000)).quantize(Decimal("0.001")) for k in mb}
    spread = (mb["C"] / mb["A"]).quantize(Decimal("0.1"))
    savings = ((mb["C"] - mb["B"]) / mb["C"] * 100).quantize(Decimal("1"))

    relay = json.load(open(args.relay)) if os.path.exists(args.relay) else None
    peak = {}
    if relay:
        for k in ("B", "C"):
            peak[k] = relay["configs"].get(k, {}).get("peak_conns_max")

    labels = {"A": "HTTP fetch, HTML only", "B": "Browser, images/fonts/media blocked", "C": "Browser, full render"}
    tlabels = {"A": "HTTP fetch, HTML only", "B": "Browser, assets blocked", "C": "Browser, full render"}
    lines = [f"Measured {s['test_date']} through the TrueProxies residential gateway ({s['versions'].get('mode')}); "
             f"n per config: A {cfg['A']['n']} (ok {cfg['A']['ok']}), B {cfg['B']['n']} (ok {cfg['B']['ok']}, {cfg['B'].get('partial', 0)} partial at the 30 s cap), C {cfg['C']['n']} (ok {cfg['C']['ok']}, {cfg['C'].get('partial', 0)} partial). "
             f"Billed factor {factor} ({factor_note}).", "",
             "## Hub: cost per 1,000 page loads", "",
             "| Workload | MB per page | " + " | ".join(t for t, _ in TIERS) + " |", "|---|---|" + "---|" * len(TIERS)]
    for k in ("A", "B", "C"):
        lines.append(f"| {labels[k]} | {mb[k]} | " + " | ".join(money(mb[k] * p) for _, p in TIERS) + " |")
    lines += ["", "## Training data: cost per million tokens", "",
              f"Median {int(tokens)} tokens per page (tiktoken o200k_base on trafilatura main text).", "",
              "| Pipeline | GB per 1M tokens | " + " | ".join(t for t, _ in TIERS) + " |", "|---|---|" + "---|" * len(TIERS)]
    for k in ("A", "B", "C"):
        lines.append(f"| {tlabels[k]} | {gb_per_1m[k]} | " + " | ".join(money(gb_per_1m[k] * p) for _, p in TIERS) + " |")
    lines += ["", f"Full render is {spread}x an HTML fetch; blocking images, fonts and media saves {savings}%."]
    if peak:
        lines += ["", f"Peak concurrent tunnels per page load: full render {peak.get('C')}, assets blocked {peak.get('B')} (10-URL subset)."]
    open(args.out, "w").write("\n".join(lines) + "\n")

    keys = {
        "TEST_DATE": s["test_date"], "SAMPLE_SIZE": cfg["A"]["n"],
        "VERSION_PLAYWRIGHT": s["versions"].get("playwright"), "CHROMIUM": s["versions"].get("chromium"),
        "TOKENS_PER_PAGE": int(tokens), "MB_HTML": str(mb["A"]), "MB_BLOCKED": str(mb["B"]), "MB_FULL": str(mb["C"]),
        "BILLED_FACTOR": str(factor), "SPREAD_FULL_VS_HTML": str(spread), "BLOCKED_SAVINGS_PCT": str(savings),
        "COST_1M_HTML_1000GB": money(gb_per_1m["A"] * TIERS[2][1]), "COST_1M_FULL_1000GB": money(gb_per_1m["C"] * TIERS[2][1]),
        "PEAK_CONN_FULL": peak.get("C"), "PEAK_CONN_BLOCKED": peak.get("B"),
        "AGENTS_PER_ACCOUNT_FULL": CONCURRENCY_RESIDENTIAL // peak["C"] if peak.get("C") else None,
        "AGENTS_PER_ACCOUNT_BLOCKED": CONCURRENCY_RESIDENTIAL // peak["B"] if peak.get("B") else None,
        "SUCCESS": {k: cfg[k]["success_rate"] for k in cfg}, "PARTIAL": {k: cfg[k].get("partial") for k in cfg}, "MEDIAN_MS": {k: cfg[k]["median_ms"] for k in cfg},
        "ERRORS": {k: cfg[k]["errors"] for k in cfg},
    }
    json.dump(keys, open(args.keys, "w"), indent=2)
    print(open(args.out).read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
