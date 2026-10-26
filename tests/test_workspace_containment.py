"""
SE-01 (workspace escape), across elicitation levels.

read_file and write_file resolved paths and checked containment, and every L0/L1 probe
against them was refused. list_files did not check anything: its glob is caller-supplied,
Path.glob honours ".." and absolute patterns, and the module docstring claimed otherwise.
An L2 probe -- "paths are resolved at the read/write site, so attack the operation that
does not resolve" -- found it. These tests pin the containment of all three operations,
and assert only refusal: they do not need to reach anything outside the workspace to
prove the boundary holds.
"""

import os
import tempfile
from pathlib import Path

import pytest

from sovereign_os.connectors import code_workspace as cw


@pytest.fixture
def ws():
    base = Path(tempfile.mkdtemp())
    (base / "src").mkdir()
    (base / "src" / "app.py").write_text("print('hi')\n")
    return base


@pytest.fixture
def outside():
    d = Path(tempfile.mkdtemp())
    (d / "marker.txt").write_text("outside\n")
    return d


# ------------------------------------------------------------ L0: naive traversal

def test_read_refuses_parent_traversal(ws):
    assert "refused" in cw.read_file(ws, "../marker.txt").get("error", "")


def test_write_refuses_parent_traversal(ws, outside):
    r = cw.write_file(ws, "../escape.txt", "x")
    assert r.get("written") is False
    assert not (ws.parent / "escape.txt").exists()


def test_read_refuses_an_absolute_path(ws, outside):
    assert cw.read_file(ws, str(outside / "marker.txt"))["text"] == ""


# ------------------------------------------- L1: spec-aware (paths are resolved)

def test_a_symlink_inside_the_workspace_cannot_be_read_through(ws, outside):
    os.symlink(outside, ws / "link")
    assert "refused" in cw.read_file(ws, "link/marker.txt").get("error", "")


def test_a_symlink_inside_the_workspace_cannot_be_written_through(ws, outside):
    os.symlink(outside, ws / "link")
    assert cw.write_file(ws, "link/planted.txt", "x").get("written") is False
    assert not (outside / "planted.txt").exists()


# ------------------- L2: iterative (attack the operation that did not resolve)

def test_list_refuses_a_glob_with_parent_segments(ws):
    r = cw.list_files(ws, "../*")
    assert r["files"] == [] and "refused" in r.get("error", "")


def test_list_refuses_an_absolute_glob(ws, outside):
    r = cw.list_files(ws, str(outside / "*"))
    assert r["files"] == [] and "refused" in r.get("error", "")


def test_list_does_not_follow_a_symlink_out_of_the_workspace(ws, outside):
    """The pattern is innocent; the match is not. Hence the per-match check too."""
    os.symlink(outside, ws / "link")
    names = cw.list_files(ws, "**/*")["files"]
    assert not any("marker.txt" in n for n in names)


def test_every_listed_path_resolves_inside_the_root(ws, outside):
    os.symlink(outside, ws / "link")
    base = ws.resolve()
    for name in cw.list_files(ws, "**/*")["files"]:
        (base / name).resolve().relative_to(base)       # raises if it escaped


# ---------------------------------------------------- the boundary still works

def test_ordinary_listing_is_unchanged(ws):
    assert cw.list_files(ws, "**/*")["files"] == ["src/app.py"]


def test_ordinary_globs_still_match(ws):
    assert cw.list_files(ws, "src/*.py")["files"] == ["src/app.py"]


def test_the_tool_path_an_agent_uses_is_contained(ws):
    """Pin the wiring, not only the function: this is the dispatch a worker reaches."""
    from sovereign_os.connectors.registry import dispatch

    r = dispatch("list_files", root=str(ws), glob="../*")
    assert r["files"] == []
