"""
Pre-live end-to-end readiness check for the bidirectional oversight system.
Exercises every path in DRY-RUN (no keys, no funds) and prints a GO/NO-GO report.

  python examples/oversight_e2e.py

Covers:
  INBOUND   live discovery from TaskBounty + StacksTasker, then a governed mission
            (plan -> CFO budget -> workers -> Auditor) on one pulled task.
  OUTBOUND  governed hire (budget gate -> fund -> reserve), poll, quality-gate settle.
  PREFLIGHT ClawTasks + RentAHuman go-live safety checks.

Network calls are best-effort; a platform being offline degrades gracefully.
"""

from __future__ import annotations

import asyncio

CHECK, CROSS, WARN = "✓", "✗", "!"


def _line(icon: str, name: str, detail: str) -> str:
    return f"  [{icon}] {name:<26} {detail}"


async def main() -> None:
    results: list[str] = []

    # ---------------------------------------------------------------- INBOUND
    from sovereign_os.ingest_bridge.sources.bounty_board import stackstasker_source, taskbounty_source
    from sovereign_os.ingest_bridge.normalizer import to_job_payload

    pulled = []
    for name, factory in [("taskbounty", taskbounty_source), ("stackstasker", stackstasker_source)]:
        try:
            orders = list(factory(limit=5, timeout=12).fetch())
            pulled.extend(orders)
            results.append(_line(CHECK if orders else WARN, f"inbound:{name}", f"{len(orders)} open task(s) live"))
        except Exception as e:
            results.append(_line(WARN, f"inbound:{name}", f"offline: {str(e)[:40]}"))

    # Governed mission on one pulled task (stub workers, no LLM key needed).
    from sovereign_os.agents.auth import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.auditor.review_engine import StubAuditor
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import Charter, CoreCompetency, FiscalBoundaries

    charter = Charter(
        mission="Govern inbound and outbound work.",
        core_competencies=[CoreCompetency(name="research", description="r", priority=8)],
        fiscal_boundaries=FiscalBoundaries(daily_burn_max_usd=100.0, min_job_margin_ratio=0.2, max_task_cost_usd=50.0),
    )
    if pulled:
        led = UnifiedLedger(); led.record_usd(2000)
        eng = GovernanceEngine(charter, led, auth=SovereignAuth(),
                               review_engine=ReviewEngine(charter, judge=StubAuditor()))
        p = to_job_payload(pulled[0])
        _, res, reports = await eng.run_mission_with_audit(
            p["goal"], abort_on_audit_failure=False, job_revenue_cents=p["amount_cents"])
        ok = bool(reports) and all(r.passed for r in reports)
        results.append(_line(CHECK if ok else CROSS, "inbound:governed-mission",
                             f"{pulled[0].source_id} audited {sum(r.passed for r in reports)}/{len(reports)}"))
    else:
        results.append(_line(WARN, "inbound:governed-mission", "no live tasks to run"))

    # --------------------------------------------------------------- OUTBOUND
    from sovereign_os.governance.treasury import Treasury
    from sovereign_os.oversight import OversightBroker, OversightRegistry, RentAHumanClient, poll_and_settle

    led2 = UnifiedLedger(); led2.record_usd(10000)
    reg = OversightRegistry()
    broker = OversightBroker(Treasury(charter, led2), ReviewEngine(charter, judge=StubAuditor()),
                             RentAHumanClient("", live=False), ledger=led2, registry=reg)
    over = broker.post_governed_task(title="Over-budget gig", description="x", price_cents=8000)
    aff = broker.post_governed_task(title="Affordable gig", description="x", price_cents=2000)
    gate_ok = (over["posted"] is False) and (aff["posted"] is True)
    results.append(_line(CHECK if gate_ok else CROSS, "outbound:budget-gate",
                         f"$80 rejected, $20 funded; balance ${led2.total_usd_cents()/100:.2f}"))
    settled = await poll_and_settle(broker, reg)
    q_ok = bool(settled) and settled[0]["action"] == "released"
    results.append(_line(CHECK if q_ok else CROSS, "outbound:quality-gate",
                         f"polled+settled {len(settled)} -> {settled[0]['action'] if settled else 'none'}"))

    # -------------------------------------------------------------- PREFLIGHT
    from sovereign_os.ingest_bridge.clawtasks_preflight import run_preflight as claw_pf
    from sovereign_os.oversight.rentahuman_preflight import run_preflight as rah_pf

    for name, pf in [("clawtasks", claw_pf), ("rentahuman", rah_pf)]:
        try:
            rep = pf()
            results.append(_line(CHECK if rep["go"] else CROSS, f"preflight:{name}",
                                 f"{'GO' if rep['go'] else 'NO-GO'} (dry-run)"))
        except Exception as e:
            results.append(_line(WARN, f"preflight:{name}", f"error: {str(e)[:40]}"))

    # ----------------------------------------------------------------- REPORT
    print("\n=== Oversight end-to-end readiness (dry-run, no funds) ===")
    for r in results:
        print(r)
    blockers = sum(1 for r in results if f"[{CROSS}]" in r)
    print("-" * 60)
    print(f"  {'READY' if blockers == 0 else f'{blockers} BLOCKER(S)'} — set *_LIVE + keys and re-run the preflights to go live.")


if __name__ == "__main__":
    asyncio.run(main())
