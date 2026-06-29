"""Tests for the figma connector + DesignBriefWorker reading a referenced file."""

import json
import pytest

from sovereign_os.agents.base import TaskInput
from sovereign_os.agents.specialist_workers import DesignBriefWorker, _figma_ref
from sovereign_os.connectors import dispatch
from sovereign_os.connectors.figma import figma_get_file, file_key_from, summarize_document


FAKE_DOC = {
    "name": "Design System",
    "document": {"name": "Document", "type": "DOCUMENT", "children": [
        {"name": "Page 1", "type": "CANVAS", "children": [
            {"name": "Button", "type": "COMPONENT", "children": [
                {"name": "Label", "type": "TEXT"}]},
            {"name": "Card", "type": "FRAME"}]}]},
}


class FakeResp:
    def __init__(self, obj): self._b = json.dumps(obj).encode()
    def read(self): return self._b


def test_file_key_from_url_and_key():
    assert file_key_from("https://www.figma.com/file/ABC123/My-Design") == "ABC123"
    assert file_key_from("https://figma.com/design/XYZ789/Thing?node=1") == "XYZ789"
    assert file_key_from("RAWKEY") == "RAWKEY"


def test_summarize_document_outline():
    s = summarize_document(FAKE_DOC["document"])
    assert "Button (COMPONENT)" in s and "Label (TEXT)" in s and "Card (FRAME)" in s


def test_figma_get_file_with_injected_opener():
    r = figma_get_file("https://figma.com/file/ABC/My", opener=lambda url, tok, t: FakeResp(FAKE_DOC))
    assert r["file_key"] == "ABC" and r["name"] == "Design System"
    assert "Button (COMPONENT)" in r["summary"]


def test_figma_requires_token_without_opener(monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert "error" in figma_get_file("ABC")


def test_dispatch_figma():
    r = dispatch("figma", ref="https://figma.com/file/ABC/X", opener=lambda url, tok, t: FakeResp(FAKE_DOC))
    assert r["name"] == "Design System"


def test_figma_ref_extraction():
    assert _figma_ref("Implement https://www.figma.com/file/ABC/Spec please") .startswith("https://www.figma.com/file/ABC")
    assert _figma_ref("no link here") == ""


class ScriptLLM:
    model_name = "fake"
    def __init__(self, replies): self.replies = list(replies); self.turns = 0; self._last_usage = {"input_tokens": 5, "output_tokens": 5}
    async def chat(self, messages):
        self.turns += 1
        return self.replies[min(self.turns - 1, len(self.replies) - 1)]


@pytest.mark.asyncio
async def test_design_worker_reads_referenced_figma(monkeypatch):
    import sovereign_os.connectors as conn
    monkeypatch.setattr(conn, "dispatch", lambda name, **kw: {"name": "DS", "summary": "- Button (COMPONENT)"} if name == "figma" else {"error": "x"})
    llm = ScriptLLM([
        json.dumps({"action": "tool", "tool": "read_figma", "args": {"ref": "https://figma.com/file/ABC/X"}}),
        json.dumps({"action": "final", "output": "## Component Spec\nExtends the existing Button component."}),
    ])
    w = DesignBriefWorker(agent_id="d1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="Extend https://figma.com/file/ABC/X with a new variant",
                                  context={"use_tools": "1"}))
    assert r.success and "Button" in r.output and llm.turns == 2
