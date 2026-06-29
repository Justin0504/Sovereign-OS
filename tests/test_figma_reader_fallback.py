"""Pluggable Figma reader: an alternate backend (e.g. MCP bridge) serves without a token."""

import pytest
from sovereign_os.connectors.figma import figma_get_file, is_configured, set_figma_reader


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_figma_reader(None)  # never leak global state across tests


def test_registered_reader_serves_without_token(monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    set_figma_reader(lambda ref: {"name": "Via MCP", "summary": "- Frame (FRAME)"})
    assert is_configured() is True
    r = figma_get_file("https://figma.com/file/ABC/X")
    assert r["name"] == "Via MCP" and r["file_key"] == "ABC" and "Frame" in r["summary"]


def test_reader_error_falls_back_to_token_message(monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    set_figma_reader(lambda ref: {"error": "mcp unavailable"})
    r = figma_get_file("ABC")
    assert "error" in r and "no Figma reader" in r["error"]


def test_no_reader_no_token_errors(monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert is_configured() is False
    assert "error" in figma_get_file("ABC")
