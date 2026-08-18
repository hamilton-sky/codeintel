"""Tests for F4: unified gateway — engine routing, fan-out merge, cache, and tiering."""
from __future__ import annotations

from codeintel.gateway import Gateway
from codeintel.policy import TieringPolicy
from codeintel.provider import Result

# ---------------------------------------------------------------------------
# Stub providers — no mocking library
# ---------------------------------------------------------------------------

class _StubProvider:
    """Returns a fixed result; tracks call count."""

    available = True

    def __init__(self, engine_name: str, result_text: str) -> None:
        self._engine_name = engine_name
        self._result_text = result_text
        self.call_count = 0

    def build_result(self, op, target, files, budget, project_root) -> Result:
        self.call_count += 1
        return {
            "ok": True,
            "op": str(op or ""),
            "target": str(target or ""),
            "result": self._result_text,
            "engine": self._engine_name,
            "cached": False,
        }


class _UnavailableProvider(_StubProvider):
    available = False


class _RaisingProvider:
    available = True

    def build_result(self, op, target, files, budget, project_root):
        raise RuntimeError("injected provider error")


# ---------------------------------------------------------------------------
# Test 1: engine=graph routes to GraphProvider only
# ---------------------------------------------------------------------------

def test_engine_graph_routes_to_graph_only():
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r = gw.query(op="symbol", target="x", engine="graph")
    assert r["result"] == "graph-data"
    assert graph.call_count == 1
    assert lsp.call_count == 0


# ---------------------------------------------------------------------------
# Test 2: engine=lsp routes to LspProvider only
# ---------------------------------------------------------------------------

def test_engine_lsp_routes_to_lsp_only():
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r = gw.query(op="symbol", target="x", engine="lsp")
    assert r["result"] == "lsp-data"
    assert lsp.call_count == 1
    assert graph.call_count == 0


# ---------------------------------------------------------------------------
# Test 3: engine=semantic returns unavailable safe-null
# ---------------------------------------------------------------------------

def test_engine_semantic_returns_unavailable_safe_null():
    semantic = _UnavailableProvider("semantic", "semantic-data")
    gw = Gateway(semantic=semantic)
    r = gw.query(op="symbol", target="x", engine="semantic")
    assert r["ok"] is True
    assert r["result"] is None
    assert r.get("reason") == "engine-unavailable"


# ---------------------------------------------------------------------------
# Test 4: engine=both merges graph+lsp results
# ---------------------------------------------------------------------------

def test_engine_both_merges_graph_and_lsp():
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r = gw.query(op="symbol", target="x", engine="both")
    assert r["ok"] is True
    assert r["engine"] == "both"
    assert "graph-data" in r["result"]
    assert "lsp-data" in r["result"]


# ---------------------------------------------------------------------------
# Test 5: engine=both when graph unavailable returns lsp alone
# ---------------------------------------------------------------------------

def test_engine_both_graph_unavailable_returns_lsp_alone():
    graph = _UnavailableProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r = gw.query(op="symbol", target="x", engine="both")
    assert r["ok"] is True
    assert "lsp-data" in r["result"]
    assert "graph-data" not in r["result"]


# ---------------------------------------------------------------------------
# Test 6: engine=all merges all three
# ---------------------------------------------------------------------------

def test_engine_all_merges_all_three():
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    semantic = _StubProvider("semantic", "semantic-data")
    gw = Gateway(graph=graph, lsp=lsp, semantic=semantic)
    r = gw.query(op="search", target="x", engine="all")
    assert r["ok"] is True
    assert r["engine"] == "all"
    assert "graph-data" in r["result"]
    assert "lsp-data" in r["result"]
    assert "semantic-data" in r["result"]


# ---------------------------------------------------------------------------
# Test 7: engine=auto op=impact routes to graph
# ---------------------------------------------------------------------------

def test_engine_auto_impact_routes_to_graph():
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r = gw.query(op="impact", target="x", engine="auto")
    assert r["result"] == "graph-data"
    assert graph.call_count == 1
    assert lsp.call_count == 0


# ---------------------------------------------------------------------------
# Test 8: engine=auto op=symbol routes to lsp
# ---------------------------------------------------------------------------

def test_engine_auto_symbol_routes_to_lsp():
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r = gw.query(op="symbol", target="x", engine="auto")
    assert r["result"] == "lsp-data"
    assert lsp.call_count == 1
    assert graph.call_count == 0


# ---------------------------------------------------------------------------
# Test 9: cache hit — second call returns cached=True
# ---------------------------------------------------------------------------

def test_cache_hit_second_call_returns_cached_true():
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph)
    target = "non-existent-target-xyz-cache-test"
    r1 = gw.query(op="symbol", target=target, engine="graph")
    assert r1["cached"] is False
    r2 = gw.query(op="symbol", target=target, engine="graph")
    assert r2["cached"] is True
    assert r2["result"] == "graph-data"
    assert graph.call_count == 1  # second call served from cache


# ---------------------------------------------------------------------------
# Test 10: cache miss after content change
# ---------------------------------------------------------------------------

def test_cache_miss_after_content_change(tmp_path):
    test_file = tmp_path / "module.py"
    test_file.write_text("# version 1")
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph)
    target = str(test_file)
    root = str(tmp_path)

    r1 = gw.query(op="symbol", target=target, engine="graph", project_root=root)
    assert r1["cached"] is False

    r2 = gw.query(op="symbol", target=target, engine="graph", project_root=root)
    assert r2["cached"] is True

    test_file.write_text("# version 2")

    r3 = gw.query(op="symbol", target=target, engine="graph", project_root=root)
    assert r3["cached"] is False


def test_changed_op_is_never_cached_but_hotspots_is():
    # `changed` reads the live git worktree — which the content-hash cache key can't see — so it must
    # bypass the cache or it serves a stale diff. `hotspots` is a pure function of the index → cached.
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph)

    c1 = gw.query(op="changed", target="", engine="graph")
    c2 = gw.query(op="changed", target="", engine="graph")
    assert c1["cached"] is False and c2["cached"] is False   # never a cache hit
    assert graph.call_count == 2                              # provider re-invoked every call

    graph.call_count = 0
    h1 = gw.query(op="hotspots", target="", engine="graph")
    h2 = gw.query(op="hotspots", target="", engine="graph")
    assert h1["cached"] is False and h2["cached"] is True     # second served from cache
    assert graph.call_count == 1


def test_changed_not_cached_on_fanout_path():
    # Regression for the reviewer's HIGH finding: the fan-out path (engine="both"/"all") must ALSO
    # bypass the cache for `changed` — else an explicit engine="both" serves a stale diff.
    graph = _StubProvider("graph", "graph-data")
    lsp = _StubProvider("lsp", "lsp-data")
    gw = Gateway(graph=graph, lsp=lsp)
    r1 = gw.query(op="changed", target="", engine="both")
    r2 = gw.query(op="changed", target="", engine="both")
    assert r1["cached"] is False and r2["cached"] is False   # never a cache hit on fan-out either
    assert graph.call_count == 2                              # re-dispatched each call, not cached


# ---------------------------------------------------------------------------
# Test 11: tiering off — role ignored, all ops pass
# ---------------------------------------------------------------------------

def test_tiering_off_role_ignored():
    policy = TieringPolicy(enabled=False, rules={"reader": ["symbol"]})
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph, policy=policy)
    r = gw.query(op="impact", target="x", engine="graph", role="reader")
    assert r["result"] == "graph-data"
    assert r.get("reason") != "op-not-allowed-for-role"


# ---------------------------------------------------------------------------
# Test 12: tiering on — disallowed op returns reason=op-not-allowed-for-role
# ---------------------------------------------------------------------------

def test_tiering_on_disallowed_op_blocked():
    policy = TieringPolicy(enabled=True, rules={"reader": ["symbol"]})
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph, policy=policy)
    r = gw.query(op="impact", target="x", engine="graph", role="reader")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "op-not-allowed-for-role"


# ---------------------------------------------------------------------------
# Test 13: provider exception — gateway catches, returns safe-null
# ---------------------------------------------------------------------------

def test_provider_exception_returns_safe_null():
    gw = Gateway(graph=_RaisingProvider())
    r = gw.query(op="symbol", target="x", engine="graph")
    assert r["ok"] is True
    assert r["result"] is None
    assert r.get("reason") == "provider-error"


# ---------------------------------------------------------------------------
# Stub reindexers for Phase 6 tests
# ---------------------------------------------------------------------------

class _TrackingReindexer:
    """Records calls to maybe_reindex without doing real work."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def maybe_reindex(self, project_root: str) -> None:
        self.calls.append(project_root)


class _RaisingReindexer:
    """Raises RuntimeError on every maybe_reindex call."""

    def maybe_reindex(self, project_root: str) -> None:
        raise RuntimeError("injected reindexer failure")


# ---------------------------------------------------------------------------
# Test 14: gateway calls maybe_reindex with the correct project_root
# ---------------------------------------------------------------------------

def test_query_calls_maybe_reindex():
    reindexer = _TrackingReindexer()
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph, reindexer=reindexer)
    gw.query(op="search", target="foo", project_root="/tmp/proj")
    assert reindexer.calls == ["/tmp/proj"]


# ---------------------------------------------------------------------------
# Test 15: reindexer failure does not affect query result
# ---------------------------------------------------------------------------

def test_reindexer_failure_does_not_affect_query_result():
    reindexer = _RaisingReindexer()
    graph = _StubProvider("graph", "graph-data")
    gw = Gateway(graph=graph, reindexer=reindexer)
    r = gw.query(op="search", target="foo", project_root="/tmp/proj")
    assert r["ok"] is True


# --------------------------------------------------------------------------- engine cache keying

class _NotIndexedGraph:
    available = True

    def build_result(self, op, target, files, budget, project_root):
        from codeintel.provider import safe_null_result
        return safe_null_result(op, target, engine="graph", reason="project-not-indexed")


class _AnsweringLsp:
    available = True

    def build_result(self, op, target, files, budget, project_root):
        return {"ok": True, "op": op, "target": target, "result": "LSP ANSWER",
                "engine": "lsp", "cached": False}


def _overview_gateway():
    from codeintel.gateway import Gateway
    from codeintel.reindexer import Reindexer
    rx = Reindexer()
    rx.maybe_reindex = lambda root: None          # hold freshness steady across the calls
    return Gateway(graph=_NotIndexedGraph(), lsp=_AnsweringLsp(), semantic=None, reindexer=rx)


def test_an_auto_fallback_answer_is_not_served_to_an_explicit_engine_request():
    """`auto` and an explicit `graph` both resolved to the string "graph" and so shared one cache
    key — but they are different questions: `auto` accepts the overview LSP fallback, an explicit
    `graph` does not. One `auto` miss parked an LSP answer under the graph key, and the next
    `engine=graph` request got it back with `cached: true` and an `engine: "lsp"` field
    contradicting its own request. Reachable on any cold start, since "graph not indexed yet" is
    the normal first-query state.
    """
    gw = _overview_gateway()

    auto = gw.query(op="overview", target="sym", engine="auto", role="", project_root="/tmp/x")
    assert auto["engine"] == "lsp" and auto["result"] == "LSP ANSWER"

    explicit = gw.query(op="overview", target="sym", engine="graph", role="",
                        project_root="/tmp/x")
    assert explicit["engine"] == "graph", "an explicit engine request got another engine's answer"
    assert explicit["result"] is None
    assert explicit["reason"] == "project-not-indexed"


def test_auto_and_explicit_requests_keep_their_own_cache_entries():
    gw = _overview_gateway()
    gw.query(op="overview", target="sym", engine="graph", role="", project_root="/tmp/x")

    # The explicit miss must not poison `auto`, which can still fall back to LSP.
    auto = gw.query(op="overview", target="sym", engine="auto", role="", project_root="/tmp/x")
    assert auto["result"] == "LSP ANSWER"


def test_a_repeated_query_is_still_served_from_cache():
    """Guard the guard: splitting the key must not disable caching."""
    from codeintel.gateway import Gateway
    from codeintel.reindexer import Reindexer

    calls = {"n": 0}

    class _Counting(_AnsweringLsp):
        def build_result(self, op, target, files, budget, project_root):
            calls["n"] += 1
            return super().build_result(op, target, files, budget, project_root)

    rx = Reindexer()
    rx.maybe_reindex = lambda root: None
    gw = Gateway(graph=None, lsp=_Counting(), semantic=None, reindexer=rx)

    first = gw.query(op="symbol", target="sym", engine="lsp", role="", project_root="/tmp/x")
    second = gw.query(op="symbol", target="sym", engine="lsp", role="", project_root="/tmp/x")

    assert first["cached"] is False and second["cached"] is True
    assert calls["n"] == 1


def test_cache_get_and_put_always_use_the_same_key():
    """A `get`/`put` that disagree on the engine component silently disable the cache — every
    lookup misses, every dispatch re-runs, and nothing fails. It happened while fixing the
    auto/explicit collision above and was caught only because an unrelated test asserted a hit.
    Pin the agreement directly rather than relying on that coincidence."""
    import inspect
    import re

    from codeintel.gateway import Gateway

    # `query` is a thin redaction wrapper; the dispatch body (and so the cache) lives in `_query`.
    source = inspect.getsource(Gateway._query)
    gets = re.findall(r"_cache\.get\(\s*([^)]*)\)", source)
    puts = re.findall(r"_cache\.put\(\s*([^)]*)\)", source)
    assert gets and puts, "cache call sites not found — did the query body get restructured?"

    def engine_arg(call: str) -> str:
        return [a.strip() for a in call.split(",")][2]

    keys = {engine_arg(c) for c in gets} | {engine_arg(c) for c in puts}
    assert keys == {"cache_engine"}, f"cache key components disagree: {keys}"


# --------------------------------------------------------------------------- staleness signalling

class _Answering:
    available = True

    def build_result(self, op, target, files, budget, project_root):
        return {"ok": True, "op": op, "target": target, "result": "ANSWER",
                "engine": "graph", "cached": False}


def test_an_answer_served_during_a_reindex_says_so():
    """Structural answers hash a symbol NAME, not file bytes, so nothing else in the envelope can
    reveal that the index is behind. An agent's loop is edit -> ask what I broke, which lands
    exactly in that window. Invalidating the cache would not help: the index itself is stale, so
    re-asking just refetches the same data more expensively. Saying so is the only honest fix."""
    from codeintel.gateway import Gateway
    from codeintel.reindexer import Reindexer

    rx = Reindexer()
    gw = Gateway(graph=_Answering(), lsp=None, semantic=None, reindexer=rx)
    rx._in_flight.add("/repo")

    res = gw.query(op="callers", target="f", role="", project_root="/repo")
    assert res["result"] == "ANSWER"                 # still answers — this is a caveat, not a error
    assert res["reindexing"] is True
    assert "re-ask" in res["hint"]


def test_a_settled_index_does_not_flag_an_answer():
    """The signal must be rare enough to mean something — flagging every answer would train a
    reader to ignore it. Steady state is: a reindex ran, the debounce window has not elapsed, so
    no new pass fires and answers come back unqualified. (The FIRST query for a root does fire one
    and is legitimately flagged while it runs — the index really is being rebuilt underneath.)"""
    import time

    from codeintel.gateway import Gateway
    from codeintel.reindexer import Reindexer

    rx = Reindexer()
    rx._last_fired["/repo"] = time.monotonic()      # inside the debounce window: nothing re-fires
    gw = Gateway(graph=_Answering(), lsp=None, semantic=None, reindexer=rx)

    res = gw.query(op="callers", target="f", role="", project_root="/repo")
    assert res["result"] == "ANSWER"
    assert res.get("reindexing") is None


def test_an_empty_result_is_not_flagged_as_reindexing():
    """`reindexing` qualifies an answer. On a safe-null there is no answer to qualify, and the
    `reason`/`hint` already carry the explanation."""
    from codeintel.gateway import Gateway
    from codeintel.provider import safe_null_result
    from codeintel.reindexer import Reindexer

    class _Empty(_Answering):
        def build_result(self, op, target, files, budget, project_root):
            return safe_null_result(op, target, engine="graph", reason="not-in-graph")

    rx = Reindexer()
    gw = Gateway(graph=_Empty(), lsp=None, semantic=None, reindexer=rx)
    rx._in_flight.add("/repo")

    res = gw.query(op="callers", target="f", role="", project_root="/repo")
    assert res.get("reindexing") is None
    assert res["reason"] == "not-in-graph"


def test_reindex_pending_clears_once_the_pass_completes(monkeypatch):
    from codeintel.reindexer import Reindexer

    rx = Reindexer()
    monkeypatch.setattr(rx, "_semantic_reindex", lambda root: None)
    monkeypatch.setattr(rx, "_graph_reindex", lambda root: None)

    rx._in_flight.add("/repo")
    assert rx.reindex_pending("/repo") is True
    rx._do_reindex("/repo")
    assert rx.reindex_pending("/repo") is False, "a completed reindex must clear the flag"


def test_reindex_pending_clears_even_when_both_passes_fail(monkeypatch):
    """A stuck flag would mark every later answer stale and train the reader to ignore it."""
    from codeintel.reindexer import Reindexer

    rx = Reindexer()
    for name in ("_semantic_reindex", "_graph_reindex"):
        monkeypatch.setattr(rx, name, lambda root: (_ for _ in ()).throw(RuntimeError("down")))

    rx._in_flight.add("/repo")
    rx._do_reindex("/repo")
    assert rx.reindex_pending("/repo") is False


# ---------------------------------------------------------------------------
# "nothing found" must stay distinguishable from "nothing could be asked"
#
# `_merge` collapsed every engine's reason into a flat "no-result" — the string an agent is told
# to read as "nothing found / not indexed yet". `context` fans out by default, so a fan-out with
# both backends missing produced a confident "that symbol does not exist".
# ---------------------------------------------------------------------------

class _NullProvider:
    """Available, but answers safe-null with a specific reason."""

    available = True

    def __init__(self, engine_name: str, reason: str) -> None:
        self._engine_name = engine_name
        self._reason = reason

    def build_result(self, op, target, files, budget, project_root) -> Result:
        return {
            "ok": True, "op": str(op or ""), "target": str(target or ""), "result": None,
            "engine": self._engine_name, "cached": False, "reason": self._reason,
        }


def test_a_fanout_where_no_engine_could_be_asked_is_not_reported_as_no_result():
    gw = Gateway(graph=_NullProvider("graph", "engine-unavailable"),
                 lsp=_NullProvider("lsp", "boot-failed"))
    r = gw.query(op="context", target="foo", engine="both")
    assert r["result"] is None
    assert r["reason"] == "engines-unavailable"
    # The per-engine causes survive the merge, and the envelope says plainly that this is not
    # evidence of absence — the inference an agent would otherwise draw.
    assert "graph: engine-unavailable" in r["hint"]
    assert "lsp: boot-failed" in r["hint"]
    assert "NOT evidence" in r["hint"]


def test_a_fanout_where_engines_ran_and_found_nothing_still_reports_no_result():
    """The distinction has to cut both ways, or it is just a rename."""
    gw = Gateway(graph=_NullProvider("graph", "not-in-graph"),
                 lsp=_NullProvider("lsp", "not-found"))
    r = gw.query(op="context", target="nope", engine="both")
    assert r["reason"] == "no-result"
    assert "NOT evidence" not in (r.get("hint") or "")


# ---------------------------------------------------------------------------
# The staleness marker belongs on every exit
# ---------------------------------------------------------------------------

class _StubReindexer:
    """Reports a reindex in flight, with a generation that does NOT advance — exactly the state in
    which the cache serves an answer built from the previous index."""

    def __init__(self) -> None:
        self.generation_value = 7

    def generation(self, root):
        return self.generation_value

    def reindex_pending(self, root=None):
        return True

    def maybe_reindex(self, *a, **k):
        return None


def _reindexing_gateway():
    gw = Gateway(graph=_StubProvider("graph", "graph-data"), lsp=_StubProvider("lsp", "lsp-data"))
    gw._reindexer = _StubReindexer()
    return gw


def test_a_cache_hit_during_a_reindex_is_marked_stale():
    """The case the marker exists for, and the one path that used to drop it: the freshness
    generation only advances when a reindex COMPLETES, so a hit taken mid-pass is served from the
    previous index."""
    gw = _reindexing_gateway()
    first = gw.query(op="callers", target="x", engine="graph", project_root="/repo")
    assert first["cached"] is False
    second = gw.query(op="callers", target="x", engine="graph", project_root="/repo")
    assert second["cached"] is True
    assert second.get("reindexing") is True, "a cached answer mid-reindex must say it may be behind"


def test_a_fanout_result_is_marked_stale_during_a_reindex():
    gw = _reindexing_gateway()
    r = gw.query(op="context", target="x", engine="both", project_root="/repo")
    assert r.get("reindexing") is True
