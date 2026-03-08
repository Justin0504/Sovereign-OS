# Reddit Ingest + Delivery: Step-by-Step Setup

This guide walks you through **getting Reddit API credentials**, **running the Ingest Bridge** (crawl r/forhire etc.), **running Sovereign-OS with Reddit delivery** (reply on the post with the result), and **posting a task on Reddit** so the full flow works.

---

## Part 1: Get Reddit API credentials

You need a **Reddit application** so the bridge can read posts and (optionally) the same or another app so Sovereign-OS can post the delivery comment.

### 1.1 Create a Reddit “script” app (read + write)

1. Log in to Reddit, then open: **https://www.reddit.com/prefs/apps**
2. Scroll down and click **“create another app…”** or **“create application”**.
3. Fill in:
   - **name**: e.g. `SovereignOS-Bridge`
   - **type**: choose **“script”** (so you can use username + password for posting later).
   - **description**: optional (e.g. “Ingest jobs from forhire and post delivery comments”).
   - **redirect uri**: for script apps Reddit often requires a placeholder; use `http://localhost:8080` or `https://localhost:8080`.
4. Click **“create app”**.
5. Under the app you’ll see:
   - **Personal use script** (or “client id”): a short string under the app name. This is `REDDIT_CLIENT_ID`.
   - **Secret**: click “secret” to reveal. This is `REDDIT_CLIENT_SECRET`.

Write these down; you’ll use them for both the **bridge** (read posts) and **Sovereign-OS** (post delivery comment).

### 1.2 Choose how Sovereign-OS will post (comment on the post)

To post the delivery comment, Sovereign-OS needs to act as a Reddit user. Two options:

| Option | Env vars | Use when |
|--------|----------|----------|
| **A. Username + password (script app)** | `REDDIT_USERNAME`, `REDDIT_PASSWORD` | Same Reddit account that owns the “script” app; quick to set up. |
| **B. OAuth2 refresh token** | `REDDIT_REFRESH_TOKEN` | You’ve already done the OAuth flow and saved the refresh token (better for automation/long-lived). |

For a first run, **Option A** is simpler: use the Reddit account that created the app and set `REDDIT_USERNAME` and `REDDIT_PASSWORD` (your real Reddit login).  
**Security**: Prefer a **dedicated Reddit account** for the bot (e.g. “YourBotName”) and create the script app with that account, so you don’t put your main account password in env.

---

## Part 2: Run the Ingest Bridge (Reddit → jobs)

The bridge **polls Reddit** (e.g. r/forhire, r/HireaWriter), turns each matching post into a job (goal + amount + `delivery_contact`), and either **serves** them to Sovereign-OS (Sovereign-OS polls the bridge) or **POSTs** them to Sovereign-OS.

### 2.1 Install bridge dependencies

```bash
cd /path/to/Sovereign-OS
pip install praw requests beautifulsoup4
```

### 2.2 Set environment variables for the bridge

**All of these** (replace with your values):

```bash
# ----- Reddit (bridge reads from these subreddits) -----
export BRIDGE_REDDIT_ENABLED=true
export REDDIT_CLIENT_ID=your_personal_use_script_id
export REDDIT_CLIENT_SECRET=your_secret
export REDDIT_USER_AGENT="SovereignOS-Bridge/1.0"
export REDDIT_SUBREDDITS=forhire,HireaWriter

# ----- Mode: "serve" or "post" -----
# Serve: bridge runs HTTP server; Sovereign-OS will poll it.
export BRIDGE_MODE=serve
export BRIDGE_PORT=9000
export BRIDGE_POLL_INTERVAL_SEC=60
export BRIDGE_DEDUP_WINDOW_SEC=3600
```

Optional:

- `REDDIT_LIMIT_PER_SUB=25` (default) — how many new posts per subreddit per run.
- `REDDIT_MIN_SCORE=0` — skip posts with score below this.
- `REDDIT_KEYWORDS_REQUIRED=hiring,$` — only posts containing one of these (comma-separated); leave unset to allow any.

### 2.3 If you use **post** mode instead (bridge POSTs to Sovereign-OS)

```bash
export BRIDGE_MODE=post
export SOVEREIGN_OS_URL=http://localhost:8000
# If your Sovereign-OS requires an API key:
# export SOVEREIGN_OS_API_KEY=your_api_key
```

Then start Sovereign-OS **first** (see Part 3), and keep it running so the bridge can POST to `http://localhost:8000/api/jobs`.

### 2.4 Start the bridge

```bash
python -m sovereign_os.ingest_bridge
```

You should see logs like “Bridge runner started” and, when Reddit is enabled, “Reddit source: yielded N orders” (or similar). Leave this terminal open.

**Serve mode summary**: Bridge is now serving at `http://localhost:9000`. Next, Sovereign-OS will poll `http://localhost:9000/jobs?take=true` to pull jobs (with `delivery_contact` when they come from Reddit).

---

## Part 3: Run Sovereign-OS (with ingest + Reddit delivery)

Sovereign-OS needs to: (1) **pull jobs** from the bridge (if you use serve mode) or receive them via POST (if you use post mode); (2) **post the delivery comment** on Reddit when a job with `delivery_contact.platform = "reddit"` completes.

### 3.1 Set environment variables for Sovereign-OS

**Ingest (only if bridge is in serve mode):**

```bash
export SOVEREIGN_INGEST_URL=http://localhost:9000/jobs?take=true
export SOVEREIGN_INGEST_INTERVAL_SEC=60
export SOVEREIGN_INGEST_DEDUP_SEC=300
```

Use `?take=true` so each poll consumes the buffer and the same job isn’t enqueued twice.

**Reddit delivery (so Sovereign-OS can comment on the post):**

```bash
export REDDIT_CLIENT_ID=your_personal_use_script_id
export REDDIT_CLIENT_SECRET=your_secret
export REDDIT_USER_AGENT="SovereignOS-Bridge/1.0"
# Option A: script app (same account that owns the app)
export REDDIT_USERNAME=your_reddit_username
export REDDIT_PASSWORD=your_reddit_password
# Option B (optional): if you have a refresh token instead, use it and omit USERNAME/PASSWORD
# export REDDIT_REFRESH_TOKEN=your_refresh_token
```

**Optional but recommended for a smooth demo:**

```bash
export SOVEREIGN_AUTO_APPROVE_JOBS=true
export SOVEREIGN_JOB_DB=./data/jobs.db
export SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl
export OPENAI_API_KEY=sk-...   # or ANTHROPIC_API_KEY
export STRIPE_API_KEY=sk_test_...
```

### 3.2 Start Sovereign-OS

In a **second terminal** (first one is running the bridge):

```bash
cd /path/to/Sovereign-OS
python -m sovereign_os.web.app
```

Open **http://localhost:8000**. You should see the dashboard; after the next ingest poll, jobs from Reddit (if any) will appear in the Job queue. If auto-approve is on, they’ll run and, when they complete, Sovereign-OS will post the result as a **comment on the original Reddit post** (because the job has `delivery_contact.platform = "reddit"`).

---

## Part 4: Post a task on Reddit (so the bridge can pick it up)

To test the full flow, **you** post a “client” task on Reddit using one of the short copy examples from [DEMO_TASK_POSTS.md](DEMO_TASK_POSTS.md). The bridge will crawl it, create a job with `delivery_contact`, and after Sovereign-OS completes the job it will reply on that post.

### 4.1 Go to the subreddit

- **r/forhire**: https://www.reddit.com/r/forhire/
- Or **r/HireaWriter**: https://www.reddit.com/r/HireaWriter/

(Use subreddits you included in `REDDIT_SUBREDDITS`.)

### 4.2 Create a new post

1. Click **“Create Post”** (or “Post”).
2. Choose **“Post”** (text) or **“Link”** as needed; for a text task, **“Post”** is typical.
3. **Title**: use a short, clear title that includes the price so the bridge can parse it (e.g. `[Hiring] $80 — 1000w B2B blog: reduce new-hire ramp-up (HR/ops audience). Deliver: draft + meta + 2 social captions. DM me.`).
4. **Body** (if the subreddit allows body text): you can paste the same line or a slightly longer version from [DEMO_TASK_POSTS.md](DEMO_TASK_POSTS.md).

Example for **Task 1 (Long-form blog, $80)**:

- **Title**: `[Hiring] $80 — 1000w B2B blog: reduce new-hire ramp-up (HR/ops audience). Deliver: draft + meta + 2 social captions. DM me.`

Example for **Task 3 (Cold email sequence, $60)**:

- **Title**: `[Hiring] $60 — 4-email cold sequence (D2C, 10d). Subject lines + timing note. DM.`

5. Add **flair** if the subreddit requires it (e.g. r/forhire often has “Hiring” or “Task”).
6. Click **“Post”** (or “Submit”).

### 4.3 What happens next

1. Within about **1 poll interval** (e.g. 60 seconds), the **bridge** will fetch new posts from r/forhire (and any other subreddits you set). It will see your post, parse goal + amount, and attach `delivery_contact` (your Reddit username, post id, permalink).
2. **Serve mode**: Sovereign-OS will request `http://localhost:9000/jobs?take=true` and receive this job, then enqueue it (and, if auto-approve is on, run it).
3. **Post mode**: The bridge will POST the job to `http://localhost:8000/api/jobs`; Sovereign-OS will enqueue and run it (if auto-approve is on).
4. When the job **completes**, Sovereign-OS will call the Reddit delivery logic: it uses `delivery_contact` to find the post and **post a comment** with the result. You (as the OP) will see that comment under your post — that’s “联系他并交付” (contact the client and deliver).

### 4.4 If the bridge doesn’t pick your post

- Check that **BRIDGE_REDDIT_ENABLED=true** and **REDDIT_SUBREDDITS** includes the subreddit you used (e.g. `forhire`).
- Ensure the post **title or body** contains something the bridge can parse as a goal (it concatenates title + body). Including a **$ amount** (e.g. `$80`) helps the bridge set `amount_cents`.
- Check bridge logs for “Reddit source: yielded N orders” or errors (e.g. Reddit API rate limit or invalid credentials).

### 4.5 If Sovereign-OS doesn’t post the delivery comment

- Ensure **REDDIT_CLIENT_ID**, **REDDIT_CLIENT_SECRET**, **REDDIT_USER_AGENT** are set in the **Sovereign-OS** process (not only in the bridge).
- Ensure **REDDIT_USERNAME** and **REDDIT_PASSWORD** (or **REDDIT_REFRESH_TOKEN**) are set so Sovereign-OS can post.
- Check Sovereign-OS logs for “Reddit delivery: posted comment” or “Reddit delivery failed …”.

---

## Quick reference: env vars by role

| Role | Variables |
|------|-----------|
| **Bridge (Reddit ingest)** | `BRIDGE_REDDIT_ENABLED=true`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `REDDIT_SUBREDDITS=forhire,HireaWriter` |
| **Bridge mode** | `BRIDGE_MODE=serve` or `post`; if serve: `BRIDGE_PORT=9000`; if post: `SOVEREIGN_OS_URL=http://localhost:8000` |
| **Sovereign-OS (ingest URL)** | Only for serve mode: `SOVEREIGN_INGEST_URL=http://localhost:9000/jobs?take=true`, `SOVEREIGN_INGEST_INTERVAL_SEC=60` |
| **Sovereign-OS (Reddit delivery)** | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, and either `REDDIT_USERNAME` + `REDDIT_PASSWORD` or `REDDIT_REFRESH_TOKEN` |

---

## One-page checklist

1. [ ] Create Reddit script app at reddit.com/prefs/apps; note **client id** and **secret**.
2. [ ] Install: `pip install praw requests beautifulsoup4`.
3. [ ] Terminal 1 – Bridge: set `BRIDGE_REDDIT_ENABLED=true`, `REDDIT_*`, `BRIDGE_MODE=serve`, then `python -m sovereign_os.ingest_bridge`.
4. [ ] Terminal 2 – Sovereign-OS: set `SOVEREIGN_INGEST_URL=http://localhost:9000/jobs?take=true`, `REDDIT_*` (including `REDDIT_USERNAME` + `REDDIT_PASSWORD` for posting), then `python -m sovereign_os.web.app`.
5. [ ] Open http://localhost:8000; optionally set `SOVEREIGN_AUTO_APPROVE_JOBS=true` and LLM/Stripe keys.
6. [ ] On Reddit, post in r/forhire (or r/HireaWriter) using a short copy from [DEMO_TASK_POSTS.md](DEMO_TASK_POSTS.md) (e.g. `[Hiring] $80 — 1000w B2B blog …`).
7. [ ] Wait for the next ingest poll; job appears in queue and runs; after completion, check your Reddit post for the delivery comment.
