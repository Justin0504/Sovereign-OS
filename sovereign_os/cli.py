"""
Sovereign-OS CLI: run a one-shot mission from the command line.

  sovereign run --charter path/to/charter.yaml "Your goal here"
  sovereign run -c charter.example.yaml "Summarize the market"
  sovereign --version
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sovereign_os import __version__


def _run_mission(
    charter_path: str,
    goal: str,
    *,
    ledger_path: str | None = None,
    audit_trail_path: str | None = None,
) -> int:
    from sovereign_os import load_charter, UnifiedLedger
    from sovereign_os.agents import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance import GovernanceEngine

    path = Path(charter_path)
    if not path.exists():
        print(f"Error: Charter file not found: {charter_path}", file=sys.stderr)
        return 2
    charter = load_charter(str(path))
    ledger = UnifiedLedger(persist_path=ledger_path) if ledger_path else UnifiedLedger()
    ledger.record_usd(1000)
    auth = SovereignAuth()
    review = ReviewEngine(charter, audit_trail_path=audit_trail_path or os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH"))
    engine = GovernanceEngine(charter, ledger, auth=auth, review_engine=review)

    async def _run():
        plan, results, reports = await engine.run_mission_with_audit(goal, abort_on_audit_failure=False)
        passed = sum(1 for r in reports if getattr(r, "passed", False))
        print(f"Tasks: {len(plan.tasks)}, Passed: {passed}/{len(reports)}")
        for r in results:
            out = (r.output or "").strip()
            preview = out[:80] + ("..." if len(out) > 80 else "")
            print(f"  {r.task_id}: {'OK' if r.success else 'FAIL'} — {preview}")
        return 0 if passed == len(reports) and reports else 1

    return asyncio.run(_run())


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sovereign",
        description="Sovereign-OS CLI: charter-driven missions with CEO/CFO and audit.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, help="Command to run")
    run_parser = sub.add_parser("run", help="Run a one-shot mission (plan → approve → dispatch → audit)")
    run_parser.add_argument("-c", "--charter", required=True, help="Path to Charter YAML file")
    run_parser.add_argument("--ledger", default=None, help="Optional path to persist ledger JSONL (env: SOVEREIGN_LEDGER_PATH)")
    run_parser.add_argument("--audit-trail", default=None, help="Optional path for audit JSONL (env: SOVEREIGN_AUDIT_TRAIL_PATH)")
    run_parser.add_argument("goal", nargs="+", help="Goal text (one or more words)")
    args = parser.parse_args()
    if args.command == "run":
        goal_text = " ".join(args.goal)
        ledger_path = getattr(args, "ledger", None) or os.getenv("SOVEREIGN_LEDGER_PATH")
        audit_path = getattr(args, "audit_trail", None) or os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH")
        return _run_mission(args.charter, goal_text, ledger_path=ledger_path, audit_trail_path=audit_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
