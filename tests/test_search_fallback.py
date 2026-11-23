"""An auto-routed `search` the semantic engine cannot answer falls back to the graph name search.

On a host where the embedding model cannot download, every `search` used to come back empty with
`index-failed` (or `indexing-in-progress`, forever) while the graph engine, which needs no model,
sat idle beside it. The fallback asks the graph engine `pattern` with the same target and says
plainly that the matches are by name, not by meaning.
"""
from __future__ import annotations

import pytest

from codeintel.core.provider import attach_confidence, safe_null_result
from codeintel.gateway.gateway import Gateway
from codeintel.gateway.policy import TieringPolicy


class _Semantic:
    """A semantic engine that cannot answer, for the given reason."""

    available = True

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.calls: list[tuple[str, str]] = []

    def build_result(self, op, target, files, budget, project_root):
        self.calls.append((op, target))
        return safe_null_result(op, target, engine="semantic", reason=self.reason,
                                hint=f"semantic says {self.reason}")


class _Graph:
    """A graph engine that answers `pattern` with one row per name containing the target."""

    available = True

    def __init__(self, names: list[str] | None = None) -> None:
        self.names = names if names is not None else ["core.helper", "core.helper_two"]
        self.calls: list[tuple[str, str]] = []

    def build_result(self, op, target, files, budget, project_root):
        self.calls.append((op, target))
        hits = [n for n in self.names if target in n]
        if op != "pattern" or not hits:
            return safe_null_result(op, target, engine="graph", reason="not-in-graph")
        return attach_confidence({
            "ok": True, "op": op, "target": target, "engine": "graph", "cached": False,
            "result": "\n".join(f"core.py:1 | {n}" for n in hits),
        }, [])


class _CountingReindexer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def maybe_reindex(self, project_root: str) -> bool:
        self.calls.append(project_root)
        return False

    def reindex_pending(self, project_root: str) -> bool:
        return False

    def generation(self, project_root: str) -> int:
        return 0


def _gateway(semantic_reason: str, *, graph: _Graph | None = None, policy=None,
             reindexer=None) -> tuple[Gateway, _Semantic, _Graph]:
    sem = _Semantic(semantic_reason)
    g = graph or _Graph()
    gw = Gateway(graph=g, semantic=sem, policy=policy, reindexer=reindexer, oneshot=True)
    return gw, sem, g


@pytest.mark.parametrize(
    "reason", ["index-failed", "indexing-in-progress", "engine-unavailable", "no-index"])
def test_auto_search_falls_back_to_graph_name_search(tmp_path, reason):
    gw, sem, graph = _gateway(reason)

    env = gw.query(op="search", target="helper", project_root=str(tmp_path))

    assert sem.calls == [("search", "helper")]
    assert graph.calls == [("pattern", "helper")]
    assert env["fallback"] == "graph-name-search"
    assert env["engine"] == "graph"
    assert "core.helper" in env["result"]
    kinds = [g.get("kind") for g in env.get("gaps") or []]
    assert "name-only-fallback" in kinds
    gap = next(g for g in env["gaps"] if g["kind"] == "name-only-fallback")
    assert "matched by name, not meaning" in gap["detail"]
    assert env["confidence"] == "partial"
    # The semantic reason is kept, so the caller still learns why meaning search did not run.
    assert reason in env["hint"]


def test_the_fallback_keeps_the_graph_answers_own_gaps(tmp_path):
    class _GappyGraph(_Graph):
        def build_result(self, op, target, files, budget, project_root):
            self.calls.append((op, target))
            return attach_confidence({
                "ok": True, "op": op, "target": target, "engine": "graph", "cached": False,
                "result": "core.py:1 | core.helper",
            }, [{"section": "index", "kind": "stale-index", "detail": "the index is old"}])

    gw, _sem, _graph = _gateway("index-failed", graph=_GappyGraph())
    env = gw.query(op="search", target="helper", project_root=str(tmp_path))

    kinds = [g.get("kind") for g in env["gaps"]]
    assert kinds == ["stale-index", "name-only-fallback"]


def test_an_explicit_semantic_engine_gets_no_fallback(tmp_path):
    gw, _sem, graph = _gateway("index-failed")

    env = gw.query(op="search", target="helper", engine="semantic", project_root=str(tmp_path))

    assert graph.calls == []
    assert "fallback" not in env
    assert env["reason"] == "index-failed"
    assert env["result"] is None


def test_a_semantic_answer_about_the_repository_is_not_replaced(tmp_path):
    """`below-floor` is the engine asking and finding nothing similar: an answer, not a gap."""
    gw, _sem, graph = _gateway("below-floor")

    env = gw.query(op="search", target="helper", project_root=str(tmp_path))

    assert graph.calls == []
    assert "fallback" not in env
    assert env["reason"] == "below-floor"


def test_no_name_match_returns_the_semantic_answer_unchanged(tmp_path):
    gw, _sem, graph = _gateway("index-failed", graph=_Graph(names=[]))

    env = gw.query(op="search", target="helper", project_root=str(tmp_path))

    assert graph.calls == [("pattern", "helper")]
    assert "fallback" not in env
    assert env["reason"] == "index-failed"
    assert env["hint"] == "semantic says index-failed"


def test_a_role_allowed_search_but_not_pattern_gets_no_fallback(tmp_path):
    policy = TieringPolicy(enabled=True,
                           rules={"searcher": ["search"], "admin": ["search", "pattern"]},
                           roots={"searcher": ["*"], "admin": ["*"]})
    gw, _sem, graph = _gateway("index-failed", policy=policy)
    assert gw.allows("searcher", "pattern") is False

    restricted = gw.query(op="search", target="helper", project_root=str(tmp_path),
                          role="searcher")

    assert graph.calls == []
    assert "fallback" not in restricted
    assert restricted["reason"] == "index-failed"
    assert restricted["result"] is None

    allowed = gw.query(op="search", target="helper", project_root=str(tmp_path), role="admin")
    assert allowed["fallback"] == "graph-name-search"
    assert graph.calls == [("pattern", "helper")]


def test_the_fallback_does_not_run_a_second_reindex_check(tmp_path):
    rx = _CountingReindexer()
    sem = _Semantic("index-failed")
    gw = Gateway(graph=_Graph(), semantic=sem, reindexer=rx)

    env = gw.query(op="search", target="helper", project_root=str(tmp_path))

    assert env["fallback"] == "graph-name-search"
    assert rx.calls == [str(tmp_path)]


def test_a_fallback_answer_is_not_cached_over_the_semantic_engine(tmp_path):
    """The next search must ask the semantic engine again: it may have recovered, and a cached
    name-only answer would hide that for as long as the cache entry lived."""
    gw, sem, _graph = _gateway("index-failed")

    gw.query(op="search", target="helper", project_root=str(tmp_path))
    second = gw.query(op="search", target="helper", project_root=str(tmp_path))

    assert len(sem.calls) == 2
    assert second.get("cached") is False
