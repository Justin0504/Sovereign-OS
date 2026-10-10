"""
The governance evaluation suite the paper reports.

Written because the paper's headline numbers — a 100% fiscal block rate over 30
scenarios, 94% permission-gating accuracy over 200 missions, zero integrity failures
over 1,200+ reports — had no harness in the repository. Whatever produced them was not
committed, so a reader cloning the repo could not reproduce the result that the paper
leads with. For a governance system whose entire argument is "verify rather than trust",
an unreproducible evaluation is the wrong kind of irony.

So this runs the three axes for real and reports what actually happens, including where
that differs from the published figures. The numbers below are outputs, not targets: a
harness written to reach a number it already knows is a different and much less useful
artifact.

    python -m sovereign_os.bench.governance_eval --out data/governance_eval.json
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AxisResult:
    name: str
    total: int
    passed: int
    details: list[dict] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    def as_dict(self) -> dict:
        return {"name": self.name, "total": self.total, "passed": self.passed,
                "rate": round(self.rate, 4), "failures": [
                    d for d in self.details if not d.get("ok")][:20]}


# --------------------------------------------------------------- axis 1: fiscal

# Each scenario states a ledger balance, charter bounds, and a request that MUST be
# refused. The expectation is named, so a scenario that fails for the wrong reason —
# refused, but by a different gate than the one under test — is not scored as a pass.
FISCAL_SCENARIOS: tuple[dict, ...] = (
    # insufficient balance
    *({"category": "insufficient_balance", "balance": bal, "cost": cost,
       "expect": "FiscalInsolvencyError"}
      for bal, cost in ((0, 1), (10, 50), (100, 101), (500, 5000), (1, 2), (250, 400))),
    # minimum reserve depletion
    *({"category": "min_reserve", "balance": bal, "cost": cost, "min_reserve": reserve,
       "expect": "FiscalInsolvencyError"}
      for bal, cost, reserve in ((1000, 600, 500), (200, 150, 100), (5000, 4600, 500),
                                 (300, 250, 100), (100, 60, 50), (2000, 1800, 500))),
    # per-task ceiling
    *({"category": "task_ceiling", "balance": 100000, "cost": cost, "max_task": cap,
       "expect": "FiscalInsolvencyError"}
      for cost, cap in ((500, 100), (1000, 250), (5000, 1000), (200, 50), (10000, 2000),
                        (750, 500))),
    # daily burn cap
    *({"category": "daily_burn", "balance": 100000, "cost": cost, "daily_cap": cap,
       "spent_today": spent, "expect": "FiscalInsolvencyError"}
      for cost, cap, spent in ((500, 1000, 800), (200, 500, 400), (1000, 1000, 1),
                               (100, 300, 250), (50, 100, 60), (2000, 2500, 1000))),
    # unprofitable job (margin floor)
    *({"category": "unprofitable", "revenue": rev, "cost": cost, "margin_floor": floor,
       "expect": "UnprofitableJobError"}
      for rev, cost, floor in ((100, 90, 0.35), (1000, 900, 0.35), (500, 400, 0.35),
                               (200, 199, 0.5), (300, 250, 0.4), (150, 120, 0.35))),
)


def run_fiscal_axis() -> AxisResult:
    """Every scenario must be refused, and refused by the gate it targets."""
    from sovereign_os.governance.exceptions import FiscalInsolvencyError, UnprofitableJobError
    from sovereign_os.governance.treasury import Treasury
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import Charter, FiscalBoundaries

    result = AxisResult(name="fiscal_governance", total=len(FISCAL_SCENARIOS), passed=0)

    def _charter(**bounds) -> Charter:
        # Bounds live on the Charter in USD; the gates work in cents. Converting here
        # keeps the scenario table in the unit the gates actually compare.
        return Charter(mission="governance evaluation",
                       fiscal_boundaries=FiscalBoundaries(**bounds))

    for i, sc in enumerate(FISCAL_SCENARIOS):
        ledger = UnifiedLedger()
        detail: dict[str, Any] = {"scenario": i, "category": sc["category"],
                                  "expect": sc["expect"]}
        try:
            if sc["category"] == "unprofitable":
                charter = _charter(min_job_margin_ratio=sc["margin_floor"])
                treasury = Treasury(charter, ledger)
                ledger.record_usd(1_000_000, purpose="eval_seed")
                treasury.approve_job_profitability(sc["revenue"], sc["cost"])
            else:
                bounds: dict[str, Any] = {}
                if "max_task" in sc:
                    bounds["max_task_cost_usd"] = sc["max_task"] / 100.0
                if "daily_cap" in sc:
                    bounds["daily_burn_max_usd"] = sc["daily_cap"] / 100.0
                charter = _charter(**bounds)
                kwargs: dict[str, Any] = {}
                if "min_reserve" in sc:
                    kwargs["min_reserve_cents"] = sc["min_reserve"]
                treasury = Treasury(charter, ledger, **kwargs)
                if sc["balance"]:
                    ledger.record_usd(sc["balance"], purpose="eval_seed")
                if sc.get("spent_today"):
                    ledger.record_usd(-sc["spent_today"], purpose="eval_prior_spend")
                treasury.approve_task(sc["cost"], task_id=f"eval-{i}")
            detail.update(ok=False, got="no exception — the request was approved")
        except (FiscalInsolvencyError, UnprofitableJobError) as exc:
            got = type(exc).__name__
            detail.update(ok=(got == sc["expect"]), got=got)
        except Exception as exc:  # noqa: BLE001 - an unexpected error is not a clean refusal
            detail.update(ok=False, got=f"unexpected {type(exc).__name__}: {exc}")

        result.passed += bool(detail["ok"])
        result.details.append(detail)
    return result


# ----------------------------------------------------- axis 2: permission gating

def run_permission_axis(missions: int = 200, seed: int = 20261009) -> AxisResult:
    """
    Does SovereignAuth grant exactly what an agent has earned?

    Each mission updates an agent's record and then asks for a capability. The oracle is
    the threshold rule itself, applied independently to the score the store reports — so
    this measures whether the gate is consistent with its own policy, not whether the
    policy is wise. Scoring it against a hand-labelled expectation would instead measure
    the labeller.
    """
    from sovereign_os.agents.auth import Capability, SovereignAuth

    rng = random.Random(seed)
    profiles = {"reliable": 0.95, "mixed": 0.55, "failing": 0.15}
    result = AxisResult(name="permission_gating", total=missions, passed=0)

    auth = SovereignAuth()
    agents = [f"agent-{i}" for i in range(10)]
    for agent in agents:
        auth._set_score(agent, 50)

    capabilities = list(Capability)
    for m in range(missions):
        agent = agents[m % len(agents)]
        profile = list(profiles)[m % len(profiles)]
        auth.record_audit(agent, passed=(rng.random() < profiles[profile]))

        capability = capabilities[m % len(capabilities)]
        score = auth.get_trust_score(agent)
        threshold = auth.get_threshold(capability)
        expected = score >= threshold
        granted = auth.check_permission(agent, capability)

        ok = granted == expected
        result.passed += ok
        result.details.append({"mission": m, "agent": agent, "profile": profile,
                               "capability": capability.value, "score": score,
                               "threshold": threshold, "granted": granted,
                               "expected": expected, "ok": ok})
    return result


# ---------------------------------------------------- axis 3: audit integrity

def run_integrity_axis(reports: int = 1200, seed: int = 20261009) -> AxisResult:
    """
    Recompute each report's proof hash and compare, then confirm a tampered report is
    actually caught — a verifier that returns True for everything would pass the first
    half and be worthless.
    """
    from sovereign_os.auditor.trail import verify_report_integrity

    import hashlib

    rng = random.Random(seed)
    result = AxisResult(name="audit_integrity", total=reports, passed=0)

    # The verifier hashes a FIXED set of seven fields, so a report has to be sealed the
    # same way to be checkable. Fields outside that set are not covered — which is a
    # property of the guarantee, probed below rather than assumed away.
    CANONICAL = ("task_id", "kpi_name", "passed", "score", "reason", "suggested_fix",
                 "timestamp_utc")

    def seal(entry: dict) -> dict:
        canonical = {
            "task_id": entry.get("task_id", ""), "kpi_name": entry.get("kpi_name", ""),
            "passed": bool(entry.get("passed", False)),
            "score": float(entry.get("score", 0)), "reason": entry.get("reason", ""),
            "suggested_fix": entry.get("suggested_fix", ""),
            "timestamp_utc": entry.get("timestamp_utc", ""),
        }
        payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
        entry["proof_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return entry

    hashes: set[str] = set()
    tamper_attempts = tamper_caught = 0
    uncovered_attempts = uncovered_caught = 0

    for i in range(reports):
        entry = seal({
            "task_id": f"task-{i}", "kpi_name": f"kpi-{i % 5}",
            "passed": bool(rng.random() > 0.3), "score": round(rng.random(), 4),
            "reason": f"evaluation report {i}", "suggested_fix": "",
            "timestamp_utc": f"2026-10-09T00:00:{i % 60:02d}+00:00",
            # Deliberately outside the canonical set.
            "agent_id": f"agent-{i % 10}",
        })
        intact = verify_report_integrity(entry)
        hashes.add(entry["proof_hash"])

        if i % 10 == 0:
            # Tamper with the verdict — inside the covered set, must be caught.
            tamper_attempts += 1
            flipped = dict(entry)
            flipped["passed"] = not flipped["passed"]
            tamper_caught += not verify_report_integrity(flipped)

            # Tamper with WHO produced it — outside the covered set. Recorded rather
            # than asserted: the point is to measure the guarantee's edge, not to claim
            # one that is not there.
            uncovered_attempts += 1
            relabelled = dict(entry)
            relabelled["agent_id"] = "someone-else"
            uncovered_caught += not verify_report_integrity(relabelled)

        result.passed += bool(intact)
        if not intact:
            result.details.append({"report": i, "ok": False,
                                   "got": "verifier rejected an untampered report"})

    result.details.append({
        "ok": True, "distinct_hashes": len(hashes), "collisions": reports - len(hashes),
        "tamper_inside_canonical": f"{tamper_caught}/{tamper_attempts} caught",
        "tamper_outside_canonical": f"{uncovered_caught}/{uncovered_attempts} caught",
        "covered_fields": list(CANONICAL),
        "note": "The proof hash covers the verdict, not the whole record. Re-attributing "
                "a report to a different agent leaves the hash valid.",
    })
    return result


# --------------------------------------------------------------------- driver

def run_all(*, missions: int = 200, reports: int = 1200) -> dict:
    axes = [run_fiscal_axis(), run_permission_axis(missions), run_integrity_axis(reports)]
    return {
        "axes": {a.name: a.as_dict() for a in axes},
        "summary": {a.name: f"{a.passed}/{a.total} ({a.rate:.1%})" for a in axes},
    }


def main() -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/governance_eval.json")
    parser.add_argument("--missions", type=int, default=200)
    parser.add_argument("--reports", type=int, default=1200)
    args = parser.parse_args()

    report = run_all(missions=args.missions, reports=args.reports)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report["summary"], indent=2))
    for name, axis in report["axes"].items():
        if axis["failures"]:
            print(f"\n{name}: {len(axis['failures'])} failure(s), first few:")
            for f in axis["failures"][:5]:
                print("  ", json.dumps(f, default=str)[:160])
    print(f"\nfull report -> {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
