"""Production-hardening behaviors added in 0.3.0:
  - SemanticProvider only pays the inline full-index on a COLD repo (warm rides the reindexer),
    but always indexes when background reindexing is disabled.
  - Indexer honours a global per-pass chunk ceiling (memory backstop).
  - overview auto-routing falls back to LSP when the repo isn't in the graph (not only when the
    graph backend is unavailable).
  - log_swallowed never raises.
"""
from __future__ import annotations

import os

import pytest

import codeintel.providers.semantic as sem
from codeintel.gateway import Gateway
from codeintel.provider import log_swallowed

# --------------------------------------------------------------------------- semantic cold/warm

def _patch_semantic(monkeypatch, tmp_path, has_index_value):
    """Point the provider at a throwaway db and stub the heavy Searcher/Indexer bits."""
    from codeintel.indexer import Indexer
    from codeintel.searcher import Searcher

    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    calls = {"index": 0}

    def _fake_index(self, root):
        calls["index"] += 1
        return 0

    monkeypatch.setattr(Indexer, "index", _fake_index)
    monkeypatch.setattr(Searcher, "has_index", lambda self, root: has_index_value)
    monkeypatch.setattr(Searcher, "search", lambda self, *a, **k: [])
    return calls


def test_cold_repo_triggers_inline_index(tmp_path, monkeypatch):
    if not sem._DEPS_OK:
        pytest.skip("semantic deps missing")
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    calls = _patch_semantic(monkeypatch, tmp_path, has_index_value=False)
    sem.SemanticProvider().build_result("search", "q", [], 0, str(tmp_path))
    assert calls["index"] == 1  # nothing indexed yet → index inline


def test_warm_repo_skips_inline_index(tmp_path, monkeypatch):
    if not sem._DEPS_OK:
        pytest.skip("semantic deps missing")
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    calls = _patch_semantic(monkeypatch, tmp_path, has_index_value=True)
    sem.SemanticProvider().build_result("search", "q", [], 0, str(tmp_path))
    assert calls["index"] == 0  # already indexed + background reindex on → skip the costly walk


def test_reindex_off_forces_inline_index_even_when_warm(tmp_path, monkeypatch):
    if not sem._DEPS_OK:
        pytest.skip("semantic deps missing")
    monkeypatch.setenv("CODEINTEL_REINDEX", "off")
    calls = _patch_semantic(monkeypatch, tmp_path, has_index_value=True)
    sem.SemanticProvider().build_result("search", "q", [], 0, str(tmp_path))
    assert calls["index"] == 1  # no background reindex → inline pass is the only freshness path


# --------------------------------------------------------------------------- indexer global cap

def test_indexer_stops_at_max_total_chunks(tmp_path):
    from codeintel.indexer import Indexer, _project_key
    from codeintel.semantic_db import SemanticDb

    for i in range(6):
        (tmp_path / f"f{i}.py").write_text("\n".join(f"x = {j}" for j in range(60)))

    db = SemanticDb(str(tmp_path / "db"))
    db.init()
    real = os.path.realpath(str(tmp_path))
    chunks = Indexer(db, max_chunks=100, max_total_chunks=3)._collect_new_chunks(
        tmp_path, _project_key(real), real
    )
    db.close()

    # The ceiling is checked at each file boundary, so it stops after the first file — far fewer
    # than all six files' worth of chunks got collected.
    files_touched = {c[2] for c in chunks}
    assert len(files_touched) < 6
    assert len(chunks) > 0


# --------------------------------------------------------------------------- overview fallback

class _StubProvider:
    available = True

    def __init__(self, result, reason=None):
        self._result, self._reason = result, reason

    def build_result(self, op, target, files, budget, project_root):
        r = {"ok": True, "op": op, "target": target, "result": self._result,
             "engine": "x", "cached": False}
        if self._reason:
            r["reason"] = self._reason
        return r


def test_overview_falls_back_to_lsp_when_repo_not_in_graph():
    graph = _StubProvider(result=None, reason="project-not-indexed")
    lsp = _StubProvider(result="## Overview\n(from lsp)")
    gw = Gateway(graph=graph, lsp=lsp, semantic=None)
    r = gw.query(op="overview", target="", engine="auto", project_root="/tmp/x")
    assert r["result"] == "## Overview\n(from lsp)"


def test_overview_falls_back_to_lsp_when_graph_unavailable():
    graph = _StubProvider(result=None, reason="engine-unavailable")
    lsp = _StubProvider(result="lsp-view")
    gw = Gateway(graph=graph, lsp=lsp, semantic=None)
    r = gw.query(op="overview", target="", engine="auto", project_root="/tmp/x")
    assert r["result"] == "lsp-view"


# --------------------------------------------------------------------------- reindex="never" wiring

def test_reindexer_honors_reindex_never(tmp_path, monkeypatch):
    from codeintel.reindexer import Reindexer
    (tmp_path / ".codeintel.toml").write_text('reindex = "never"\n')
    rx = Reindexer(debounce_seconds=0)
    submitted = []
    monkeypatch.setattr(rx._executor, "submit", lambda *a: submitted.append(a))
    rx.maybe_reindex(str(tmp_path))
    assert submitted == []  # reindex="never" → no background pass scheduled


def test_reindexer_schedules_by_default(tmp_path, monkeypatch):
    from codeintel.reindexer import Reindexer
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    rx = Reindexer(debounce_seconds=0)  # no .codeintel.toml → default "on-demand"
    submitted = []
    monkeypatch.setattr(rx._executor, "submit", lambda *a: submitted.append(a))
    rx.maybe_reindex(str(tmp_path))
    assert len(submitted) == 1  # default → a background pass is scheduled


# --------------------------------------------------------------------------- graph negative-lookup TTL

def _bare_graph(monkeypatch, run_result):
    import threading as _t

    import codeintel.providers.graph as gmod
    from codeintel.graph_backend import BackendClient
    p = gmod.GraphProvider.__new__(gmod.GraphProvider)  # skip _detect_backend / PATH probing
    p._backend = BackendClient.__new__(BackendClient)
    p._project_cache = {}
    p._negative_until = {}
    p._project_cache_lock = _t.Lock()
    calls = {"n": 0}

    def fake_run(method, payload, timeout):
        calls["n"] += 1
        return run_result

    p._run = fake_run
    return p, calls


def test_graph_negative_lookup_reprobes_after_ttl(monkeypatch):
    p, calls = _bare_graph(monkeypatch, {"projects": []})  # never matches → None
    assert p._resolve_project("/repo/x") is None and calls["n"] == 1
    assert p._resolve_project("/repo/x") is None and calls["n"] == 1  # within TTL: no re-probe
    p._negative_until["/repo/x"] = 0.0                                # force-expire the TTL
    assert p._resolve_project("/repo/x") is None and calls["n"] == 2  # re-probed


def test_graph_positive_lookup_is_cached(monkeypatch):
    p, calls = _bare_graph(monkeypatch, {"projects": [{"name": "proj", "root_path": "/repo/x"}]})
    assert p._resolve_project("/repo/x").name == "proj" and calls["n"] == 1
    assert p._resolve_project("/repo/x").name == "proj" and calls["n"] == 1  # positive cached, no re-probe


# --------------------------------------------------------------------------- log_swallowed

def test_log_swallowed_never_raises():
    log_swallowed("unit-test", RuntimeError("boom"))  # must not raise
