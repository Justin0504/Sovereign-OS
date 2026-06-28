"""
Outbound oversight demo: Sovereign-OS governs an agent hiring external workers.
No API key, no network, no funds moved (RentAHuman client in dry-run).

Shows the two gates:
  - Budget gate: an over-budget task is rejected before any escrow is funded.
  - Quality gate: a good deliverable is released (paid); a poor one is disputed
    (funds withheld).

Run:  python examples/oversight_demo.py
"""

from __future__ import annotations

import asyncio

from sovereign_os.auditor import ReviewEngine
from sovereign_os.auditor.review_engine import StubAuditor
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, FiscalBoundaries
from sovereign_os.oversight import OversightBroker, RentAHumanClient


async def main() -> None:
    charter = Charter(
        mission="Coordinate and quality-gate outsourced work.",
        fiscal_boundaries=FiscalBoundaries(daily_burn_max_usd=100.0, max_task_cost_usd=50.0),
    )
    ledger = UnifiedLedger()
    ledger.record_usd(20000)  # $200 working capital
    broker = OversightBroker(
        Treasury(charter, ledger),
        ReviewEngine(charter, judge=StubAuditor()),
        RentAHumanClient(api_key="", live=False),  # dry-run
        ledger=ledger,
    )
    print("=== OUTBOUND OVERSIGHT (dry-run, no funds moved) ===")
    print(f"Start balance: ${ledger.total_usd_cents()/100:.2f}  (per-task ceiling $50)\n")

    # 1) BUDGET GATE — over the $50 per-task ceiling -> rejected, nothing funded.
    over = broker.post_governed_task(title="Expensive gig", description="big job", price_cents=8000)
    print(f"[budget gate] $80 task -> posted={over['posted']}  ({over['reason'][:60]})\n")

    # 2) Two affordable tasks get posted + funded.
    good = broker.post_governed_task(title="Write a product blurb", description="100 words", price_cents=2500)
    bad = broker.post_governed_task(title="Translate a paragraph", description="EN->ES", price_cents=1500)
    print(f"[posted] {good['escrow_id']} $25.00  |  {bad['escrow_id']} $15.00\n")

    # 3) QUALITY GATE — human delivers; Auditor decides release vs dispute.
    r1 = await broker.review_and_settle(
        escrow_id=good["escrow_id"], deliverable="A crisp, on-brand 100-word product blurb...",
        task_description="Write a product blurb", price_cents=2500,
    )
    print(f"[quality gate] good work  -> {r1['action']}  paid={r1['paid']}")

    r2 = await broker.review_and_settle(
        escrow_id=bad["escrow_id"], deliverable="",  # nothing delivered
        task_description="Translate a paragraph", price_cents=1500,
    )
    print(f"[quality gate] empty work -> {r2['action']}  paid={r2['paid']}")

    paid = ledger.total_usd_cents()
    print(f"\nEnd balance: ${paid/100:.2f}  (only the passing $25 task was paid; the $15 one was withheld)")


if __name__ == "__main__":
    asyncio.run(main())
