"""Tests for the web_fetch and code_workspace connectors (no real network/exec)."""

import io
import pytest

from sovereign_os.connectors import dispatch
from sovereign_os.connectors.web import html_to_text, web_fetch
from sovereign_os.connectors.code_workspace import list_files, read_file, run_tests


# ---- web_fetch
class FakeResp:
    status = 200
    def __init__(self, body): self._b = body.encode()
    def read(self, n=-1): return self._b if n < 0 else self._b[:n]


def test_html_to_text_strips_scripts_and_tags():
    raw = "<html><head><style>x{}</style><script>bad()</script></head><body><h1>Hi</h1><p>World &amp; more</p></body></html>"
    t = html_to_text(raw)
    assert "Hi" in t and "World & more" in t and "bad()" not in t and "x{}" not in t


def test_web_fetch_with_injected_opener():
    r = web_fetch("https://example.com", opener=lambda url, timeout: FakeResp("<p>Hello <b>web</b></p>"))
    assert r["status"] == 200 and "Hello web" in r["text"]


def test_web_fetch_rejects_non_http():
    r = web_fetch("file:///etc/passwd")
    assert r["status"] == 0 and "error" in r


def test_dispatch_web_fetch():
    r = dispatch("web_fetch", url="https://x.test", opener=lambda u, t: FakeResp("ok"))
    assert r["status"] == 200 and r["text"] == "ok"


# ---- code_workspace
def test_list_and_read(tmp_path):
    (tmp_path / "a.py").write_text("print('hi')")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("data")
    files = list_files(tmp_path)["files"]
    assert "a.py" in files and "sub/b.txt" in files
    assert read_file(tmp_path, "a.py")["text"] == "print('hi')"


def test_read_file_refuses_path_escape(tmp_path):
    r = read_file(tmp_path, "../../etc/passwd")
    assert r["text"] == "" and "refused" in r["error"]


def test_run_tests_dry_run_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVEREIGN_CODE_EXEC_ENABLED", raising=False)
    r = run_tests(tmp_path)
    assert r["ran"] is False and r["dry_run"] is True


def test_dispatch_code_workspace(tmp_path):
    (tmp_path / "x.py").write_text("y=1")
    r = dispatch("read_file", root=str(tmp_path), relpath="x.py")
    assert r["text"] == "y=1"


def test_write_file_dry_run_and_escape(tmp_path, monkeypatch):
    from sovereign_os.connectors.code_workspace import write_file
    monkeypatch.delenv("SOVEREIGN_CODE_EXEC_ENABLED", raising=False)
    assert write_file(tmp_path, "x.py", "y=1")["dry_run"] is True       # gated
    assert "refused" in write_file(tmp_path, "../escape.py", "x")["error"]


def test_write_file_writes_when_enabled(tmp_path, monkeypatch):
    from sovereign_os.connectors.code_workspace import write_file, read_file
    monkeypatch.setenv("SOVEREIGN_CODE_EXEC_ENABLED", "1")
    r = write_file(tmp_path, "pkg/mod.py", "VALUE = 42\n")
    assert r["written"] is True and (tmp_path / "pkg" / "mod.py").exists()
    assert read_file(tmp_path, "pkg/mod.py")["text"] == "VALUE = 42\n"
