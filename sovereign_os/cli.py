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


def _print_cost_trace(ledger) -> None:
    """Print a compact per-model / per-agent cost trace after a mission."""
    summary = ledger.cost_summary()
    cost_cents = summary["token_cost_cents"]
    print(
        f"Cost: ${cost_cents / 100:.4f} "
        f"({summary['total_input_tokens']} in + {summary['total_output_tokens']} out tokens)"
    )
    by_model = summary["by_model_cents"]
    if by_model:
        for model, cents in sorted(by_model.items(), key=lambda kv: -kv[1]):
            print(f"    {model}: ${cents / 100:.4f}")
    by_agent = summary["by_agent_cents"]
    if by_agent:
        print("  By agent:")
        for agent, cents in sorted(by_agent.items(), key=lambda kv: -kv[1]):
            print(f"    {agent}: ${cents / 100:.4f}")


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
        _print_cost_trace(ledger)
        return 0 if passed == len(reports) and reports else 1

    return asyncio.run(_run())


_INBOUND_SOURCES = {"taskbounty", "stackstasker", "clawtasks", "botbounty"}


def _pull(platform: str, limit: int) -> int:
    """Inbound: list open tasks live from a marketplace (discovery, no auth, no funds)."""
    platform = platform.strip().lower()
    if platform not in _INBOUND_SOURCES:
        print(f"Unknown platform '{platform}'. Choose from: {', '.join(sorted(_INBOUND_SOURCES))}", file=sys.stderr)
        return 2
    from sovereign_os.ingest_bridge.sources.bounty_board import (
        botbounty_source, stackstasker_source, taskbounty_source,
    )
    from sovereign_os.ingest_bridge.sources.clawtasks import ClawTasksOrderSource

    if platform == "taskbounty":
        src = taskbounty_source(limit=limit)
    elif platform == "stackstasker":
        src = stackstasker_source(limit=limit)
    elif platform == "botbounty":
        src = botbounty_source(limit=limit)
    else:
        src = ClawTasksOrderSource(limit=limit)
    orders = list(src.fetch())
    print(f"{platform}: {len(orders)} open task(s)")
    for o in orders:
        print(f"  {o.source_id}  {o.amount_cents/100:.2f} {o.currency}  — {o.goal[:64]!r}")
    return 0


def _hire(title: str, price_cents: int, description: str, balance_cents: int) -> int:
    """Outbound: post a governed task (CFO budget gate → fund escrow). Dry-run unless RENTAHUMAN_LIVE."""
    from sovereign_os import UnifiedLedger
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.auditor.review_engine import StubAuditor
    from sovereign_os.governance.treasury import Treasury
    from sovereign_os.models.charter import Charter, FiscalBoundaries
    from sovereign_os.oversight import OversightBroker, OversightRegistry, RentAHumanClient

    charter = Charter(mission="Oversight CLI", fiscal_boundaries=FiscalBoundaries(max_task_cost_usd=0.0))
    ledger = UnifiedLedger(persist_path=os.getenv("SOVEREIGN_LEDGER_PATH"))
    if ledger.total_usd_cents() <= 0:
        ledger.record_usd(balance_cents, purpose="working_capital")
    live = os.getenv("RENTAHUMAN_LIVE", "").lower() in ("1", "true", "yes")
    broker = OversightBroker(
        Treasury(charter, ledger),
        ReviewEngine(charter, judge=StubAuditor()),
        RentAHumanClient(os.getenv("RENTAHUMAN_API_KEY", ""), live=live),
        ledger=ledger,
        registry=OversightRegistry(persist_path=os.getenv("SOVEREIGN_OVERSIGHT_DB")),
    )
    print(f"Balance ${ledger.total_usd_cents()/100:.2f} · mode {'LIVE' if live else 'dry-run'}")
    res = broker.post_governed_task(title=title, description=description, price_cents=price_cents)
    if res["posted"]:
        print(f"[budget gate ✓] posted+funded '{title}' (escrow {res['escrow_id']}, ${price_cents/100:.2f})")
        print("  Worker delivers, then settle via the web /api/oversight/poll or poll_and_settle().")
        return 0
    print(f"[budget gate ✗] rejected: {res['reason']}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sovereign",
        description="Sovereign-OS CLI: govern inbound missions and outbound hires (budget + quality).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, help="Command to run")

    run_parser = sub.add_parser("run", help="Run a one-shot mission (plan → approve → dispatch → audit)")
    run_parser.add_argument("-c", "--charter", required=True, help="Path to Charter YAML file")
    run_parser.add_argument("--ledger", default=None, help="Optional path to persist ledger JSONL (env: SOVEREIGN_LEDGER_PATH)")
    run_parser.add_argument("--audit-trail", default=None, help="Optional path for audit JSONL (env: SOVEREIGN_AUDIT_TRAIL_PATH)")
    run_parser.add_argument("goal", nargs="+", help="Goal text (one or more words)")

    pull_parser = sub.add_parser("pull", help="Inbound: list open tasks live from a marketplace (no auth)")
    pull_parser.add_argument("platform", help="taskbounty | stackstasker | botbounty | clawtasks")
    pull_parser.add_argument("--limit", type=int, default=20, help="Max tasks to show")

    hire_parser = sub.add_parser("hire", help="Outbound: post a governed task (budget gate → fund escrow, dry-run)")
    hire_parser.add_argument("--title", required=True)
    hire_parser.add_argument("--price-cents", type=int, required=True, help="Task price in cents")
    hire_parser.add_argument("--description", default="")
    hire_parser.add_argument("--balance-cents", type=int, default=100000, help="Seed working capital if ledger empty")

    args = parser.parse_args()
    if args.command == "run":
        goal_text = " ".join(args.goal)
        ledger_path = getattr(args, "ledger", None) or os.getenv("SOVEREIGN_LEDGER_PATH")
        audit_path = getattr(args, "audit_trail", None) or os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH")
        return _run_mission(args.charter, goal_text, ledger_path=ledger_path, audit_trail_path=audit_path)
    if args.command == "pull":
        return _pull(args.platform, args.limit)
    if args.command == "hire":
        return _hire(args.title, args.price_cents, args.description, args.balance_cents)
    return 0


if __name__ == "__main__":
    sys.exit(main())
