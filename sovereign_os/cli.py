"""
Sovereign-OS CLI: run a one-shot mission from the command line.

  sovereign run --charter path/to/charter.yaml "Your goal here"
  sovereign run -c charter.example.yaml "Summarize the market"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _run_mission(charter_path: str, goal: str) -> int:
    from sovereign_os import load_charter, UnifiedLedger
    from sovereign_os.agents import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance import GovernanceEngine

    charter = load_charter(charter_path)
    ledger = UnifiedLedger()
    ledger.record_usd(1000)
    auth = SovereignAuth()
    review = ReviewEngine(charter)
    engine = GovernanceEngine(charter, ledger, auth=auth, review_engine=review)

    async def _run():
        plan, results, reports = await engine.run_mission_with_audit(goal, abort_on_audit_failure=False)
        passed = sum(1 for r in reports if getattr(r, "passed", False))
        print(f"Tasks: {len(plan.tasks)}, Passed: {passed}/{len(reports)}")
        for r in results:
            print(f"  {r.task_id}: {'OK' if r.success else 'FAIL'} — {r.output[:80]}...")
        return 0 if passed == len(reports) and reports else 1

    return asyncio.run(_run())


def main() -> int:
    parser = argparse.ArgumentParser(prog="sovereign", description="Sovereign-OS CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Run a one-shot mission")
    run_parser.add_argument("-c", "--charter", required=True, help="Path to Charter YAML")
    run_parser.add_argument("goal", nargs="+", help="Goal text (one or more words)")
    args = parser.parse_args()
    if args.command == "run":
        goal_text = " ".join(args.goal)
        return _run_mission(args.charter, goal_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
