"""Tests for the `sovereign categories` and `sovereign connectors` CLI commands."""

import sys
from sovereign_os import cli


def _run(argv, capsys):
    old = sys.argv
    sys.argv = ["sovereign"] + argv
    try:
        rc = cli.main()
    finally:
        sys.argv = old
    return rc, capsys.readouterr().out


def test_categories_cmd(capsys):
    rc, out = _run(["categories"], capsys)
    assert rc == 0
    assert "coding" in out and "code_assistant" in out and "write_files" in out


def test_connectors_cmd(capsys):
    rc, out = _run(["connectors"], capsys)
    assert rc == 0
    assert "MCP servers" in out and "git" in out
