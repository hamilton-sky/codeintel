"""LspProvider tests: never-raise invariant and state-machine correctness."""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

from codeintel.outcome import Missing, Ok
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
    # A WARMING session is now WAITED for rather than declined outright. This double stands in for
    # a boot that does not settle within the wait, which is what keeps these tests asserting the
    # states they were written to assert; the wait itself is covered separately below.
    s.settled = threading.Event()
    s.wait_until_settled.return_value = state
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

        def wait_until_settled(self, timeout_s):
            return self.state  # a boot still in flight when the wait expires

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
            return Ok("def parse_result(x): ...")
        if tool == "find_referencing_symbols":
            return Ok("main.py:10")
        return Missing("backend-error", "unstubbed tool")

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
    p._call_tool = lambda *a, **k: Ok(raw)                         # type: ignore[method-assign]
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


# ---------------------------------------------------------------------------
# Warming: a first call must not silently forfeit the LSP engine
# ---------------------------------------------------------------------------

def test_a_warming_session_is_waited_for_rather_than_declined():
    """The LSP was effectively unavailable on the FIRST call of every session — the one an agent
    makes when it starts on a repo. `callers`/`context` fold the LSP in as the cross-check behind
    their `[?…]` unverified badges, so the engine that would confirm them had always just declined.
    Dogfooding on three repos never once saw the cross-check arrive."""
    import threading

    from codeintel.providers import lsp as lsp_mod

    session = MagicMock()
    session._lock = threading.Lock()
    session.state = lsp_mod._State.WARMING

    def _settle(timeout_s):
        session.state = lsp_mod._State.READY
        return lsp_mod._State.READY

    session.wait_until_settled.side_effect = _settle

    provider = LspProvider.__new__(LspProvider)
    provider.available = True
    provider._get_or_create_session = lambda root: session
    provider._clear_backend_error = lambda: None
    provider._dispatch = lambda *a, **k: "## Symbol: thing"
    provider._pending_gaps = ()

    out = provider.build_result("symbol", "thing", "auto", 0, "/repo")

    session.wait_until_settled.assert_called_once()
    assert out.get("result") == "## Symbol: thing"
    assert out.get("reason") != "warming"


def test_a_boot_that_never_settles_still_degrades_to_the_warming_safe_null():
    """The wait is a bounded courtesy, not a promise. A genuinely slow boot (a cold `uvx` still
    downloading serena-agent) must return the same safe null it always did rather than hold the
    agent's call open."""
    import threading

    from codeintel.providers import lsp as lsp_mod

    session = MagicMock()
    session._lock = threading.Lock()
    session.state = lsp_mod._State.WARMING
    session.wait_until_settled.return_value = lsp_mod._State.WARMING

    provider = LspProvider.__new__(LspProvider)
    provider.available = True
    provider._get_or_create_session = lambda root: session
    provider._pending_gaps = ()

    out = provider.build_result("symbol", "thing", "auto", 0, "/repo")

    assert out.get("result") is None
    assert out.get("reason") == "warming"


def test_the_wait_is_bounded_by_the_callers_own_timeout():
    """A caller that granted a short budget must not be held for the full `_WARM_WAIT_S`."""
    import threading

    from codeintel.providers import lsp as lsp_mod

    session = MagicMock()
    session._lock = threading.Lock()
    session.state = lsp_mod._State.WARMING
    session.wait_until_settled.return_value = lsp_mod._State.WARMING

    provider = LspProvider.__new__(LspProvider)
    provider.available = True
    provider._get_or_create_session = lambda root: session
    provider._pending_gaps = ()

    provider.build_result("symbol", "thing", "auto", 1000, "/repo")  # 1s budget

    waited = session.wait_until_settled.call_args[0][0]
    assert waited <= 1.0, f"a 1s budget must not wait {waited}s"


def test_settled_is_set_on_a_failed_boot_so_a_waiter_is_not_stranded():
    """A waiter that is never woken burns its whole timeout on a session that already settled."""
    import threading

    from codeintel.providers import lsp as lsp_mod

    sess = lsp_mod._LspSession.__new__(lsp_mod._LspSession)
    sess.state = lsp_mod._State.WARMING
    sess.cooldown_until = 0.0
    sess._lock = threading.Lock()
    sess.settled = threading.Event()
    sess._loop = MagicMock()

    def _boom(*a, **k):
        raise RuntimeError("boot failed")

    sess._loop.run_until_complete.side_effect = _boom
    sess._run("/repo", "uvx")

    assert sess.settled.is_set()
    assert sess.wait_until_settled(0.01) is lsp_mod._State.FAILED
