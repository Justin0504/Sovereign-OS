"""
End-to-end demo of the ClawTasks auto-accept loop — runs with NO API keys and
NO network (a mock bounty feed stands in for ClawTasks while its /bounties
endpoint is in free-tasks-only hardening mode).

Flow per bounty:
  discover (ClawTasksOrderSource) -> job payload -> govern (CEO/CFO/workers/audit)
  -> deliver back (claim + submit, dry-run)

Run:  python examples/clawtasks_loop_demo.py
"""

from __future__ import annotations

import asyncio

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.auditor import ReviewEngine
from sovereign_os.auditor.review_engine import StubAuditor
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.ingest_bridge.normalizer import to_job_payload
from sovereign_os.ingest_bridge.sources.clawtasks import ClawTasksClient, ClawTasksOrderSource
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, CoreCompetency, FiscalBoundaries, SuccessKPI

# A ClawTasks-shaped bounty feed (what GET /bounties would return when healthy).
MOCK_BOUNTIES = [
    {
        "id": "demo-101", "title": "Write a short blog post about AI agents",
        "description": "300 words, friendly tone, 3 takeaways.",
        "amount": 25, "currency": "USDC", "status": "open", "mode": "instant",
        "funded": True, "deadline_hours": 24, "tags": ["writing"], "poster": "alice",
    },
    {
        "id": "demo-102", "title": "Research the BNPL competitive landscape",
        "description": "Top 5 players, one-line positioning each.",
        "amount": 40, "currency": "USDC", "status": "open", "mode": "instant",
        "funded": True, "deadline_hours": 48, "tags": ["research"], "poster": "bob",
    },
    {
        "id": "demo-103", "title": "Unfunded — should be skipped",
        "description": "x", "amount": 99, "status": "open", "funded": False,
    },
]


def _charter() -> Charter:
    return Charter(
        mission="Deliver high-quality freelance content and research.",
        core_competencies=[
            CoreCompetency(name="research", description="Market research", priority=8),
            CoreCompetency(name="write_article", description="Articles/blog posts", priority=8),
        ],
        fiscal_boundaries=FiscalBoundaries(daily_burn_max_usd=50.0, min_job_margin_ratio=0.2),
        success_kpis=[SuccessKPI(name="ok", metric="tasks_ok", target_value=0.9,
                                 verification_prompt="Did the output satisfy the request?")],
    )


async def main() -> None:
    charter = _charter()

    # 1) DISCOVER — mock feed injected in place of the live HTTP call.
    source = ClawTasksOrderSource(get_json=lambda *a, **k: MOCK_BOUNTIES)
    orders = list(source.fetch())
    print(f"[discover] {len(orders)} fundable bount(ies) from ClawTasks\n")

    client = ClawTasksClient(api_key="", live=False)  # dry-run delivery

    for order in orders:
        payload = to_job_payload(order)
        bounty_id = order.contact["bounty_id"]
        print(f"=== bounty {bounty_id} — ${payload['amount_cents']/100:.2f} {payload['currency']} ===")
        print(f"[goal] {payload['goal'][:70]!r}")

        # 2) GOVERN — fund working capital, then plan -> CFO -> workers -> audit.
        ledger = UnifiedLedger()
        ledger.record_usd(1000)  # $10 working capital
        engine = GovernanceEngine(
            charter, ledger,
            auth=SovereignAuth(),
            review_engine=ReviewEngine(charter, judge=StubAuditor()),
        )
        plan, results, reports = await engine.run_mission_with_audit(
            payload["goal"], abort_on_audit_failure=False,
            job_revenue_cents=payload["amount_cents"],
        )
        passed = sum(1 for r in reports if r.passed)
        cost = ledger.cost_summary()
        print(f"[govern] {len(plan.tasks)} task(s), audit {passed}/{len(reports)} passed, "
              f"cost ${cost['token_cost_cents']/100:.4f}")

        # 3) DELIVER — claim + submit back to ClawTasks (dry-run; no funds moved).
        deliverable = "\n".join(r.output for r in results).strip()
        client.claim(bounty_id)
        client.submit(bounty_id, deliverable)
        print(f"[deliver] claim+submit (dry-run) for {bounty_id}\n")

    print("Loop complete. Set CLAWTASKS_LIVE=true + a funded Base wallet to settle for real.")


if __name__ == "__main__":
    asyncio.run(main())
