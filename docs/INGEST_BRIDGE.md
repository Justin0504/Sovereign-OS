# Ingest Bridge (industrial-grade)

The **ingest bridge** pulls orders from real sources (Reddit, generic scrapers, retail APIs) and feeds them to Sovereign-OS so you can **really** take orders from the web.

- **Reddit**: PRAW-based; subreddits like `forhire`, `slavelabour`; parses goal and optional $ amount.
- **Scraper**: Any URL returning JSON or HTML; CSS selectors or JSON path for goal/amount.
- **Retail**: Shopify or WooCommerce; map new orders to goals (e.g. “Order #123: …”).

Two modes:

1. **Serve mode** (default): Bridge runs an HTTP server. You set `SOVEREIGN_INGEST_URL` to the bridge’s `/jobs` endpoint. Sovereign-OS polls it and enqueues jobs.
2. **Post mode**: Bridge has no server; it periodically fetches from sources and POSTs each new job to `SOVEREIGN_OS_URL/api/jobs`.

---

## Quick start

### 1. Install bridge dependencies (optional but recommended)

```bash
pip install praw requests beautifulsoup4
```

Reddit needs `praw`; scraper needs `requests` and `beautifulsoup4` for HTML. Retail uses `requests` only.

### 2. Configure (env or YAML)

Minimal **serve** mode (no sources – for testing with static JSON):

```bash
export BRIDGE_MODE=serve
export BRIDGE_PORT=9000
python -m sovereign_os.ingest_bridge
```

Then set Sovereign-OS:

```bash
export SOVEREIGN_INGEST_URL=http://localhost:9000/jobs?take=true
```

Use `?take=true` so each poll consumes the buffer and jobs are not re-sent.

**Reddit** (serve mode):

```bash
export BRIDGE_MODE=serve
export BRIDGE_PORT=9000
export BRIDGE_REDDIT_ENABLED=true
export REDDIT_CLIENT_ID=your_client_id
export REDDIT_CLIENT_SECRET=your_client_secret
export REDDIT_USER_AGENT="SovereignOS-Bridge/1.0"
export REDDIT_SUBREDDITS=forhire,slavelabour
export BRIDGE_POLL_INTERVAL_SEC=60
export BRIDGE_DEDUP_WINDOW_SEC=3600
python -m sovereign_os.ingest_bridge
```

**Post mode** (bridge POSTs directly to Sovereign-OS):

```bash
export BRIDGE_MODE=post
export SOVEREIGN_OS_URL=http://localhost:8000
export SOVEREIGN_OS_API_KEY=your_key_if_required
export BRIDGE_REDDIT_ENABLED=true
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
python -m sovereign_os.ingest_bridge
```

---

## Environment reference

| Variable | Description | Default |
|----------|-------------|--------|
| `BRIDGE_MODE` | `serve` or `post` | `serve` |
| `BRIDGE_HOST` | Bind host (serve mode) | `0.0.0.0` |
| `BRIDGE_PORT` | Bind port (serve mode) | `9000` |
| `SOVEREIGN_OS_URL` | Sovereign-OS base URL (post mode) | `http://localhost:8000` |
| `SOVEREIGN_OS_API_KEY` | API key for POST /api/jobs | (none) |
| `BRIDGE_POLL_INTERVAL_SEC` | How often to fetch from sources | `60` |
| `BRIDGE_DEDUP_WINDOW_SEC` | Don’t re-emit same source_id within this many seconds | `3600` |
| `BRIDGE_CONFIG_PATH` | Optional YAML overlay path | (none) |

### Reddit

| Variable | Description |
|----------|-------------|
| `BRIDGE_REDDIT_ENABLED` | `true` to enable |
| `REDDIT_CLIENT_ID` | Reddit app client ID |
| `REDDIT_CLIENT_SECRET` | Reddit app client secret |
| `REDDIT_USER_AGENT` | User-Agent string |
| `REDDIT_SUBREDDITS` | Comma-separated, e.g. `forhire,slavelabour` |
| `REDDIT_LIMIT_PER_SUB` | New posts per subreddit per run | `25` |
| `REDDIT_MIN_SCORE` | Skip posts with score below this | `0` |
| `REDDIT_KEYWORDS_REQUIRED` | Comma-separated; post must contain one | (any) |

Create a Reddit “script” app at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) to get client ID and secret. **Full Reddit setup (bridge + delivery + posting on r/forhire):** [REDDIT_SETUP_GUIDE.md](REDDIT_SETUP_GUIDE.md).

### Scraper

| Variable | Description |
|----------|-------------|
| `BRIDGE_SCRAPER_ENABLED` | `true` to enable |
| `BRIDGE_SCRAPER_URL` | URL to fetch (JSON or HTML) |
| `BRIDGE_SCRAPER_SELECTOR_GOAL` | CSS selector for goal (HTML) or leave empty for JSON |
| `BRIDGE_SCRAPER_SELECTOR_AMOUNT` | CSS selector for amount (HTML) |
| `BRIDGE_SCRAPER_SELECTOR_ID` | CSS selector for unique id (HTML) |

If the URL returns **JSON**, the bridge expects an array (or `{ "jobs": [...] }`) of objects with `goal` or `title`/`description`, and optional `amount_cents` or `amount`.

### Retail (Shopify / WooCommerce)

| Variable | Description |
|----------|-------------|
| `BRIDGE_RETAIL_ENABLED` | `true` to enable |
| `BRIDGE_RETAIL_PROVIDER` | `shopify` or `woocommerce` |
| `BRIDGE_RETAIL_API_URL` | Shopify: `https://store.myshopify.com/admin/api/2024-01`; Woo: store REST URL |
| `BRIDGE_RETAIL_API_KEY` | Shopify: Admin API access token; Woo: `consumer_key:consumer_secret` |
| `BRIDGE_RETAIL_STORE_DOMAIN` | Optional store domain |

---

## Endpoints (serve mode)

- **GET /jobs?take=true**  
  Returns a JSON array of `{ goal, amount_cents, currency, charter }`.  
  If `take=true`, returns the current buffer and clears it (recommended for `SOVEREIGN_INGEST_URL` so each job is only sent once).

- **GET /health**  
  Returns `{ "status": "ok", "service": "ingest_bridge" }`.

---

## Wiring to Sovereign-OS

1. Start the bridge (e.g. `python -m sovereign_os.ingest_bridge`).
2. Start Sovereign-OS with:
   - `SOVEREIGN_INGEST_URL=http://<bridge_host>:9000/jobs?take=true`
   - `SOVEREIGN_INGEST_INTERVAL_SEC=60` (or your choice)
   - `SOVEREIGN_AUTO_APPROVE_JOBS=true` for full auto flow.

Jobs from Reddit/scraper/retail will then appear in the Job queue and run through CEO/CFO/permissions/delivery/Stripe as in [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

---

## Compliance and safety

- **Reddit**: Respect [Reddit API Terms](https://www.redditinc.com/policies/data-api-terms) and rate limits; use a proper user agent.
- **Scraping**: Respect robots.txt and site ToS; prefer official APIs when available.
- **Retail**: Use read-only or order-read scopes where possible; keep API keys in env only.
