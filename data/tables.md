Measured 2026-10-01 through the TrueProxies residential gateway (proxy); n per config: A 50 (ok 47), B 50 (ok 41, 9 partial at the 30 s cap), C 50 (ok 39, 14 partial). Billed factor 1.381 (gateway counted 42518968 bytes against 30791318 client-measured bytes over the config C factor pass (20 URLs)).

## Hub: cost per 1,000 page loads

| Workload | MB per page | 1 GB tier | 100 GB tier | 1,000 GB tier |
|---|---|---|---|---|
| HTTP fetch, HTML only | 0.072 | $0.12 | $0.07 | $0.06 |
| Browser, images/fonts/media blocked | 0.934 | $1.53 | $0.91 | $0.76 |
| Browser, full render | 2.284 | $3.75 | $2.23 | $1.85 |

## Training data: cost per million tokens

Median 990 tokens per page (tiktoken o200k_base on trafilatura main text).

| Pipeline | GB per 1M tokens | 1 GB tier | 100 GB tier | 1,000 GB tier |
|---|---|---|---|---|
| HTTP fetch, HTML only | 0.073 | $0.12 | $0.07 | $0.06 |
| Browser, assets blocked | 0.943 | $1.55 | $0.92 | $0.76 |
| Browser, full render | 2.307 | $3.78 | $2.25 | $1.87 |

Full render is 31.7x an HTML fetch; blocking images, fonts and media saves 59%.

Peak concurrent tunnels per page load: full render 22, assets blocked 26 (10-URL subset).
