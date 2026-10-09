"""
Sovereign-OS as an MCP server: governed execution inside the tool people already use.

Everything else in this repo assumes someone adopts a runtime — clones it, configures a
charter, runs a console. That is a large ask for a governance layer, whose value only
shows up once it is in the path of real work. Exposing the same control plane over MCP
inverts it: a developer adds one entry to their client config and the agent they are
already using starts clearing a budget check, carrying scoped authority, and leaving a
verifiable receipt.

The tools are deliberately few, and each answers a question someone actually has before
or after spending money:

- `forecast_cost`    what will this cost, before I run it
- `run_governed`     do the work under a budget and a scoped grant
- `audit_receipt`    what happened, and can I verify it independently
- `governance_status` what are the current limits and how good are the estimates

What is NOT exposed matters as much. There is no tool to raise a budget, grant a
capability, or disable a check. A governance layer whose own controls are reachable from
the agent it governs is decorative: the first thing a model does when it hits a ceiling
is look for the lever. The levers live in the charter and the environment, outside the
protocol surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "sovereign-os"


def _charter_path() -> str:
    for candidate in (os.getenv("SOVEREIGN_CHARTER", ""), "charter.default.yaml",
                      "charter.example.yaml"):
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


class GovernedRuntime:
    """
    One engine, built lazily and reused across calls.

    Lazy because an MCP client starts the server at launch and may never call a tool;
    building a ledger and a trust store on a tool list would make every client start
    slower for nothing. Reused because the trust score, the ledger and the learned cost
    calibration are the state that makes governance mean anything — a fresh engine per
    call would reset the history each time and quietly turn the whole thing into theatre.
    """

    def __init__(self) -> None:
        self._engine = None
        self._ledger = None
        self._auth = None
        self._broker = None

    def _build(self):
        if self._engine is not None:
            return
        from sovereign_os.agents.auth import SovereignAuth
        from sovereign_os.agents.delegation import DelegationBroker
        from sovereign_os.agents.delegation_gate import set_broker
        from sovereign_os.governance.cost_model import CostCalibrator
        from sovereign_os.governance.engine import GovernanceEngine
        from sovereign_os.ledger.unified_ledger import UnifiedLedger
        from sovereign_os.models.charter import Charter, load_charter

        root = os.getenv("SOVEREIGN_DATA_DIR", ".sovereign-mcp")
        os.makedirs(root, exist_ok=True)

        path = _charter_path()
        charter = load_charter(path) if path else Charter(
            mission="Governed agent execution over MCP.")

        self._ledger = UnifiedLedger(persist_path=os.path.join(root, "ledger.jsonl"))
        self._auth = SovereignAuth(persist_path=os.path.join(root, "trust.json"))
        self._broker = DelegationBroker(eligibility=self._auth.check_permission_for)
        set_broker(self._broker)

        self._calibrator = CostCalibrator.load(os.path.join(root, "calibration.json"))
        self._engine = GovernanceEngine(
            charter, self._ledger, auth=self._auth,
            calibrator=self._calibrator, delegation_broker=self._broker,
        )
        self._root = root

    @property
    def engine(self):
        self._build()
        return self._engine

    @property
    def ledger(self):
        self._build()
        return self._ledger

    def save(self) -> None:
        """Persist what this call taught the estimator, so the next one is better."""
        try:
            self._calibrator.save(os.path.join(self._root, "calibration.json"))
        except Exception:  # noqa: BLE001 - never fail a completed call over bookkeeping
            logger.debug("MCP server: calibration save failed", exc_info=True)


_RUNTIME = GovernedRuntime()


# --------------------------------------------------------------------------- tools

async def forecast_cost(goal: str, category: str = "general") -> dict:
    """What this work is expected to cost, before any of it is done."""
    from sovereign_os.governance.cost_model import cost_stats
    from sovereign_os.governance.economics import complexity_from_goal, estimate_task_cost_cents

    cat = (category or "general").strip().lower()
    complexity = complexity_from_goal(goal or "")
    point = estimate_task_cost_cents(cat, complexity=complexity, calibrated=True)
    raw = estimate_task_cost_cents(cat, complexity=complexity, calibrated=False)
    stats = cost_stats(cat)

    return {
        "estimate_cents": point,
        # Reported only with a measured spread behind it. An interval invented from two
        # samples reads as precision the system does not have, which is worse than none
        # for a number someone is about to trust.
        "upper_cents_p80": (max(point, int(round(raw * stats.safety_multiplier(0.8))))
                            if stats.n >= 3 else None),
        "calibrated_on_samples": stats.n,
        "within_2x": stats.within_2x if stats.n else None,
        "note": (
            "Estimates are uncalibrated until tasks settle in this category."
            if stats.n < 3 else
            f"Calibrated on {stats.n} settled tasks in '{cat}'."
        ),
    }


async def run_governed(goal: str, max_repair_attempts: int = 0) -> dict:
    """
    Run a goal under governance: budget check, scoped authority, audit on the result.

    Returns what was produced together with what it cost and what authorized it, because
    a result without its cost and provenance is the thing this project exists to stop
    shipping.
    """
    engine = _RUNTIME.engine
    before = _spend(_RUNTIME.ledger)
    try:
        plan, results, reports = await engine.run_mission_with_audit(
            goal, abort_on_audit_failure=False, max_repair_attempts=max_repair_attempts)
    except Exception as exc:  # noqa: BLE001 - a refusal is an answer, not a crash
        return {"ok": False, "error": str(exc),
                "hint": "A budget or permission check refused this. Raise the ceiling in "
                        "the charter if that is genuinely intended — not from here."}
    _RUNTIME.save()

    spent = max(0, _spend(_RUNTIME.ledger) - before)
    return {
        "ok": True,
        "tasks": [{"task_id": r.task_id, "success": r.success,
                   "output": (r.output or "")[:20000]} for r in (results or [])],
        "audit": [{"task_id": a.task_id, "passed": a.passed, "score": a.score,
                   "reason": a.reason} for a in (reports or [])],
        "all_passed": all(getattr(a, "passed", False) for a in reports) if reports else True,
        "cost_cents": spent,
        "plan_tasks": len(getattr(plan, "tasks", []) or []),
    }


async def audit_receipt(limit: int = 10) -> dict:
    """
    The recent audit trail, with the proof hashes that make it checkable.

    Returned so a caller can verify independently rather than being asked to believe a
    summary — which is the only version of an audit trail that is worth anything.
    """
    root = os.getenv("SOVEREIGN_DATA_DIR", ".sovereign-mcp")
    path = os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH", os.path.join(root, "audit.jsonl"))
    if not os.path.exists(path):
        return {"entries": [], "note": "No audit trail yet; run something first."}

    lines = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                lines.append(line)
    entries = []
    for line in lines[-max(1, int(limit)):]:
        try:
            entries.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return {
        "entries": entries,
        "total": len(lines),
        "path": path,
        "verify": "Each entry's proof_hash is a SHA-256 over its canonical content; "
                  "recompute it from the entry to check the record was not amended.",
    }


async def governance_status() -> dict:
    """The limits currently in force, and how well the estimator is doing against them."""
    from sovereign_os.agents.delegation_gate import strict_delegation
    from sovereign_os.governance.cost_model import calibration_report

    engine = _RUNTIME.engine
    charter = getattr(engine, "_charter", None)
    report = calibration_report()
    return {
        "charter_mission": getattr(charter, "mission", "") if charter else "",
        "balance_cents": _RUNTIME.ledger.total_usd_cents(),
        "strict_delegation": strict_delegation(),
        "calibration": report.get("overall", {}),
        "categories": sorted(report.get("by_category", {})),
    }


def _spend(ledger) -> int:
    try:
        return int(ledger.total_token_estimated_usd_cents())
    except Exception:  # noqa: BLE001
        return 0


TOOLS: dict[str, dict] = {
    "forecast_cost": {
        "fn": forecast_cost,
        "description": "Estimate what a goal will cost before running it, with a "
                       "confidence band once enough tasks have settled to measure one.",
        "schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What you want done."},
                "category": {"type": "string",
                             "description": "coding | research | writing | data | design | automation"},
            },
            "required": ["goal"],
        },
    },
    "run_governed": {
        "fn": run_governed,
        "description": "Run a goal under governance: budget check before compute, "
                       "task-scoped authority, rubric audit on the result. Returns the "
                       "work together with what it cost.",
        "schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "max_repair_attempts": {"type": "integer",
                                        "description": "Re-run a failed task this many times."},
            },
            "required": ["goal"],
        },
    },
    "audit_receipt": {
        "fn": audit_receipt,
        "description": "Recent audit entries with their proof hashes, so the record can "
                       "be verified rather than taken on trust.",
        "schema": {"type": "object",
                   "properties": {"limit": {"type": "integer"}}},
    },
    "governance_status": {
        "fn": governance_status,
        "description": "Current budget, delegation posture, and how well cost estimates "
                       "have been tracking reality.",
        "schema": {"type": "object", "properties": {}},
    },
}


async def dispatch(name: str, arguments: dict | None = None) -> Any:
    """Invoke a tool by name. Shared by the protocol server and the tests."""
    tool = TOOLS.get(name)
    if tool is None:
        raise KeyError(f"unknown tool {name!r}; have {sorted(TOOLS)}")
    return await tool["fn"](**(arguments or {}))


def build_server():
    """
    Construct the MCP server over stdio.

    Imported lazily so the module stays importable — and testable — without the `mcp`
    package present, which keeps the tool logic above exercisable in the normal suite.
    """
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(
            "The MCP server needs the `mcp` package: pip install 'sovereign-os[mcp]'"
        ) from exc

    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            Tool(name=name, description=spec["description"], inputSchema=spec["schema"])
            for name, spec in TOOLS.items()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None = None) -> list:
        try:
            result = await dispatch(name, arguments)
        except Exception as exc:  # noqa: BLE001 - a refusal is a result the model can act on
            result = {"ok": False, "error": str(exc)}
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    return server


def main() -> int:  # pragma: no cover - entrypoint
    import mcp.server.stdio

    logging.basicConfig(level=logging.WARNING)

    async def _run():
        server = build_server()
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
