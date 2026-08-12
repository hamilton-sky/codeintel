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
