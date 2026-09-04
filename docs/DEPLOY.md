# Deploying Sovereign-OS

Sovereign-OS is **local-first and self-hosted**. Each operator runs their own instance
on their own machine or cloud, with their own keys — nothing is sent to a central
service, and no one else custodies your keys or funds. There are three things you might
deploy, independent of each other:

1. **The workspace** — the governed agent app you actually use (web console + TUI).
2. **The marketing site** — the static landing page in [`site/`](../site), for a domain.
3. **A shared team instance** — optional multi-tenant self-host for an org (see below).

---

## 1 · Run the workspace (self-host)

Your keys stay in your environment. Set the LLM key you want the agents to run on, then
start the app.

### Docker (recommended)

```bash
git clone https://github.com/Justin0504/Sovereign-OS
cd Sovereign-OS
export ANTHROPIC_API_KEY=sk-ant-…        # or OPENAI_API_KEY
docker compose up -d redis web            # web console on :8000
# open http://localhost:8000
```

The `web` service persists its ledger and vector store in the `sovereign_data` volume,
so audit history survives restarts. For the terminal UI instead: `docker compose run --rm -it app`.

### Without Docker

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-…
python -m sovereign_os.web.app 8000        # web console
# or:  python -m sovereign_os.ui.app        # terminal UI
```

### Deploying the workspace to a server

The same container runs anywhere Docker does (a VPS, Fly.io, Render, a home box). Put it
behind your own reverse proxy / TLS and keep it on a private network or behind auth — the
single-tenant console assumes a trusted operator. Persist `/app/data` (the ledger + audit
trail) on a real volume. Never bake keys into the image; pass them as runtime env vars or
secrets.

---

## 2 · Publish the marketing site to a domain

The landing page is a single static file — [`site/index.html`](../site/index.html) — with
no build step. Host it anywhere static.

### GitHub Pages (with the included workflow)

1. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. Push to `main`. [`.github/workflows/pages.yml`](../.github/workflows/pages.yml)
   publishes `site/` automatically.
3. **Custom domain:** buy the domain, then in **Settings → Pages → Custom domain** enter
   it (e.g. `sovereign-os.dev`). GitHub commits a `site/CNAME` and provisions TLS. At your
   registrar, point the domain at GitHub Pages:
   - apex (`example.com`): `A` records → `185.199.108.153`, `.109.153`, `.110.153`, `.111.153`
   - `www` (or any subdomain): `CNAME` → `<your-user>.github.io`

### Netlify / Vercel / Cloudflare Pages

Point the project at this repo with **publish directory = `site`** and **no build command**,
then add your domain in their dashboard. Same static file, same result.

---

## 3 · Optional: a shared team instance (multi-tenant self-host)

If a team wants one instance they run for their own seats — each workspace isolated, each
bringing its own key — the multi-tenant control plane in [`sovereign_os/saas/`](../sovereign_os/saas)
provides it. This is *self-hosted by the team*, not a service anyone else operates for you;
the platform still never holds funds or custodies keys on a tenant's behalf. See
[SAAS.md](SAAS.md) for the tenant model, isolation, and API.

```bash
export SOVEREIGN_SAAS_ROOT=/var/lib/sovereign-os/tenants   # isolated per-tenant data
python -m sovereign_os.saas.api                            # console + API on :8020
```

Run it only on a network you control, behind your own auth/TLS. Each tenant supplies its
own LLM (and, if settling, its own Stripe/wallet) key; those are stored per-workspace and
used only for that tenant's missions.

---

## Notes

- **Keys** are read from the environment (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) or, in
  team mode, from per-tenant config — never committed, never sent anywhere but the model
  provider you chose.
- **Data** (ledger, audit trail, trust store) is local to your instance. Back up the data
  volume if the audit history matters to you.
- **Experimental.** Keep a human in the loop for financial and production decisions.
