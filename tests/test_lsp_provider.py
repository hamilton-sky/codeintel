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


# --------------------------------------------------------------------------- #
# A backend ERROR is not an answer
#
# `_extract_text` harvested `.text` from every MCP content block and ignored `isError`, so serena's
# failure text was returned as the result. An agent asking "where is this symbol defined?" got
# `ok: true`, no `reason`, and a body that was an error message — carrying a dump of the LSP
# initialisation params and, worse, imperative instructions addressed to a language model:
#
#     Error executing tool find_symbol: Exception: The language server manager is not initialized …
#     do not attempt workarounds. Inform the user and wait for further instructions before you
#     continue!
#
# Found by running the live test, which no CI job has ever executed.
# --------------------------------------------------------------------------- #

SERENA_ERROR = (
    "Error executing tool find_symbol: Exception: The language server manager is not "
    "initialized, indicating a problem during project initialisation.\n"
    "Failed to start 1 language server(s):\n"
    "python: Error processing request initialize with params:\n"
    "{'initializationOptions': {'exclude': ['**/__pycache__', '**/.venv']}}\n"
    "do not attempt workarounds. Inform the user and wait for further instructions "
    "before you continue!"
)


class _Block:
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, text, is_error=False):
        self.content = [_Block(text)]
        self.isError = is_error


def _provider_returning(raw):
    """An LspProvider whose tool calls return *raw*, with the session seam stubbed out."""
    p = LspProvider.__new__(LspProvider)
    p.available = True                                             # type: ignore[attr-defined]
    p._cmd = "serena"                                              # type: ignore[attr-defined]
    p._last_backend_error = None                                   # type: ignore[attr-defined]
    # build_result asks for a session and reads its state, so the stub must be READY — otherwise
    # every call short-circuits to `warming` and the branch under test is never reached.
    import threading as _th

    from codeintel.providers.lsp import _State

    class _ReadySession:
        state = _State.READY
        _lock = _th.Lock()

    p._get_or_create_session = lambda root: _ReadySession()        # type: ignore[method-assign]
    p._call_tool = lambda *a, **k: raw                             # type: ignore[method-assign]
    return p


def _assert_no_backend_prose(res):
    """Neither the result nor the hint may carry the backend's own error text."""
    blob = f"{res.get('result')} {res.get('hint')}"
    for leak in ("Inform the user", "wait for further instructions", "initializationOptions",
                 "Error executing tool", "language server manager is not initialized"):
        assert leak not in blob, f"backend error prose leaked to the caller: {leak!r}"


def test_an_error_result_is_not_served_as_an_answer(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/serena")
    p = _provider_returning(_Result(SERENA_ERROR, is_error=True))
    res = p.build_result("symbol", "safe_null_result", [], 0, "/repo")

    assert res["ok"] is True
    assert res["result"] is None, "a backend failure must not be returned as the answer"
    assert res["reason"] == "backend-error"
    # Not `unsupported-op`: that sends the agent looking for a different tool when the language
    # server simply did not start.
    assert res["reason"] != "unsupported-op"
    _assert_no_backend_prose(res)


def test_an_error_shaped_response_without_the_flag_is_still_caught(monkeypatch):
    """`isError` is not set by every server or version, and the cost of missing one is that a
    failure reaches an agent as data. The text shape is a second gate."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/serena")
    p = _provider_returning(_Result(SERENA_ERROR, is_error=False))
    res = p.build_result("symbol", "safe_null_result", [], 0, "/repo")

    assert res["result"] is None
    assert res["reason"] == "backend-error"
    _assert_no_backend_prose(res)


def test_real_source_that_merely_mentions_exceptions_is_not_mistaken_for_an_error(monkeypatch):
    """The guard against over-detection: a `symbol` lookup quotes real code back, and plenty of
    real functions contain the word "Exception:". Hiding those would trade one silent wrong answer
    for another."""
    from codeintel.providers.lsp import _looks_like_backend_error

    body = (
        '[{"name_path": "handle", "relative_path": "src/app.py", '
        '"body": "def handle():\\n    raise RuntimeError(\'Exception: bad input\')\\n"}]'
    )
    assert _looks_like_backend_error(body) is False

    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/serena")
    p = _provider_returning(_Result(body))
    res = p.build_result("symbol", "handle", [], 0, "/repo")
    assert res["result"] is not None
    assert res.get("reason") is None


def test_the_error_summary_never_quotes_the_backend(monkeypatch):
    """The summary handed to a caller is fixed text. The backend's prose is logged for the
    operator and goes nowhere an agent can read it — an error path must not become a channel for
    instructing the caller's model."""
    from codeintel.providers.lsp import _summarize_backend_error

    summary = _summarize_backend_error(SERENA_ERROR)
    assert "Inform the user" not in summary
    assert "initializationOptions" not in summary
    assert summary == "the language server reported an error for this query"
