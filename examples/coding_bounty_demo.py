"""
End-to-end coding-bounty demo: a TaskBounty-shaped bug-fix runs through the whole
governed agentic loop — categorize -> CFO budget gate -> CodeAssistantWorker tool
loop (read -> write fix -> run tests -> submit PR) -> Auditor.

Real file writes + real pytest in a temp repo; the LLM is SCRIPTED (no API key)
and the PR is dry-run (no real git push). Shows the full path a real bug-fix bounty
would take.

  python examples/coding_bounty_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from sovereign_os.agents.base import TaskInput
from sovereign_os.agents.categories import categorize
from sovereign_os.agents.code_workers import CodeAssistantWorker
from sovereign_os.auditor import ReviewEngine
from sovereign_os.auditor.review_engine import StubAuditor
from sovereign_os.governance.budget_policy import CategoryBudgetPolicy
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, FiscalBoundaries

# A buggy repo: add() subtracts; its test fails until fixed.
BUGGY = "def add(a, b):\n    return a - b  # BUG: should add\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


class ScriptedAgent:
    """Stands in for the LLM: drives the tool loop deterministically."""
    model_name = "scripted"
    def __init__(self):
        self.turns = 0
        self._last_usage = {"input_tokens": 50, "output_tokens": 30}
        self._script = [
            {"action": "tool", "tool": "read_file", "args": {"relpath": "calc.py"}},
            {"action": "tool", "tool": "write_file", "args": {"relpath": "calc.py", "content": FIXED}},
            {"action": "tool", "tool": "run_tests", "args": {"cmd": ["python", "-m", "pytest", "-q"]}},
            {"action": "tool", "tool": "submit_pr", "args": {"branch": "fix/add-bug", "title": "Fix add() to add instead of subtract"}},
            {"action": "final", "output": "## Fix\nadd() now returns a+b; the test passes. PR opened on branch fix/add-bug."},
        ]
    async def chat(self, messages):
        i = min(self.turns, len(self._script) - 1)
        self.turns += 1
        return json.dumps(self._script[i])


async def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "calc.py").write_text(BUGGY)
        (repo / "test_calc.py").write_text(TEST)
        # Trusted temp repo -> allow execution (writes + pytest run for real).
        os.environ["SOVEREIGN_CODE_EXEC_ENABLED"] = "1"

        task_title = "Bug: add() subtracts instead of adding"
        print("=== CODING BOUNTY (TaskBounty-shaped) ===")
        print(f"task: {task_title}  ($50, category 'Bug Fix')\n")

        # 1) Categorize -> routes to the coding worker.
        cat = categorize("Bug Fix", task_title)
        print(f"[route] category={cat.key} -> worker skill={cat.skill}")

        # 2) CFO budget gate.
        charter = Charter(mission="Fix bugs.", fiscal_boundaries=FiscalBoundaries(daily_burn_max_usd=50.0))
        ledger = UnifiedLedger(); ledger.record_usd(1000)
        treasury = Treasury(charter, ledger, budget_policy=CategoryBudgetPolicy())
        try:
            treasury.approve_task(50, task_id="bounty", skill=cat.skill)  # ~$0.50 est within coding ceiling
            print("[budget] CFO approved (within coding category ceiling)")
        except Exception as e:
            print(f"[budget] rejected: {e}"); return

        # 3) Agentic loop: read -> write fix -> run tests -> submit PR.
        worker = CodeAssistantWorker(agent_id="coding-1", system_prompt="", llm=ScriptedAgent())
        result = await worker.execute(TaskInput(
            task_id="bounty", description=task_title, required_skill="code_assistant",
            context={"use_tools": "1", "workspace_root": str(repo)},
        ))
        print(f"[agent] tool calls: {result.metadata.get('tool_calls')}  -> {result.output[:80]!r}")

        # Did the fix actually land + tests pass?
        from sovereign_os.connectors.code_workspace import run_tests, read_file
        fixed = "a + b" in read_file(repo, "calc.py")["text"]
        tests = run_tests(repo, ["python", "-m", "pytest", "-q"])
        print(f"[verify] file fixed: {fixed} | tests pass: {tests.get('passed')}")

        # 4) Audit.
        review = ReviewEngine(charter, judge=StubAuditor())
        from sovereign_os.governance.strategist import PlannedTask
        rep = await review.audit_task(
            PlannedTask(task_id="bounty", description=task_title, dependencies=[], required_skill="code_assistant",
                        estimated_token_budget=0, priority="high"),
            result,
        )
        print(f"[audit] passed={rep.passed} score={rep.score}")
        print("\nEnd-to-end: routed -> budget-gated -> agent read+fixed+tested -> PR (dry-run) -> audited.")


if __name__ == "__main__":
    asyncio.run(main())
