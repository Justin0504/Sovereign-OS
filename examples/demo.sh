#!/usr/bin/env bash
# One-command demo: run a mission with ledger and audit trail.
# Run from project root: ./examples/demo.sh  or  cd examples && ./demo.sh

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${ROOT}/data"
mkdir -p "$DATA"

echo "Running Sovereign-OS demo: mission with ledger + audit trail..."
echo
command -v sovereign >/dev/null 2>&1 || pip install -e . -q
sovereign run --charter "${ROOT}/charter.example.yaml" --ledger "${DATA}/ledger.jsonl" --audit-trail "${DATA}/audit.jsonl" "Summarize the market in one paragraph."
echo
echo "Ledger: ${DATA}/ledger.jsonl"
echo "Audit trail: ${DATA}/audit.jsonl"
echo "Inspect: cat ${DATA}/audit.jsonl"
