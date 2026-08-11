"""LspProvider tests: never-raise invariant and state-machine correctness."""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

from codeintel.providers.lsp import LspProvider, _State
from codeintel.server import code_status_handler


def _make_fake_session(state: _State, cooldown_until: float = 0.0) -> Any:
    """Return a minimal session stand-in with the fields build_result reads."""
    s = MagicMock()
    s.state = state
    s.cooldown_until = cooldown_until
    s._lock = threading.Lock()
    s._loop = None
    s._mcp_session = None
    return s


# ---------------------------------------------------------------------------
# Group 1 — Never-raise: None args
# ---------------------------------------------------------------------------

def test_lsp_provider_none_args(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: None)
    p = LspProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 2 — Never-raise: wrong types
# ---------------------------------------------------------------------------

def test_lsp_provider_wrong_types(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: None)
    p = LspProvider()
    r = p.build_result(123, [], {}, "bad", object())
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 3 — Backend unavailable
# ---------------------------------------------------------------------------

def test_lsp_provider_unavailable(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: None)
    p = LspProvider()
    assert p.available is False
    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "engine-unavailable"


# ---------------------------------------------------------------------------
# Group 4 — WARMING state
# ---------------------------------------------------------------------------

def test_lsp_provider_warming(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.WARMING)
    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "warming"


# ---------------------------------------------------------------------------
# Group 5 — FAILED / cooldown active
# ---------------------------------------------------------------------------

def test_lsp_provider_boot_failed_during_cooldown(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(
        _State.FAILED, cooldown_until=time.monotonic() + 60
    )
    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "boot-failed"


# ---------------------------------------------------------------------------
# Group 6 — Cooldown expiry triggers new WARMING
# ---------------------------------------------------------------------------

def test_lsp_provider_cooldown_expiry(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")

    # Prevent real thread from starting by patching _LspSession.__init__
    started = []

    class FakeNewSession:
        state = _State.WARMING
        cooldown_until = 0.0
        _lock = threading.Lock()
        _loop = None
        _mcp_session = None

        def __init__(self, project_root, cmd):
            started.append(project_root)

    monkeypatch.setattr("codeintel.providers.lsp._LspSession", FakeNewSession)

    p = LspProvider()
    # Inject an expired FAILED session
    p._sessions["/my/repo"] = _make_fake_session(
        _State.FAILED, cooldown_until=time.monotonic() - 1
    )
    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    # A fresh WARMING session was created
    assert r["ok"] is True
    assert r["reason"] == "warming"
    assert "/my/repo" in started


# ---------------------------------------------------------------------------
# Group 7 — READY state with mocked MCP call
# ---------------------------------------------------------------------------

def test_lsp_provider_ready_symbol(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    fake = _make_fake_session(_State.READY)
    p._sessions["/my/repo"] = fake

    def _fake_call_tool(session, tool, args, timeout_s):
        if tool == "find_symbol":
            return "def parse_result(x): ..."
        if tool == "find_referencing_symbols":
            return "main.py:10"
        return None

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    monkeypatch.setattr(p, "_extract_text", lambda raw: raw if isinstance(raw, str) else None)

    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["engine"] == "lsp"
    assert r["result"] is not None
    assert "parse_result" in r["result"]


# ---------------------------------------------------------------------------
# Group 8 — Unsupported op when READY
# ---------------------------------------------------------------------------

def test_lsp_provider_unsupported_op(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    r = p.build_result("callers", "foo", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "unsupported-op"


# ---------------------------------------------------------------------------
# Group 9 — Server status with available LspProvider
# ---------------------------------------------------------------------------

def test_code_status_with_lsp(monkeypatch):
    # uvx present (lsp available), codebase-memory-mcp absent (graph unavailable)
    def _which(cmd):
        return "/fake/uvx" if cmd == "uvx" else None

    monkeypatch.setattr("shutil.which", _which)
    r = code_status_handler({})
    assert r["ok"] is True
    assert "lsp" in r["engines"]


# ---------------------------------------------------------------------------
# Group 10 — Server status with unavailable LspProvider
# ---------------------------------------------------------------------------

def test_code_status_without_lsp(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: None)
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    r = code_status_handler({})
    assert r["ok"] is True
    assert "lsp" not in r["engines"]
