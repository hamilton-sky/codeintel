"""The gateway cache key moves with the index files, not only with this process's own reindexer.

Finding `server-cache-survives-external-reindex`: with `CODEINTEL_REINDEX=off` (or any time the
pass ran in another process), `codeintel index` rewrote the graph db and the running server went on
serving its pre-index `callers` answer with `cached: true`, because the only freshness input was
this process's reindex generation. The key now folds in the graph db's stamp and the semantic db's
mtime, so a write by any process, or a `reset` that deletes the db, forces a miss.

Every test points `CODEBASE_MEMORY_CACHE_DIR` at a `tmp_path` folder, so the key never reads the
machine's real graph cache.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from codeintel.core.paths import graph_slug
from codeintel.gateway.gateway import Gateway
from codeintel.gateway.reindexer import Reindexer


class _CountingGraph:
    """A graph engine that answers `callers` and counts how often it was really asked."""

    available = True

    def __init__(self) -> None:
        self.calls = 0

    def build_result(self, op, target, files, budget, project_root):
        self.calls += 1
        return {"ok": True, "op": op, "target": target, "engine": "graph", "cached": False,
                "result": f"Callers of {target} (1) - core.c1 [call {self.calls}]"}


@pytest.fixture
def graph_cache(tmp_path, monkeypatch) -> pathlib.Path:
    cache = tmp_path / "graph-cache"
    cache.mkdir()
    monkeypatch.delenv("CODEBASE_MEMORY_HOME", raising=False)
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def root(tmp_path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core.py").write_text("def helper():\n    return 1\n\n\ndef c1():\n    return helper()\n")
    return str(repo)


def _db(graph_cache: pathlib.Path, root: str) -> pathlib.Path:
    db = graph_cache / (graph_slug(os.path.realpath(root)) + ".db")
    db.write_bytes(b"graph index")
    os.utime(db, (1_700_000_000, 1_700_000_000))
    return db


def _gateway(monkeypatch) -> tuple[Gateway, _CountingGraph]:
    monkeypatch.setenv("CODEINTEL_REINDEX", "off")   # nothing in this process advances the index
    graph = _CountingGraph()
    return Gateway(graph=graph, reindexer=Reindexer()), graph


def _callers(gw: Gateway, root: str) -> dict:
    return gw.query(op="callers", target="helper", engine="graph", role="", project_root=root)


def test_a_graph_db_rewritten_by_another_process_is_a_cache_miss(graph_cache, root, monkeypatch):
    db = _db(graph_cache, root)
    gw, graph = _gateway(monkeypatch)

    assert _callers(gw, root)["cached"] is False
    assert _callers(gw, root)["cached"] is True          # the cache is in play before the index

    os.utime(db, (1_700_000_100, 1_700_000_100))         # `codeintel index` in another process
    after = _callers(gw, root)

    assert after["cached"] is False, after
    assert graph.calls == 2


def test_a_deleted_graph_db_is_a_cache_miss(graph_cache, root, monkeypatch):
    db = _db(graph_cache, root)
    gw, graph = _gateway(monkeypatch)

    _callers(gw, root)
    assert _callers(gw, root)["cached"] is True

    db.unlink()                                          # `codeintel reset` while the server runs
    after = _callers(gw, root)

    assert after["cached"] is False, after
    assert graph.calls == 2


def test_a_rewritten_semantic_db_is_a_cache_miss(graph_cache, root, monkeypatch, tmp_path):
    """The semantic half of the key: the semantic db file `default_db_path` names, under
    `CODEINTEL_HOME`."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEINTEL_HOME", str(home))
    semantic = home / "semantic.db"
    semantic.write_bytes(b"vectors")
    os.utime(semantic, (1_700_000_000, 1_700_000_000))
    _db(graph_cache, root)
    gw, graph = _gateway(monkeypatch)

    _callers(gw, root)
    assert _callers(gw, root)["cached"] is True

    os.utime(semantic, (1_700_000_100, 1_700_000_100))
    assert _callers(gw, root)["cached"] is False
    assert graph.calls == 2


def test_an_untouched_index_still_serves_from_cache(graph_cache, root, monkeypatch):
    """Guard the guard: the new key must not turn every query into a miss."""
    _db(graph_cache, root)
    gw, graph = _gateway(monkeypatch)

    _callers(gw, root)
    assert _callers(gw, root)["cached"] is True
    assert _callers(gw, root)["cached"] is True
    assert graph.calls == 1
