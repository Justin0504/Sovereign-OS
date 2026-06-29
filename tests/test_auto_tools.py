"""Engine auto-enables the worker tool loop by category when SOVEREIGN_AUTO_TOOLS is on."""

from sovereign_os.agents.worker_tools import auto_tool_context


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_AUTO_TOOLS", raising=False)
    assert auto_tool_context("research") == {}
    assert auto_tool_context("code_assistant") == {}


def test_enables_tool_categories(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_AUTO_TOOLS", "true")
    monkeypatch.delenv("SOVEREIGN_WORKSPACE_ROOT", raising=False)
    assert auto_tool_context("research")["use_tools"] == "1"
    assert auto_tool_context("data_analysis")["use_tools"] == "1"
    assert auto_tool_context("design_brief")["use_tools"] == "1"
    # writing/general are not tool categories
    assert auto_tool_context("write_article") == {}
    assert auto_tool_context("summarize") == {}


def test_injects_workspace_root_for_coding(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_AUTO_TOOLS", "1")
    monkeypatch.setenv("SOVEREIGN_WORKSPACE_ROOT", "/repo")
    ctx = auto_tool_context("code_assistant")
    assert ctx["use_tools"] == "1" and ctx["workspace_root"] == "/repo"
    # research gets use_tools but not a workspace root
    assert "workspace_root" not in auto_tool_context("research")
