"""Integration tests — the cross-cutting behaviors that per-feature unit tests missed:
two real projects sharing the index, the gateway lifecycle across calls, cache
invalidation on reindex, engine fallback, config wiring, and the HTTP header guard.

These are the tests that would have caught the bugs the review surfaced.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from codeintel.gateway import Gateway
from codeintel.indexer import Indexer
from codeintel.provider import safe_null_result
from codeintel.providers.semantic import SemanticProvider
from codeintel.searcher import Searcher
from codeintel.semantic_db import SemanticDb


class _FakeTextEmbedding:
    """Deterministic stub — one dimension keyed off text length so distinct content
    embeds distinctly, with no dependency on PYTHONHASHSEED."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        out = []
        for t in list(texts):
            v = np.full(384, 0.01, dtype=np.float32)
            v[len(t) % 384] = 1.0
            out.append(v)
        return out


class _StubProvider:
    def __init__(self, engine, text):
        self._e, self._t = engine, text

    @property
    def available(self):
        return True

    def build_result(self, op, target, files, budget, project_root):
        return {"ok": True, "op": op, "target": target, "result": self._t,
                "engine": self._e, "cached": False}


class _FakeReindexer:
    def __init__(self):
        self.gen = 0

    def maybe_reindex(self, project_root):
        pass

    def generation(self, project_root):
        return self.gen


# --- Bug #1: cross-project isolation (data-loss regression) -------------------

def test_two_projects_do_not_leak_or_purge(tmp_path):
    """Indexing repo B must NOT purge repo A from the shared cache, and a search in B
    must never surface A's files."""
    repo_a = tmp_path / "a"; repo_a.mkdir()
    (repo_a / "alpha.py").write_text("def alpha_only():\n    return 1\n")
    repo_b = tmp_path / "b"; repo_b.mkdir()
    (repo_b / "beta.py").write_text("def beta_only():\n    return 2\n")

    db = SemanticDb(str(tmp_path / "shared.db")); db.init()

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        assert Indexer(db).index(str(repo_a)) > 0
        assert Indexer(db).index(str(repo_b)) > 0   # must not purge A
        s = Searcher(db)
        assert s.has_index(str(repo_a)) is True      # A survived B's index pass
        assert s.has_index(str(repo_b)) is True

        res_b = s.search("beta", str(repo_b), cosine_floor=-1.0)
        assert res_b, "B should return its own matches"
        assert all("alpha.py" not in r["path"] for r in res_b)  # no cross-project leak

        res_a = s.search("alpha", str(repo_a), cosine_floor=-1.0)
        assert all("beta.py" not in r["path"] for r in res_a)


# --- Bug #3: the gateway persists across handler calls -----------------------

def test_gateway_is_reused_across_handler_calls():
    from codeintel import server
    server._reset_gateway()
    try:
        assert server._get_gateway() is server._get_gateway(), \
            "gateway must be reused so the cache + LSP session survive between calls"
    finally:
        server._reset_gateway()


# --- Bug #4: cache busts once the index generation moves ---------------------

def test_cache_busts_when_index_generation_bumps():
    rx = _FakeReindexer()
    gw = Gateway(graph=_StubProvider("graph", "v1"), reindexer=rx)
    # "parse_result" is a symbol, not a file — pre-fix its content hash never changed,
    # so the cache would never bust.
    r1 = gw.query(op="callers", target="parse_result", engine="graph", project_root="/x")
    assert r1["cached"] is False
    r2 = gw.query(op="callers", target="parse_result", engine="graph", project_root="/x")
    assert r2["cached"] is True          # same generation → hit
    rx.gen += 1                          # a reindex completed
    r3 = gw.query(op="callers", target="parse_result", engine="graph", project_root="/x")
    assert r3["cached"] is False         # stale entry invalidated


# --- Bug #9 (F4 AC): overview auto-falls-back graph → lsp --------------------

class _UnavailableGraph:
    available = False

    def build_result(self, *a, **k):
        return safe_null_result("overview", "", engine="graph", reason="engine-unavailable")


def test_overview_auto_falls_back_to_lsp():
    gw = Gateway(graph=_UnavailableGraph(), lsp=_StubProvider("lsp", "lsp-overview"))
    r = gw.query(op="overview", target="mod.py", engine="auto", project_root="/x")
    assert r["result"] == "lsp-overview"
    assert r["engine"] == "lsp"


# --- F5 drift: empty project reports no-index (not below-floor) --------------

def test_empty_project_reports_no_index(tmp_path, monkeypatch):
    import codeintel.providers.semantic as sem
    empty = tmp_path / "empty"; empty.mkdir()
    monkeypatch.setattr(sem, "_DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(sem, "_DEPS_OK", True)
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        r = SemanticProvider().build_result("search", "x", [], 0, str(empty))
    assert r["result"] is None
    assert r["reason"] == "no-index"


# --- Bug #5: .codeintel.toml values are actually threaded into the query -----

def test_config_cosine_floor_reaches_searcher(tmp_path, monkeypatch):
    import codeintel.providers.semantic as sem
    (tmp_path / ".codeintel.toml").write_text(
        'cosine_floor = 0.99\nrerank = "off"\nrerank_candidates = 7\n'
    )
    (tmp_path / "code.py").write_text("def f():\n    return 1\n")
    monkeypatch.setattr(sem, "_DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(sem, "_DEPS_OK", True)

    captured = {}
    real_search = Searcher.search

    def spy(self, query, project_root, k=10, cosine_floor=0.25, rerank="on", rerank_candidates=30):
        captured.update(floor=cosine_floor, rerank=rerank, rerank_candidates=rerank_candidates)
        return real_search(self, query, project_root, k=k, cosine_floor=cosine_floor,
                           rerank=rerank, rerank_candidates=rerank_candidates)

    monkeypatch.setattr(Searcher, "search", spy)
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        SemanticProvider().build_result("search", "f", [], 0, str(tmp_path))
    # every semantic knob from .codeintel.toml must reach the searcher, not just cosine_floor
    assert captured.get("floor") == 0.99
    assert captured.get("rerank") == "off"
    assert captured.get("rerank_candidates") == 7


# --- Bug #8: HTTP transport 400s a malformed Content-Length (never crashes) --

def test_http_bad_content_length_returns_400():
    import socket
    import threading
    import time

    from codeintel.http_server import CodeIntelHTTPServer, _Handler

    srv = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.handle_request, daemon=True).start()
    time.sleep(0.1)
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(
            b"POST /code/query HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: not-a-number\r\nConnection: close\r\n\r\n"
        )
        status_line = s.recv(4096).decode(errors="replace").split("\r\n")[0]
        s.close()
    finally:
        srv.server_close()
    assert "400" in status_line
