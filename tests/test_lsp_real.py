"""LspProvider tests against the REAL serena contract.

The old suite mocked ``_call_tool`` with fabricated tool names/args, which is exactly what
hid the drift (``uvx serena`` doesn't exist; tools take ``name_path_pattern`` /
``name_path``+``relative_path``, never ``project_root``). These tests instead:
  1. pin the exact launch argv (the thing that was wrong),
  2. drive the two-step symbol flow with CAPTURED real serena responses and assert the
     second call receives the located symbol's real ``relative_path``,
  3. fault-inject a real bogus subprocess and assert cooldown + never-raise,
  4. (opt-in) launch serena for real and assert a live definition + references.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Any

import pytest

from codeintel.providers.lsp import LspProvider, _State, _serena_launch_args

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# --------------------------------------------------------------------------- #
# Captured real serena tool responses (trimmed but shape-accurate)
# --------------------------------------------------------------------------- #

FIND_SYMBOL_JSON = (
    '[{"name_path": "safe_null_result", "kind": "Function", '
    '"relative_path": "src/codeintel/provider.py", '
    '"body_location": {"start_line": 30, "end_line": 45}, '
    '"body": "def safe_null_result(op, target):\\n    return {}"}]'
)

FIND_REFS_JSON = (
    '{"src/codeintel/gateway.py": {'
    '"File": [{"name_path": "gateway", "body_location": {"start_line": 0, "end_line": 229}, '
    '"content_around_reference": "...   6:from x import y\\n  >   7:from codeintel.provider import safe_null_result\\n...   8:z"}], '
    '"Method": [{"name_path": "Gateway/_fan_out", "body_location": {"start_line": 55, "end_line": 86}, '
    '"content_around_reference": "...  78:a\\n  >  79:        results[e] = safe_null_result(op)\\n...  80:b"}]}, '
    '"src/codeintel/server.py": {'
    '"Function": [{"name_path": "code_query_handler", "body_location": {"start_line": 62, "end_line": 72}, '
    '"content_around_reference": "...  71:except\\n  >  72:        return safe_null_result()\\n...  73:x"}]}}'
)

OVERVIEW_JSON = '{"Class": ["Result", "CodeProvider"], "Function": ["safe_null_result"]}'


class _FakeText:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeToolResult:
    """Stand-in for mcp CallToolResult — a .content list of text parts, like serena returns."""
    def __init__(self, text: str) -> None:
        self.content = [_FakeText(text)]


def _ready_session() -> Any:
    s = type("S", (), {})()
    s.state = _State.READY
    s.cooldown_until = 0.0
    s._lock = threading.Lock()
    s._loop = None
    s._mcp_session = object()  # non-None so _call_tool would proceed (but we replace it)
    return s


# --------------------------------------------------------------------------- #
# 1 — launch argv contract (the exact drift that was fixed)
# --------------------------------------------------------------------------- #

def test_launch_argv_uvx_matches_real_serena_invocation():
    args = _serena_launch_args("uvx", "/my/repo")
    assert args == [
        "uvx", "--from", "git+https://github.com/oraios/serena", "serena",
        "start-mcp-server", "--context", "ide-assistant",
        "--enable-web-dashboard", "false", "--project", "/my/repo",
    ]
    # The old broken form must be gone.
    assert "--project_root" not in args
    assert not (args[1] == "serena")  # `uvx serena ...` (no --from) never worked


def test_launch_argv_direct_binary():
    args = _serena_launch_args("serena", "/my/repo")
    assert args[0] == "serena"
    assert args[1] == "start-mcp-server"
    assert args[-2:] == ["--project", "/my/repo"]


# --------------------------------------------------------------------------- #
# 2 — two-step symbol flow on captured real responses
# --------------------------------------------------------------------------- #

def test_symbol_two_step_uses_located_relative_path(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    session = _ready_session()
    p._sessions["/repo"] = session

    calls: list[tuple[str, dict]] = []

    def _fake_call_tool(sess, tool, args, timeout_s):
        calls.append((tool, args))
        if tool == "find_symbol":
            return _FakeToolResult(FIND_SYMBOL_JSON)
        if tool == "find_referencing_symbols":
            return _FakeToolResult(FIND_REFS_JSON)
        return None

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)

    r = p.build_result("symbol", "safe_null_result", [], 0, "/repo")
    assert r["ok"] is True and r["engine"] == "lsp"
    body = r["result"]
    assert body is not None

    # find_symbol called with name_path_pattern (NOT name/project_root).
    fs = dict(calls)["find_symbol"]
    assert fs.get("name_path_pattern") == "safe_null_result"
    assert "project_root" not in fs and "name" not in fs

    # find_referencing_symbols called with BOTH required args, using the LOCATED path.
    fr = dict(calls)["find_referencing_symbols"]
    assert fr.get("name_path") == "safe_null_result"
    assert fr.get("relative_path") == "src/codeintel/provider.py"
    assert "project_root" not in fr

    # Rendered output carries the real definition + parsed references (with line numbers).
    assert "**Function** — src/codeintel/provider.py:30-45" in body
    assert "def safe_null_result" in body
    assert "## References (3)" in body
    assert "src/codeintel/gateway.py:7" in body      # line parsed from content_around_reference
    assert "src/codeintel/server.py:72" in body


def test_symbol_definition_only_when_no_references(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/repo"] = _ready_session()

    def _fake_call_tool(sess, tool, args, timeout_s):
        if tool == "find_symbol":
            return _FakeToolResult(FIND_SYMBOL_JSON)
        return _FakeToolResult("{}")  # no referencing symbols

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result("symbol", "safe_null_result", [], 0, "/repo")
    assert r["result"] is not None
    assert "**Function**" in r["result"]
    assert "## References" in r["result"]  # section present, empty


# --------------------------------------------------------------------------- #
# 3 — overview uses relative_path, no project_root
# --------------------------------------------------------------------------- #

def test_overview_parses_symbols_overview(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/repo"] = _ready_session()

    seen: dict = {}

    def _fake_call_tool(sess, tool, args, timeout_s):
        seen[tool] = args
        if tool == "get_symbols_overview":
            return _FakeToolResult(OVERVIEW_JSON)
        return None

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result("overview", "src/codeintel/provider.py", [], 0, "/repo")
    assert r["result"] is not None
    assert seen["get_symbols_overview"] == {"relative_path": "src/codeintel/provider.py"}
    assert "Result, CodeProvider" in r["result"]
    assert "safe_null_result" in r["result"]


# --------------------------------------------------------------------------- #
# 4 — boot failure: real bogus subprocess → cooldown, never raises
# --------------------------------------------------------------------------- #

def test_boot_failure_cools_down_and_never_raises(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    # Point the launcher at a command that cannot spawn — the background warm-up must fail,
    # flip to FAILED with a cooldown, and NOT respawn on the next request.
    p._cmd = "/nonexistent/serena-does-not-exist-xyz"

    r1 = p.build_result("symbol", "x", [], 0, "/repo")  # warming (spawn in flight)
    assert r1["ok"] is True

    sess = p._sessions["/repo"]
    for _ in range(60):
        with sess._lock:
            if sess.state == _State.FAILED:
                break
        time.sleep(0.1)

    with sess._lock:
        assert sess.state == _State.FAILED
        assert sess.cooldown_until > time.monotonic()

    r2 = p.build_result("symbol", "x", [], 0, "/repo")
    assert r2["ok"] is True
    assert r2["result"] is None
    assert r2["reason"] == "boot-failed"
    # No respawn while cooling down — same session object.
    assert p._sessions["/repo"] is sess


# --------------------------------------------------------------------------- #
# 4b — fault injection on the rewritten symbol path (never-raise)
# --------------------------------------------------------------------------- #

def test_symbol_tool_returns_none_degrades(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/repo"] = _ready_session()
    monkeypatch.setattr(p, "_call_tool", lambda *a, **k: None)  # every tool call fails
    r = p.build_result("symbol", "safe_null_result", [], 0, "/repo")
    assert r["ok"] is True
    # No JSON to parse → definition falls back to "(not found)", references empty, no crash.
    assert r["result"] is not None
    assert "(not found)" in r["result"]


def test_symbol_tool_raising_is_caught(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/repo"] = _ready_session()

    def _boom(*a, **k):
        raise RuntimeError("serena call blew up")

    monkeypatch.setattr(p, "_call_tool", _boom)
    r = p.build_result("symbol", "safe_null_result", [], 0, "/repo")
    assert r["ok"] is True  # never-raise holds


# --------------------------------------------------------------------------- #
# 5 — LIVE: launch serena for real (opt-in; heavyweight: uvx + git + LS boot)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    shutil.which("uvx") is None or os.environ.get("CODEINTEL_LIVE_LSP") != "1",
    reason="live LSP test is opt-in: set CODEINTEL_LIVE_LSP=1 (launches serena via uvx)",
)
def test_live_symbol_returns_definition_and_references():
    p = LspProvider()
    assert p.available is True
    deadline = time.monotonic() + 60.0
    result = None
    while time.monotonic() < deadline:
        r = p.build_result("symbol", "safe_null_result", [], 0, REPO_ROOT)
        assert r["ok"] is True  # never raises, even mid-warmup
        if r["result"] is not None:
            result = r["result"]
            break
        assert r["reason"] in ("warming", "boot-failed")
        if r["reason"] == "boot-failed":
            pytest.skip("serena failed to boot in this environment")
        time.sleep(0.5)

    assert result is not None, "serena never returned a definition"
    assert "safe_null_result" in result
    assert "src/codeintel/provider.py" in result
    assert "## References" in result
