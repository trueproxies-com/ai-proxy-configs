# ai-proxy-configs

Scripts, configurations and raw results behind the TrueProxies pages on proxies for AI agents:

- https://trueproxies.com/use-cases/ai-proxies/ (bytes per page by workload, fleet sizing, sessions)
- https://trueproxies.com/use-cases/ai-training-data/ (cost per million tokens)
- https://trueproxies.com/integrations/ (Crawl4AI, self-hosted Firecrawl, Browser Use, tested through the gateway)

Everything here was measured on 2026-10-01 through the TrueProxies residential gateway with the tool versions named in `data/`. Fork it, rerun it against your own targets, or open an issue if a number looks wrong.

## What was measured

50 public pages, 10 each of news, documentation, product, blog and forum, one per registrable domain, each allowed for generic crawlers by its robots.txt (`data/urls.csv`, selected by `scripts/select_urls.py` from `data/candidates.csv`). Each page was loaded three ways through the gateway:

| Config | Client | Bytes counted |
|---|---|---|
| A | `httpx`, HTML only, `Accept-Encoding: gzip, br` | `response.num_bytes_downloaded` |
| B | Playwright Chromium, images, fonts and media aborted | sum of CDP `Network.loadingFinished.encodedDataLength`, cache disabled |
| C | Playwright Chromium, full render | same |

Load rule: wait for `load`, then up to 3 s of network idle, 30 s cap. A load that hits the cap with the document in hand is kept as `partial`; a hung browser is killed by a 90 s watchdog. Tokens: main text extracted with trafilatura, counted with tiktoken `o200k_base`. Peak concurrent tunnels per page load: configs B and C over 10 URLs through `scripts/relay.py`, a local counting CONNECT relay. Billed bytes: the gateway's own usage counters before and after a run (`data/usage_delta.json` for the whole run, `data/usage_factor.json` for a dedicated config C pass over 20 URLs).

## Results (medians, `data/tables.md`)

| Workload | MB per page, client | MB per page, billed (× 1.381) | ok / 50 |
|---|---|---|---|
| HTTP fetch, HTML only | 0.052 | 0.072 | 47 |
| Browser, images/fonts/media blocked | 0.677 | 0.934 | 41 (9 partial) |
| Browser, full render | 1.654 | 2.284 | 39 (14 partial) |

990 tokens of main text per page at the median. Full render moved 31.7× the bytes of an HTML fetch; blocking assets saved 59%. Peak concurrent tunnels on one page load: 22 (full render), 26 (assets blocked); medians 12 and 6. The gateway counted 1.381× the bytes the browser's own network accounting reported, over the factor pass.

Direct-connection baseline on the same URLs, for comparison only: `data/direct_results.csv`, `data/direct_summary.json`.

Tool runs (`data/tools/`): Crawl4AI 0.9.4 (rotating 15/20, one session per crawl 14/20; failures were 30 s navigation timeouts and one 503), Browser Use 0.13.10 (browser layer, 5/5, five distinct exits for five session IDs), self-hosted Firecrawl v2.11.444 (20/20 `/v1/scrape`).

## Run it yourself

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "httpx[http2,brotli]" playwright trafilatura tiktoken && playwright install chromium
cd scripts

# credentials stay in the environment; the username carries targeting and sessions
export TP_PROXY_URL='http://USER:PASS@YOUR_GATEWAY_HOST:8080'

python select_urls.py                               # robots + status checks -> urls.csv
python measure.py --urls ../data/urls.csv --configs A,B,C --out results.csv --summary summary.json
python relay.py --port 18750 & python measure.py --urls ../data/urls.csv --configs B,C --limit 10 --relay 127.0.0.1:18750 --out results_relay.csv --summary summary_relay.json; kill %1
python compute_tables.py --summary summary.json --results results.csv --relay summary_relay.json --usage usage_delta.json
```

`measure.py --direct` runs without a proxy. `usage_delta.py before|after` reads billed bytes through the TrueProxies API with `TP_API_KEY` and `TP_SERVICE_ID`. The tool runners (`tool_crawl4ai.py`, `tool_browser_use.py`, `tool_firecrawl.py`) take the same `TP_PROXY_URL`; the Firecrawl one expects a self-hosted stack on `http://127.0.0.1:3002` started with `PROXY_SERVER`, `PROXY_USERNAME`, `PROXY_PASSWORD` and `USE_DB_AUTHENTICATION=false` in its `.env`.

Scripts never print credentials; `measure.py` scrubs the proxy username and password from error text.

## Method notes and limits

- CDP `encodedDataLength` is on-the-wire size per request for the page's main frame and same-process frames; out-of-process iframes are not counted. That is one reason the gateway counts more than the browser does.
- One request per URL per configuration, fresh browser context per URL, cache disabled. Pages change by the hour; rerun before you quote a number.
- Through a residential exit, heavy pages reach `load` later than from a datacenter. Timeouts in the tool runs were of that kind, not proxy refusals.
- Sample size is 50 pages. The medians are planning figures, not guarantees.

## License

MIT. See `LICENSE`.
