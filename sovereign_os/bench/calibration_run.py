"""
Calibration harness: generate the (estimate, actual) corpus the money stack lacks.

`cost_model` can now describe the shape of the estimator's error, but describing it
requires settled jobs, and a fresh deployment has none. This module produces them
systematically: a task suite spread across categories and complexity, each task run
end to end, with the estimate captured before execution and the realized cost measured
afterwards.

Two measurement decisions matter more than the plumbing.

**The estimate is taken uncalibrated.** `estimate_task_cost_cents(..., calibrated=False)`
is deliberately used, so what gets measured is the raw estimator rather than an estimator
already corrected by the factor this experiment exists to evaluate. Measuring the
corrected one would be chasing a moving target and would understate the error.

**The actual is taken from the ledger tap, not from the agent.** Realized cost is read
through `GroundTruthMonitor`, which observes `UnifiedLedger.record_*` at the call site.
An agent that under-reports its own consumption — whether adversarially or, far more
commonly, because the model is simply bad at introspecting token use — cannot move this
number.

The runner is injected, so the suite can be exercised against a stub offline and against
the real governance engine when spending real tokens is intended.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sovereign_os.bench.oracle import GroundTruthMonitor, ObservationKind
from sovereign_os.governance.cost_model import CostCalibrator
from sovereign_os.governance.economics import complexity_from_goal, estimate_task_cost_cents

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibrationTask:
    """One probe: a real goal whose cost we want to predict before running it."""

    id: str
    goal: str
    category: str
    tier: str = "medium"
    """Intended difficulty. Kept separate from the derived complexity score so the
    heuristic's own notion of difficulty can be checked against the intended one."""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CalibrationRow:
    """One settled probe: what we predicted, what it cost, and the error."""

    task_id: str
    category: str
    tier: str
    complexity: float
    estimated_cents: float
    actual_cents: float
    model: str = ""
    ok: bool = True
    error: str = ""
    duration_s: float = 0.0

    @property
    def ratio(self) -> float | None:
        if self.estimated_cents <= 0 or self.actual_cents <= 0:
            return None
        return self.actual_cents / self.estimated_cents

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ratio"] = self.ratio
        return d


# A spread of real goals: six categories at three difficulty tiers. The spread is the
# point — a corpus concentrated in one category would measure that category, not the
# estimator.
DEFAULT_SUITE: tuple[CalibrationTask, ...] = (
    # ---- coding
    CalibrationTask("code-s", "Write a Python function that validates an email address "
                              "and return it with a docstring.", "coding", "small"),
    CalibrationTask("code-m", "Write a Python module that parses a CSV of transactions "
                              "and reports monthly totals, with unit tests.", "coding", "medium"),
    CalibrationTask("code-l", "Refactor a synchronous HTTP client into an async one with "
                              "retries, connection pooling, timeout handling, and a full "
                              "test suite covering each failure mode.", "coding", "large"),
    # ---- research
    CalibrationTask("res-s", "In one paragraph, what is HTTP 402 used for?", "research", "small"),
    CalibrationTask("res-m", "Summarize the current landscape of agent-to-agent payment "
                             "protocols and how they differ.", "research", "medium"),
    CalibrationTask("res-l", "Survey approaches to budget enforcement in autonomous agent "
                             "systems, compare their guarantees, and identify open problems.",
                    "research", "large"),
    # ---- writing
    CalibrationTask("wri-s", "Write a two-sentence product tagline for a governed AI agent "
                             "workspace.", "writing", "small"),
    CalibrationTask("wri-m", "Write a 600-word blog post explaining why per-call budget "
                             "gates fail for autonomous agents.", "writing", "medium"),
    CalibrationTask("wri-l", "Write a detailed technical white paper section on capability "
                             "leasing, including motivation, mechanism, failure modes, and "
                             "worked examples.", "writing", "large"),
    # ---- data
    CalibrationTask("dat-s", "Given monthly revenue figures, compute the growth rate.",
                    "data", "small"),
    CalibrationTask("dat-m", "Analyze a set of job records and report cost per category "
                             "with the outliers called out.", "data", "medium"),
    CalibrationTask("dat-l", "Build a cohort analysis over a year of transaction data, "
                             "segment by acquisition channel, and explain the retention "
                             "differences you find.", "data", "large"),
    # ---- design
    CalibrationTask("des-s", "Suggest a colour palette for a minimal developer tool.",
                    "design", "small"),
    CalibrationTask("des-m", "Write a design brief for a dashboard that shows agent "
                             "spend and audit status.", "design", "medium"),
    CalibrationTask("des-l", "Produce a full design specification for a multi-tenant "
                             "console: information architecture, states, and component "
                             "inventory.", "design", "large"),
    # ---- automation
    CalibrationTask("aut-s", "Describe the steps to schedule a daily backup.",
                    "automation", "small"),
    CalibrationTask("aut-m", "Design a workflow that ingests inbound tasks, screens them "
                             "for profitability, and dispatches the profitable ones.",
                    "automation", "medium"),
    CalibrationTask("aut-l", "Specify an end-to-end pipeline that monitors several job "
                             "marketplaces, prices bids, tracks outcomes, and reallocates "
                             "budget weekly based on realized yield.", "automation", "large"),
)


# A runner takes a task and performs it, returning the cost actually realized in cents.
Runner = Callable[[CalibrationTask], float]


def estimate_for(task: CalibrationTask, model: str = "gpt-4o") -> tuple[float, float]:
    """
    The raw, uncalibrated prediction for a task: (estimated_cents, complexity).

    Uncalibrated on purpose — see the module docstring.
    """
    complexity = complexity_from_goal(task.goal)
    estimated = estimate_task_cost_cents(
        task.category, model, complexity=complexity, calibrated=False
    )
    return float(estimated), complexity


def run_calibration(
    runner: Runner,
    tasks: Sequence[CalibrationTask] = DEFAULT_SUITE,
    *,
    model: str = "gpt-4o",
    calibrator: CostCalibrator | None = None,
    out_path: str | Path | None = None,
) -> tuple[list[CalibrationRow], dict]:
    """
    Run the suite, record each (estimate, actual) pair, and return the rows plus the
    calibration report computed over them.

    A task that raises is recorded with `ok=False` and excluded from the statistics —
    a crashed run carries no information about cost estimation, and silently folding it
    in as a zero would bias the result toward over-estimation.
    """
    cal = calibrator or CostCalibrator()
    rows: list[CalibrationRow] = []

    for task in tasks:
        estimated, complexity = estimate_for(task, model)
        started = time.monotonic()
        try:
            actual = float(runner(task))
            row = CalibrationRow(
                task_id=task.id, category=task.category, tier=task.tier,
                complexity=complexity, estimated_cents=estimated, actual_cents=actual,
                model=model, ok=True, duration_s=time.monotonic() - started,
            )
            cal.record(task.category, estimated, actual, model=model, complexity=complexity)
        except Exception as exc:  # noqa: BLE001 - a failed probe is data about the runner, not the estimator
            logger.warning("CALIBRATION: task %s failed: %s", task.id, exc)
            row = CalibrationRow(
                task_id=task.id, category=task.category, tier=task.tier,
                complexity=complexity, estimated_cents=estimated, actual_cents=0.0,
                model=model, ok=False, error=str(exc),
                duration_s=time.monotonic() - started,
            )
        rows.append(row)

    report = cal.calibration_report()
    report["suite"] = {
        "tasks": len(tasks),
        "completed": sum(1 for r in rows if r.ok),
        "failed": sum(1 for r in rows if not r.ok),
        "model": model,
    }

    if out_path:
        write_rows(rows, out_path, report=report)
    return rows, report


def write_rows(
    rows: Sequence[CalibrationRow], out_path: str | Path, *, report: dict | None = None
) -> Path:
    """Persist rows as JSONL so a run can be re-analyzed without re-running it."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row.as_dict()) + "\n")
    if report is not None:
        path.with_suffix(".report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    return path


def load_rows(path: str | Path) -> list[CalibrationRow]:
    """Read back a persisted run."""
    rows: list[CalibrationRow] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        d.pop("ratio", None)
        rows.append(CalibrationRow(**d))
    return rows


# --------------------------------------------------------------- production runner

def engine_runner(engine, ledger) -> Runner:
    """
    A runner that executes each task through the real governance engine and measures
    what it cost by watching the ledger, not by asking the agent.

    Wrapping the mission in a fresh `GroundTruthMonitor` per task is what makes the
    measurement trustworthy: the realized figure is the sum of charges observed at
    `UnifiedLedger.record_*`, so nothing the agent reports about its own consumption
    enters the number.
    """
    import asyncio

    def run(task: CalibrationTask) -> float:
        monitor = GroundTruthMonitor()
        with monitor.watching(ledger=ledger):
            result = engine.run_mission_with_audit(task.goal)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        return float(sum(
            int(o.payload.get("spend_cents") or 0)
            for o in monitor.of_kind(ObservationKind.COST)
        ))

    return run


def main() -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description="Run the cost-calibration suite.")
    parser.add_argument("--out", default="data/calibration.jsonl")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N tasks")
    parser.add_argument("--charter", default="", help="charter YAML (default: charter.default.yaml)")
    args = parser.parse_args()

    from sovereign_os.governance.engine import GovernanceEngine  # noqa: PLC0415
    from sovereign_os.ledger.unified_ledger import UnifiedLedger  # noqa: PLC0415
    from sovereign_os.models.charter import load_charter  # noqa: PLC0415

    charter_path = args.charter or ("charter.default.yaml"
                                    if Path("charter.default.yaml").exists()
                                    else "charter.example.yaml")
    charter = load_charter(charter_path)
    ledger = UnifiedLedger(persist_path="data/ledger.jsonl")
    engine = GovernanceEngine(charter, ledger)
    tasks = DEFAULT_SUITE[: args.limit] if args.limit else DEFAULT_SUITE

    rows, report = run_calibration(
        engine_runner(engine, ledger), tasks, model=args.model, out_path=args.out
    )
    print(json.dumps(report, indent=2))
    print(f"\n{len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
