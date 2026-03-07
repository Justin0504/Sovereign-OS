#!/usr/bin/env bash
# Full demo: auto ingest from "web" -> CEO/CFO -> permissions (firewall) -> delivery -> Stripe
# 1. Starts a local HTTP server (port 8888) serving examples/ so ingest can "pull orders from the web"
# 2. Sets env for ingest URL, auto-approve, and persistence
# 3. Runs the Web app in the foreground
# 4. Open http://localhost:8000 and watch: Job queue, Decision stream (CEO/CFO), Trust, Balance, webhook

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
EXAMPLES_DIR="$ROOT/examples"
PORT=8888
INGEST_URL="http://localhost:${PORT}/ingest_demo_orders.json"

# Start HTTP server in background
echo "Starting local order server at http://localhost:$PORT (serving examples/)..."
python3 -m http.server "$PORT" --directory "$EXAMPLES_DIR" &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null || true" EXIT
sleep 2

# Env for full auto flow
export SOVEREIGN_INGEST_URL="$INGEST_URL"
export SOVEREIGN_INGEST_INTERVAL_SEC=15
export SOVEREIGN_AUTO_APPROVE_JOBS=true
export SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true
export SOVEREIGN_JOB_DB="$ROOT/data/jobs.db"
export SOVEREIGN_LEDGER_PATH="$ROOT/data/ledger.jsonl"
mkdir -p "$ROOT/data"

echo ""
echo "Demo flow (real logic):"
echo "  1. Ingest: poller fetches $INGEST_URL every 15s -> jobs enqueued"
echo "  2. CEO: Strategist splits each job into tasks (see Decision stream)"
echo "  3. CFO: Treasury approves budget (see Decision stream)"
echo "  4. Firewall: SovereignAuth checks TrustScore vs capability (see Trust in UI)"
echo "  5. Delivery: on completion, POST to SOVEREIGN_WEBHOOK_URL if set"
echo "  6. Stripe: charge amount_cents; Ledger gets job_income"
echo ""
echo "Starting Web UI. Open http://localhost:8000 and watch Job queue + Decision stream."
echo "To stop: Ctrl+C"
echo ""

python3 -m sovereign_os.web.app
