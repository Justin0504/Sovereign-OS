"""
A governed mission anyone can watch without supplying an API key.

Why this exists: the hosted console's front door asked a visitor for an Anthropic key
before it would show them anything. Someone evaluating the system -- which is the whole
point of a public console -- saw a signup form and left. The product is governance, and
governance does not need a model: the Treasury, SovereignAuth, ReviewEngine and
UnifiedLedger used below are the same objects a paid mission runs through.

Two commitments, because a demo that overstates is worse than no demo:

1. **The governance decisions are real.** Real Treasury against real charter boundaries,
   real TrustScore thresholds, real SHA-256 proof hashes over the real audit records. The
   numbers in the trace were computed, not written.
2. **The work is not.** No model is called, so task output is a placeholder and says so.
   Nothing here should read as "the agent did the job".

The scenario is curated rather than taking the visitor's own goal. Without a model the
planner falls back to a single-task plan and routes by string matching, which produces a
bad plan and an echoed output -- a demo that makes a working system look broken. A fixed
scenario shows the mechanism honestly instead.

It deliberately includes refusals. A demo where everything is approved demonstrates
nothing: the product's claim is that it says no, so the trace has to contain a no.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.auditor import ReviewEngine
from sovereign_os.auditor.review_engine import StubAuditor
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, CoreCompetency, FiscalBoundaries
from sovereign_os.agents.base import TaskResult
from sovereign_os.governance.strategist import PlannedTask


@dataclass
class Step:
    stage: str
    decision: str                 # "allowed" | "refused" | "recorded"
    headline: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"stage": self.stage, "decision": self.decision,
                "headline": self.headline, **({"detail": self.detail} if self.detail else {})}


DEMO_CHARTER = Charter(
    mission="Deliver research briefs and outreach copy for a developer-tools company.",
    core_competencies=[
        CoreCompetency(name="research", description="Market and competitor research", priority=8),
        CoreCompetency(name="write_email", description="Outreach copy", priority=7),
    ],
    fiscal_boundaries=FiscalBoundaries(
        daily_burn_max_usd=2.00,
        max_budget_usd=25.00,
        max_task_cost_usd=0.40,          # the ceiling the third task will hit
        currency="USD",
    ),
)

# (task id, skill, what the CEO asked for, what the CFO was quoted in cents)
_TASKS = (
    ("task-1-research", "research", "Survey three competing agent-governance tools", 18),
    ("task-2-write_email", "write_email", "Draft a three-step outreach sequence", 12),
    ("task-3-research", "research", "Commission a full 40-page market report", 95),
)


def run_demo_mission() -> dict:
    """Execute the curated scenario and return the decision trace."""
    ledger = UnifiedLedger()
    ledger.record_usd(2_500, purpose="demo operating budget")
    treasury = Treasury(DEMO_CHARTER, ledger)
    auth = SovereignAuth()
    review = ReviewEngine(DEMO_CHARTER, judge=StubAuditor())

    steps: list[Step] = []
    fb = DEMO_CHARTER.fiscal_boundaries
    steps.append(Step(
        "charter", "recorded",
        "Charter loaded — every decision below is checked against these numbers.",
        {"mission": DEMO_CHARTER.mission,
         "daily_burn_max_usd": fb.daily_burn_max_usd,
         "max_budget_usd": fb.max_budget_usd,
         "max_task_cost_usd": fb.max_task_cost_usd}))

    steps.append(Step(
        "plan", "recorded",
        f"CEO decomposed the goal into {len(_TASKS)} tasks.",
        {"tasks": [{"task_id": t, "skill": s, "description": d} for t, s, d, _ in _TASKS]}))

    approved: list[tuple[str, str, int]] = []
    for task_id, skill, _desc, cents in _TASKS:
        try:
            treasury.approve_task(cents, task_id=task_id, skill=skill)
        except Exception as exc:  # noqa: BLE001 - the refusal reason is the output
            steps.append(Step(
                "budget", "refused",
                f"CFO refused {task_id} — {cents}¢ against a "
                f"{int(fb.max_task_cost_usd * 100)}¢ per-task ceiling.",
                {"task_id": task_id, "estimated_cents": cents,
                 "per_task_ceiling_cents": int(fb.max_task_cost_usd * 100),
                 "reason": str(exc)}))
            continue
        approved.append((task_id, skill, cents))
        steps.append(Step(
            "budget", "allowed",
            f"CFO approved {task_id} at {cents}¢.",
            {"task_id": task_id, "estimated_cents": cents,
             "per_task_ceiling_cents": int(fb.max_task_cost_usd * 100)}))

    # A worker that has not earned the capability is refused it, and one that has is not.
    worker = "worker-demo"
    auth._set_score(worker, 55)
    for capability in (Capability.READ_FILES, Capability.SPEND_USD):
        threshold = auth.get_threshold(capability)
        granted = auth.check_permission(worker, capability)
        steps.append(Step(
            "permission", "allowed" if granted else "refused",
            (f"{worker} may {capability.value} (TrustScore 55)." if granted
             else f"{worker} may not {capability.value} — TrustScore 55 is below the "
                  f"{threshold} this capability requires."),
            {"capability": capability.value, "trust_score": 55, "threshold": threshold}))

    for task_id, skill, cents in approved:
        ledger.record_token("demo-stub", 900, 240, task_id=task_id,
                            estimated_usd_cents=cents, category=skill)
        # No hasattr guard. An earlier draft had one and it silently swallowed a wrong
        # method name, dropping the audit -- the most load-bearing step -- from the trace
        # with no error anywhere. A rename should break this, loudly.
        planned = PlannedTask(task_id=task_id, description=f"[demo] {skill}",
                              dependencies=[], required_skill=skill,
                              estimated_token_budget=4000, priority="high")
        result = TaskResult(
            task_id=task_id, success=True,
            output=f"[placeholder — no model was called] {skill} deliverable for {task_id}",
        )
        report = asyncio.run(review.audit_task(planned, result, category=skill))
        steps.append(Step(
            "audit", "allowed" if report.passed else "refused",
            f"Auditor {'passed' if report.passed else 'failed'} {task_id} "
            f"(score {report.score}).",
            {"task_id": task_id, "score": report.score,
             "proof_hash": report.proof_hash,
             "note": "The hash is computed over the real audit record. The deliverable "
                     "it attests to is a placeholder."}))

    steps.append(Step(
        "ledger", "recorded",
        f"Ledger recorded {ledger.total_token_estimated_usd_cents()}¢ of token spend, "
        f"append-only.",
        {"token_cents": ledger.total_token_estimated_usd_cents(),
         "balance_cents": ledger.total_usd_cents()}))

    refusals = sum(1 for s in steps if s.decision == "refused")
    return {
        "demo": True,
        "disclaimer": (
            "Governance decisions here are real: the Treasury, TrustScore thresholds, "
            "audit rubric and SHA-256 proof hashes are the same code a paid mission "
            "runs through, and every number was computed. No model was called, so task "
            "output is a placeholder — nothing here is finished work."
        ),
        "scenario": DEMO_CHARTER.mission,
        "refusals": refusals,
        "steps": [s.as_dict() for s in steps],
    }
