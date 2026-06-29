"""Tests for the image_gen connector (dry-run gating + injected generator)."""

import json
import pytest

from sovereign_os.agents.base import TaskInput
from sovereign_os.agents.specialist_workers import DesignBriefWorker
from sovereign_os.connectors import dispatch
from sovereign_os.connectors.image_gen import generate_image


def test_dry_run_without_key(monkeypatch):
    monkeypatch.delenv("IMAGE_GEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = generate_image("a red logo")
    assert r["dry_run"] is True and r["prompt"] == "a red logo"


def test_empty_prompt():
    assert "error" in generate_image("")


def test_injected_generator_returns_url():
    r = generate_image("a blue icon", generator=lambda p, s: "https://img.test/x.png")
    assert r["url"] == "https://img.test/x.png" and r["size"] == "1024x1024"


def test_dispatch_image_gen():
    r = dispatch("image_gen", prompt="logo", generator=lambda p, s: "https://img/y.png")
    assert r["url"] == "https://img/y.png"


class ScriptLLM:
    model_name = "fake"
    def __init__(self, replies): self.replies = list(replies); self.turns = 0; self._last_usage = {"input_tokens": 5, "output_tokens": 5}
    async def chat(self, messages):
        self.turns += 1
        return self.replies[min(self.turns - 1, len(self.replies) - 1)]


@pytest.mark.asyncio
async def test_design_worker_can_generate_image(monkeypatch):
    import sovereign_os.connectors as conn
    monkeypatch.setattr(conn, "dispatch", lambda name, **kw: {"url": "https://img/logo.png"} if name == "image_gen" else {"error": "x"})
    llm = ScriptLLM([
        json.dumps({"action": "tool", "tool": "generate_image", "args": {"prompt": "minimal fox logo"}}),
        json.dumps({"action": "final", "output": "## Logo\nDelivered: https://img/logo.png"}),
    ])
    w = DesignBriefWorker(agent_id="d1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="Design a fox logo", context={"use_tools": "1"}))
    assert r.success and "logo.png" in r.output and llm.turns == 2
