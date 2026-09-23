"""LspProvider tests: never-raise invariant and state-machine correctness."""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from codeintel.outcome import Missing, Ok
from codeintel.providers.lsp import LspProvider, _split_target_file_hint, _State
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
    # Read by `_boot_failed_hint` on the FAILED path. A MagicMock would answer both of these with
    # a Mock, which is not what a real session hands that method.
    s.attempt = 1
    s.boot_error = None
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


def test_warming_says_when_to_ask_again_and_what_can_answer_now(monkeypatch):
    """A bare `warming` is a dead end, and it was read as one.

    It says the engine did not answer and nothing about whether asking again would help, how long
    that would take, or what can answer meanwhile — so an evaluator hit it on the first call of a
    session, recorded "I moved on and never used the LSP engine", and did exactly that.

    This was an inconsistency rather than a gap in the design: `_WARM_WAIT_S`'s own note says
    degrading to `warming` is the right answer for a cold `uvx` *with* `retry_after_s` in the
    envelope, and the `boot-failed` branch below has always carried one. Only this branch — the
    one on the common path, reached on the first call of every session — was left bare."""
    from codeintel.providers.lsp import _WARM_WAIT_S

    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.WARMING)
    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")

    assert r["reason"] == "warming"
    assert r["retry_after_s"] == _WARM_WAIT_S
    hint = r["hint"]
    assert "not a statement about your code" in hint, hint
    assert "graph" in hint, "the engine that can answer right now must be named"


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

        def __init__(self, project_root, cmd, attempt=1):
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


def test_file_qualified_symbol_selects_the_matching_definition(monkeypatch):
    """A graph file hint must survive the LSP cross-check instead of selecting a same-name method."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    calls: list[tuple[str, dict]] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append((tool, args))
        if tool == "find_symbol":
            return Ok('[{"name_path":"Other/createSession","kind":"Method",'
                      '"relative_path":"src/other.ts","body_location":{"start_line":1,'
                      '"end_line":2},"body":"wrong"},{"name_path":"WsSessionHandler/createSession",'
                      '"kind":"Method","relative_path":"backend/src/session.handler.ts",'
                      '"body_location":{"start_line":138,"end_line":177},"body":"right"}]')
        if tool == "find_referencing_symbols":
            assert args["name_path"] == "WsSessionHandler/createSession"
            assert args["relative_path"] == "backend/src/session.handler.ts"
            return Ok('{"backend/src/gateway.ts":{"Method":[{"name_path":"Gateway/routeMessage",'
                      '"content_around_reference":"> 612: await handler.createSession()"}]}}')
        return Missing("backend-error", "unstubbed tool")

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "createSession@backend/src/session.handler.ts", [], 30000, "/my/repo"
    )

    assert r["confidence"] == "complete"
    assert "right" in r["result"] and "wrong" not in r["result"]
    assert "backend/src/gateway.ts:613" in r["result"]
    assert calls[0][1]["name_path_pattern"] == "createSession"
    assert calls[0][1]["max_matches"] == 500


def test_qualified_and_file_hinted_symbol_uses_leaf_name_and_exact_file(monkeypatch):
    """Graph-qualified names must not disable the exact LSP reference cross-check."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    calls: list[tuple[str, dict]] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append((tool, args))
        if tool == "find_symbol":
            return Ok('[{"name_path":"Other/resolve","kind":"Method",'
                      '"relative_path":"src/strategy-chain.ts","body":"same-file-wrong"},'
                      '{"name_path":"StrategyChain/resolve","kind":"Method",'
                      '"relative_path":"src/strategy-chain.ts","body":"right"}]')
        assert tool == "find_referencing_symbols"
        assert args["name_path"] == "StrategyChain/resolve"
        return Ok("{}")

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "StrategyChain.resolve@src/strategy-chain.ts", [], 30000, "/my/repo"
    )

    assert calls[0][1]["name_path_pattern"] == "resolve"
    assert "relative_path" not in calls[0][1]
    assert calls[0][1]["max_matches"] == 500
    assert "right" in r["result"] and "same-file-wrong" not in r["result"]
    assert "References (0)" in r["result"]


def test_suffix_file_hint_is_not_passed_to_serena_as_an_exact_path(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    seen: list[dict] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        if tool == "find_symbol":
            seen.append(args)
            return Ok('[{"name_path":"StrategyChain/resolve","kind":"Method",'
                      '"relative_path":"src/api/strategy-chain.ts","body":"right"}]')
        return Ok("{}")

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "StrategyChain.resolve@api/strategy-chain.ts", [], 30000, "/my/repo"
    )

    assert "relative_path" not in seen[0]
    assert "right" in r["result"]


def test_nested_container_name_path_is_accepted_when_fully_matched(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)

    def _fake_call_tool(session, tool, args, timeout_s):
        if tool == "find_symbol":
            return Ok('[{"name_path":"Outer/Inner/run","kind":"Method",'
                      '"relative_path":"src/x.py","body":"right"}]')
        return Ok("{}")

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result("symbol", "Outer.Inner.run@src/x.py", [], 30000, "/my/repo")

    assert "right" in r["result"]




def test_qualified_method_does_not_accept_a_top_level_leaf_in_the_file(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)

    def _fake_call_tool(session, tool, args, timeout_s):
        return Ok('[{"name_path":"resolve","kind":"Function",'
                  '"relative_path":"src/x.ts","body":"wrong"}]')

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result("symbol", "MissingClass.resolve@src/x.ts", [], 30000, "/my/repo")

    assert "no definition matching the qualified symbol" in r["result"]
    assert "wrong" not in r["result"]
    assert "References — not retrieved" in r["result"]


def test_qualified_method_does_not_accept_unverified_module_prefix(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)

    def _fake_call_tool(session, tool, args, timeout_s):
        return Ok('[{"name_path":"StrategyChain/resolve","kind":"Method",'
                  '"relative_path":"src/core/strategy-chain.ts","body":"wrong"}]')

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "other.StrategyChain.resolve@src/core/strategy-chain.ts", [], 30000, "/my/repo"
    )

    assert "no definition matching the qualified symbol" in r["result"]
    assert "wrong" not in r["result"]


def test_ambiguous_suffix_file_hint_does_not_choose_an_arbitrary_definition(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    calls: list[str] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append(tool)
        return Ok('[{"name_path":"StrategyChain/resolve","kind":"Method",'
                  '"relative_path":"frontend/strategy-chain.ts","body":"one"},'
                  '{"name_path":"StrategyChain/resolve","kind":"Method",'
                  '"relative_path":"backend/strategy-chain.ts","body":"two"}]')

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "StrategyChain.resolve@strategy-chain.ts", [], 30000, "/my/repo"
    )

    assert calls == ["find_symbol"]
    assert "multiple definitions" in r["result"]
    assert "References — not retrieved" in r["result"]


def test_capped_candidate_page_cannot_prove_an_exact_definition(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    definitions = [
        {"name_path": "Other/resolve", "kind": "Method",
         "relative_path": f"src/other-{i}.ts", "body": "wrong"}
        for i in range(499)
    ]
    definitions.append({
        "name_path": "StrategyChain/resolve", "kind": "Method",
        "relative_path": "src/strategy-chain.ts", "body": "would-look-exact",
    })
    calls: list[str] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append(tool)
        return Ok(json.dumps(definitions))

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "StrategyChain.resolve@src/strategy-chain.ts", [], 30000, "/my/repo"
    )

    assert calls == ["find_symbol"]
    assert "maximum 500 definitions" in r["result"]
    assert "would-look-exact" not in r["result"]
    assert "References — not retrieved" in r["result"]


def test_broad_qualified_lookup_fetches_metadata_before_one_exact_body(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    calls: list[tuple[str, dict]] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append((tool, args))
        if tool == "find_symbol" and len(calls) == 1:
            return Ok('[{"name_path":"StrategyChain/resolve","kind":"Method",'
                      '"relative_path":"src/strategy-chain.ts"}]')
        if tool == "find_symbol":
            return Ok('[{"name_path":"StrategyChain/resolve","kind":"Method",'
                      '"relative_path":"src/strategy-chain.ts","body":"exact body"}]')
        return Ok("{}")

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "StrategyChain.resolve@src/strategy-chain.ts", [], 30000, "/my/repo"
    )

    assert calls[0][0] == "find_symbol"
    assert calls[0][1]["include_body"] is False
    assert calls[0][1]["max_matches"] == 500
    assert calls[1] == ("find_symbol", {
        "name_path_pattern": "StrategyChain/resolve",
        "relative_path": "src/strategy-chain.ts",
        "include_body": True,
        "max_matches": 500,
    })
    assert "exact body" in r["result"]


def test_overloaded_same_name_and_file_is_ambiguous(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    calls: list[str] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append(tool)
        return Ok('[{"name_path":"Widget/run","relative_path":"src/widget.ts",'
                  '"body_location":{"start_line":1,"end_line":2}},'
                  '{"name_path":"Widget/run","relative_path":"src/widget.ts",'
                  '"body_location":{"start_line":5,"end_line":6}}]')

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result("symbol", "Widget.run@src/widget.ts", [], 30000, "/my/repo")

    assert calls == ["find_symbol"]
    assert "multiple definitions" in r["result"]
    assert "References — not retrieved" in r["result"]


def test_unusable_exact_body_lookup_fails_closed(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    replies = [
        Ok('[{"name_path":"Widget/run","relative_path":"src/widget.ts"}]'),
        Ok("not json"),
    ]
    monkeypatch.setattr(p, "_call_tool", lambda *args, **kwargs: replies.pop(0))

    r = p.build_result("symbol", "Widget.run@src/widget.ts", [], 30000, "/my/repo")

    assert r["result"] is None
    assert r["outcome"] == "failed"
    assert "usable definition list" in r["hint"]


def test_exact_body_timeout_preserves_retryability(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    replies = [
        Ok('[{"name_path":"Widget/run","relative_path":"src/widget.ts"}]'),
        Missing("timeout", "body lookup timed out", retry_after_s=5),
    ]
    monkeypatch.setattr(p, "_call_tool", lambda *args, **kwargs: replies.pop(0))

    r = p.build_result("symbol", "Widget.run@src/widget.ts", [], 30000, "/my/repo")

    assert r["result"] is None
    assert r["reason"] == "timeout"
    assert r["retry_after_s"] == 5


def test_metadata_timeout_preserves_retryability(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    monkeypatch.setattr(
        p, "_call_tool",
        lambda *args, **kwargs: Missing("timeout", "metadata timed out", retry_after_s=5),
    )

    r = p.build_result("symbol", "Widget.run@src/widget.ts", [], 30000, "/my/repo")

    assert r["result"] is None
    assert r["reason"] == "timeout"
    assert r["retry_after_s"] == 5


def test_filename_like_symbol_is_not_split_as_a_qualifier():
    assert _split_target_file_hint("use-toast.ts@src/widgets.ts") == (
        "use-toast.ts", "src/widgets.ts", ""
    )


def test_class_qualified_target_without_file_hint_becomes_a_name_path():
    # Serena name paths are slash-separated. Without a file hint this used to return the dotted
    # target verbatim, which matched nothing and left references "not asked" — measured on
    # bench/fixtures/corpus_ts_typed with `StrategyChain.resolve`.
    assert _split_target_file_hint("StrategyChain.resolve") == (
        "resolve", "", "StrategyChain/resolve"
    )


def test_qualified_split_is_the_same_with_and_without_a_file_hint():
    bare = _split_target_file_hint("StrategyChain.resolve")
    hinted = _split_target_file_hint("StrategyChain.resolve@src/strategyChain.ts")
    assert (bare[0], bare[2]) == (hinted[0], hinted[2])
    assert hinted[1] == "src/strategyChain.ts"


def test_plain_and_filename_targets_without_hint_are_unchanged():
    assert _split_target_file_hint("resolve") == ("resolve", "", "")
    assert _split_target_file_hint("use-toast.ts") == ("use-toast.ts", "", "")


def test_retryable_missing_state_is_thread_local():
    p = LspProvider.__new__(LspProvider)
    ready = threading.Barrier(2)
    release = threading.Barrier(2)
    seen: list[Missing | None] = []

    def _timed_out_request():
        p._set_backend_missing(Missing("timeout", "slow", retry_after_s=5))
        ready.wait()
        release.wait()
        seen.append(p._get_backend_missing())

    worker = threading.Thread(target=_timed_out_request)
    worker.start()
    ready.wait()
    p._set_backend_missing(None)
    release.wait()
    worker.join()

    assert seen and seen[0] is not None
    assert seen[0].kind == "timeout"
    assert p._get_backend_missing() is None


def test_backend_error_text_is_thread_local():
    p = LspProvider.__new__(LspProvider)
    ready = threading.Barrier(2)
    release = threading.Barrier(2)
    seen: list[str | None] = []

    def _failed_request():
        p._last_backend_error = "request-a"
        ready.wait()
        release.wait()
        seen.append(p._last_backend_error)

    worker = threading.Thread(target=_failed_request)
    worker.start()
    ready.wait()
    p._last_backend_error = None
    release.wait()
    worker.join()

    assert seen == ["request-a"]
    assert p._last_backend_error is None


def test_pending_gaps_are_thread_local():
    p = LspProvider.__new__(LspProvider)
    ready = threading.Barrier(2)
    release = threading.Barrier(2)
    seen: list[tuple[dict[str, Any], ...]] = []

    def _partial_request():
        p._pending_gaps = ({"section": "references", "kind": "timeout"},)
        ready.wait()
        release.wait()
        seen.append(p._pending_gaps)

    worker = threading.Thread(target=_partial_request)
    worker.start()
    ready.wait()
    p._pending_gaps = ()
    release.wait()
    worker.join()

    assert seen == [({"section": "references", "kind": "timeout"},)]
    assert p._pending_gaps == ()


def test_file_qualified_symbol_does_not_use_a_definition_from_another_file(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._sessions["/my/repo"] = _make_fake_session(_State.READY)
    calls: list[str] = []

    def _fake_call_tool(session, tool, args, timeout_s):
        calls.append(tool)
        return Ok('[{"name_path":"Other/createSession","kind":"Method",'
                  '"relative_path":"src/other.ts","body_location":{"start_line":1,'
                  '"end_line":2},"body":"wrong"}]')

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)
    r = p.build_result(
        "symbol", "createSession@backend/src/session.handler.ts", [], 30000, "/my/repo"
    )

    assert r["confidence"] == "partial"
    assert "found no definition" in r["result"] and "wrong" not in r["result"]
    assert "References — not retrieved" in r["result"]
    assert calls == ["find_symbol"]
    assert any(g["section"] == "references" and g["kind"] == "not-asked" for g in r["gaps"])


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


# ---------------------------------------------------------------------------
# Group 9 — boot-failed says which kind of failure it is
# ---------------------------------------------------------------------------
#
# `boot-failed` used to carry no hint at all, so the FIRST query against a repo — the one an
# agent makes when it starts work — reported a broken engine while `uvx` was merely still
# resolving and downloading serena-agent from git. Run a minute later, the same serena booted
# fine with 29 tools. "Retry, this is a one-time install cost" and "go fix your machine" are
# opposite instructions, and they were reaching the caller as the same word.

def _failed_session(attempt: int = 1, boot_error: str | None = None) -> Any:
    s = _make_fake_session(_State.FAILED, cooldown_until=time.monotonic() + 60)
    s.attempt = attempt
    s.boot_error = boot_error
    return s


def test_boot_failed_on_a_cold_uvx_first_attempt_reads_as_retry(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which",
                        lambda x: "/fake/uvx" if x == "uvx" else None)
    p = LspProvider()
    assert p._cmd == "uvx"
    p._sessions["/my/repo"] = _failed_session(attempt=1)

    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["reason"] == "boot-failed"
    hint = r["hint"].lower()
    assert "retry" in hint
    assert "not a broken install" in hint
    assert "download" in hint


def test_an_unreadable_repository_never_launches_serena(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which",
                        lambda x: "/fake/uvx" if x == "uvx" else None)
    monkeypatch.setattr("codeintel.providers.lsp.os.path.isdir", lambda root: True)
    monkeypatch.setattr(
        "codeintel.providers.lsp._project_access_failure",
        lambda root: (
            f"repository root is not readable by codeintel (PermissionError: {root})",
            "grant repository access",
        ),
    )
    p = LspProvider()
    monkeypatch.setattr(
        p, "_get_or_create_session",
        lambda root: pytest.fail("an unreadable repository must not launch serena"),
    )

    r = p.build_result("symbol", "createSession", [], 1000, "/protected/repo")

    assert r["result"] is None
    assert r["reason"] == "source-unreadable"
    assert r["outcome"] == "unavailable"
    assert "PermissionError" in r["hint"]
    assert "grant repository access" in r["hint"]


def test_boot_failed_on_a_respawn_does_not_blame_a_cold_cache(monkeypatch):
    """`uvx` populated its cache on the first attempt, so "still downloading" is only true once."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which",
                        lambda x: "/fake/uvx" if x == "uvx" else None)
    p = LspProvider()
    p._sessions["/my/repo"] = _failed_session(attempt=2)

    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["reason"] == "boot-failed"
    assert "doctor --deep" in r["hint"]
    assert "not a broken install" not in r["hint"].lower()


def test_boot_failed_with_an_installed_serena_does_not_blame_uvx(monkeypatch):
    """An installed binary downloads nothing, so a failed boot there is a real failure."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which",
                        lambda x: "/usr/bin/serena" if x == "serena" else None)
    p = LspProvider()
    assert p._cmd == "serena"
    p._sessions["/my/repo"] = _failed_session(attempt=1)

    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert r["reason"] == "boot-failed"
    assert "uvx" not in r["hint"]
    assert "doctor --deep" in r["hint"]


def test_boot_failed_hint_carries_the_exception_type_but_not_backend_prose(monkeypatch):
    """The type is diagnostic; the message is not forwarded.

    `_summarize_backend_error` explains at length why this provider never hands a backend's own
    text to a calling agent — it can carry instructions addressed to a language model. A boot
    failure is subject to the same rule, so the hint names the exception class and nothing else.
    """
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which",
                        lambda x: "/fake/uvx" if x == "uvx" else None)
    p = LspProvider()
    p._sessions["/my/repo"] = _failed_session(attempt=2, boot_error="McpError")

    r = p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert "McpError" in r["hint"]


def test_a_session_built_by_new_still_answers_the_hint_fields():
    """The `__new__` stub pattern used across these tests must not fault a failure handler."""
    from codeintel.providers import lsp as lsp_mod

    sess = lsp_mod._LspSession.__new__(lsp_mod._LspSession)
    assert sess.attempt == 1
    assert sess.boot_error is None


def test_respawn_after_cooldown_increments_the_attempt_count(monkeypatch):
    """Without this the second boot would keep excusing itself as a cold cache forever."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    created: list[int] = []

    class FakeNewSession:
        state = _State.WARMING
        cooldown_until = 0.0
        _lock = threading.Lock()
        _loop = None
        _mcp_session = None

        def __init__(self, project_root, cmd, attempt=1):
            created.append(attempt)

        def wait_until_settled(self, timeout_s):
            return self.state

    monkeypatch.setattr("codeintel.providers.lsp._LspSession", FakeNewSession)
    p = LspProvider()
    p._sessions["/my/repo"] = _failed_session(attempt=3)
    p._sessions["/my/repo"].cooldown_until = time.monotonic() - 1  # cooldown elapsed

    p.build_result("symbol", "parse_result", [], 0, "/my/repo")
    assert created == [4]


# ---------------------------------------------------------------------------
# Group 12 — an empty reference list is an answer only when the backend could know
# ---------------------------------------------------------------------------

def _ts_repo(tmp_path, *, tsconfig: bool):
    """A TypeScript tree serena is configured to serve, with or without a project file."""
    serena = tmp_path / ".serena"
    serena.mkdir()
    (serena / "project.yml").write_text(
        "project_name: t\nlanguage_servers:\n- typescript\nencoding: utf-8\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    for i in range(19):
        (src / f"f{i}.ts").write_text("export const x = 1;\n", encoding="utf-8")
    if tsconfig:
        (tmp_path / "tsconfig.json").write_text('{"include":["src/**/*.ts"]}', encoding="utf-8")
    return str(tmp_path)


def _stub_empty_references(p, monkeypatch):
    def _fake_call_tool(session, tool, args, timeout_s):
        if tool == "find_symbol":
            return Ok('[{"name_path":"forwardReleasedItem","kind":"Function",'
                      '"relative_path":"src/f0.ts","body_location":{"start_line":1,"end_line":2},'
                      '"body":"export function forwardReleasedItem() {}"}]')
        if tool == "find_referencing_symbols":
            return Ok("{}")          # asked, answered, and the answer is empty
        return Missing("backend-error", "unstubbed tool")

    monkeypatch.setattr(p, "_call_tool", _fake_call_tool)


def test_empty_references_without_a_tsproject_are_disclosed_not_asserted(tmp_path, monkeypatch):
    """The deletion trap, closed at the point of use.

    `tsserver` with no `tsconfig.json` resolves each file alone and answers every cross-file lookup
    empty. Rendered as `## References (0)` at `confidence: complete` that is a confident "nothing
    references this" about the question asked immediately before deleting code — the same sentence
    outcome.py was written to make unsayable, reached from the other side."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    root = _ts_repo(tmp_path, tsconfig=False)
    p = LspProvider()
    p._sessions[root] = _make_fake_session(_State.READY)
    _stub_empty_references(p, monkeypatch)

    r = p.build_result("symbol", "forwardReleasedItem", [], 30000, root)

    assert r["confidence"] == "partial", r
    gap = next(g for g in (r.get("gaps") or []) if g["section"] == "references")
    assert gap["kind"] == "unresolvable"
    assert "UNKNOWN rather than none" in gap["detail"]
    # The body must say it too. A machine-readable gap beside prose asserting the opposite is how
    # the first version of this bug survived review.
    assert "## References — not retrieved" in r["result"]
    assert "## References (0)" not in r["result"]


def test_empty_references_with_a_tsproject_remain_a_real_answer(tmp_path, monkeypatch):
    """The other half, and the one that keeps `partial` worth reading: where the backend COULD
    know, an empty list still means there is nothing, and still says so at complete confidence."""
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    root = _ts_repo(tmp_path, tsconfig=True)
    p = LspProvider()
    p._sessions[root] = _make_fake_session(_State.READY)
    _stub_empty_references(p, monkeypatch)

    r = p.build_result("symbol", "forwardReleasedItem", [], 30000, root)

    assert r["confidence"] == "complete", r
    assert not [g for g in (r.get("gaps") or []) if g["section"] == "references"]
    assert "## References (0)" in r["result"]
