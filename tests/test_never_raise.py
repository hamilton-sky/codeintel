"""Fault-injection tests proving the never-raise invariant for codeintel F1."""
from __future__ import annotations

from codeintel.gateway import Gateway
from codeintel.providers.none import NoneProvider
from codeintel.server import code_query_handler

# ---------------------------------------------------------------------------
# Group 1: NoneProvider — None args
# ---------------------------------------------------------------------------

def test_none_provider_none_args():
    p = NoneProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 2: NoneProvider — wrong types
# ---------------------------------------------------------------------------

def test_none_provider_wrong_types():
    p = NoneProvider()
    r = p.build_result(123, [], {}, "bad", object())
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 3: NoneProvider — safe_null_result patched to raise
# ---------------------------------------------------------------------------

def test_none_provider_inner_catch(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr("codeintel.providers.none.safe_null_result", _boom)

    p = NoneProvider()
    r = p.build_result("symbol", "x", [], 0, "")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 4: Gateway — empty provider list
# ---------------------------------------------------------------------------

def test_gateway_no_providers():
    gw = Gateway([])
    r = gw.query("symbol", "x")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 5: Gateway — provider raises
# ---------------------------------------------------------------------------

class _RaisingProvider:
    def build_result(self, op, target, files, budget, project_root):
        raise RuntimeError("injected provider error")


def test_gateway_provider_raises():
    gw = Gateway([_RaisingProvider()])
    r = gw.query("symbol", "x")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 6: Gateway — provider returns None
# ---------------------------------------------------------------------------

class _NoneReturningProvider:
    def build_result(self, op, target, files, budget, project_root):
        return None


def test_gateway_provider_returns_none():
    gw = Gateway([_NoneReturningProvider()])
    r = gw.query("symbol", "x")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 7: code_query_handler — empty dict
# ---------------------------------------------------------------------------

# Both groups below pass an explicit, EMPTY `project_root`.
#
# Not decoration: a blank `project_root` now defaults to the server's cwd (the stdio affordance in
# `code_query_handler`), which during a test run is this repository — indexed, and large. Omitting
# it turned two malformed-input assertions into full live queries against the real backend, 20s and
# 10s of the suite's runtime, and made them fail slowly rather than fast whenever that backend was
# unhealthy. A tmp_path root exercises the identical never-raise path (resolution finds no project,
# every layer degrades to a safe-null) without making the invariant depend on how big or how
# well-indexed the checkout happens to be.
#
# The cwd default keeps its own coverage in `test_rbac.py`, where it is the behaviour under test
# rather than an accident of a missing argument.

def test_code_query_handler_empty_dict(tmp_path):
    r = code_query_handler({"project_root": str(tmp_path)})
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 8: code_query_handler — wrong types in args values
# ---------------------------------------------------------------------------

def test_code_query_handler_wrong_types(tmp_path):
    r = code_query_handler({"op": 999, "target": ["list", "value"],
                            "project_root": str(tmp_path)})
    assert r["ok"] is True


def test_code_query_handler_survives_a_genuinely_absent_project_root(tmp_path, monkeypatch):
    """The no-argument case still has to hold — it is just pinned without a live query.

    `{}` reaching this handler is what an agent sends when it has not read the schema, so the
    never-raise envelope has to cover it. Pointing cwd at an empty directory keeps that assertion
    exact while removing the accidental dependency on the checkout being indexed.
    """
    monkeypatch.chdir(tmp_path)
    r = code_query_handler({})
    assert r["ok"] is True


# ===========================================================================
# Groups 9-13: expanded never-raise invariant suite
# ===========================================================================

import json as _json
import shutil as _shutil
import threading
import urllib.request as _urllib_request

import codeintel.providers.semantic as _sem_mod
from codeintel.http_server import CodeIntelHTTPServer, _Handler
from codeintel.providers.graph import GraphProvider
from codeintel.providers.lsp import LspProvider
from codeintel.providers.semantic import SemanticProvider

# ---------------------------------------------------------------------------
# Group 9: GraphProvider — never-raise (None args, wrong types)
# ---------------------------------------------------------------------------

def test_graph_provider_none_args(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda *a: None)
    p = GraphProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


def test_graph_provider_wrong_types(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda *a: None)
    p = GraphProvider()
    r = p.build_result(123, [], {}, "bad", object())
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 10: LspProvider — never-raise (None args, wrong types)
# ---------------------------------------------------------------------------

def test_lsp_provider_none_args(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda *a: None)
    p = LspProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


def test_lsp_provider_wrong_types(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda *a: None)
    p = LspProvider()
    r = p.build_result(123, [], {}, "bad", object())
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 11: SemanticProvider — never-raise
# ---------------------------------------------------------------------------

def _semantic_boom_init(self):
    raise RuntimeError("injected")


def test_semantic_provider_none_args_deps_false(monkeypatch):
    monkeypatch.setattr(_sem_mod, "_DEPS_OK", False)
    p = SemanticProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


def test_semantic_provider_db_init_raises(monkeypatch):
    monkeypatch.setattr(_sem_mod, "_DEPS_OK", True)
    try:
        from codeintel.semantic_db import SemanticDb
        monkeypatch.setattr(SemanticDb, "init", _semantic_boom_init)
    except ImportError:
        pass
    p = SemanticProvider()
    r = p.build_result("search", "x", [], 0, "/tmp")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 11b: EmbeddingModelUnavailable — a NEW raising path inside the boundary
# ---------------------------------------------------------------------------
#
# `load_embedder` deliberately raises where the model is loaded, so that the failure can be
# classified at the operation rather than guessed at from its text afterwards. Every caller of it
# sits inside the never-raise contract, which means the new exception must be caught on each path
# it can escape through — a classification that reaches the caller as a traceback would be a
# strictly worse outcome than the bare "403 Forbidden" it replaces.

def _model_unavailable(*_a, **_k):
    from codeintel.semantic_db import DEFAULT_MODEL, EmbeddingModelUnavailable
    # The `remedy` argument is REQUIRED. Omitting it raised TypeError here instead, which every
    # boundary below caught anyway (they catch `Exception`) — so all four tests passed while
    # exercising none of the path they exist to pin. A fault-injection test that injects the
    # wrong fault is worse than no test: it reports coverage it does not have.
    raise EmbeddingModelUnavailable(
        DEFAULT_MODEL, RuntimeError("403 Forbidden"), "check network/proxy access",
    )


def _assert_is_model_unavailable():
    """Guard the guard: prove the injected fault is the one these tests claim to inject."""
    from codeintel.semantic_db import EmbeddingModelUnavailable
    try:
        _model_unavailable()
    except EmbeddingModelUnavailable:
        return
    except BaseException as exc:  # pragma: no cover - only on a regression
        raise AssertionError(f"injected {type(exc).__name__}, not EmbeddingModelUnavailable") from exc
    raise AssertionError("nothing raised")


def test_the_injected_fault_is_the_advertised_one():
    _assert_is_model_unavailable()


def test_indexer_index_absorbs_a_model_load_failure(monkeypatch, tmp_path):
    from codeintel.indexer import Indexer

    monkeypatch.setattr(Indexer, "_index", _model_unavailable)
    indexer = Indexer.__new__(Indexer)
    indexer.model_name = "BAAI/bge-small-en-v1.5"

    assert indexer.index(str(tmp_path)) == -1      # returns, never raises
    assert indexer.last_error


def test_searcher_embed_absorbs_a_model_load_failure(monkeypatch):
    from codeintel.searcher import Searcher

    monkeypatch.setattr(Searcher, "_get_embedder", _model_unavailable)
    s = Searcher.__new__(Searcher)
    s.model_name = "BAAI/bge-small-en-v1.5"
    s.last_query_error = None

    assert s._embed_query("anything") is None      # returns, never raises
    assert s.last_query_error


def test_semantic_provider_absorbs_a_model_load_failure(monkeypatch):
    """The full envelope path: the provider runs an inline index pass on a cold repo."""
    from codeintel.indexer import Indexer

    monkeypatch.setattr(_sem_mod, "_DEPS_OK", True)
    monkeypatch.setattr(Indexer, "_index", _model_unavailable)
    p = SemanticProvider()
    r = p.build_result("search", "x", [], 0, "/tmp")
    assert r["ok"] is True
    assert r["result"] is None


def test_reindexer_absorbs_a_model_load_failure(monkeypatch, tmp_path):
    """The background pass runs on a daemon thread, where an escape is invisible AND fatal to the
    pass — and the generation bump would still advance behind it."""
    from codeintel.indexer import Indexer
    from codeintel.reindexer import Reindexer

    monkeypatch.setattr(Indexer, "_index", _model_unavailable)
    r = Reindexer.__new__(Reindexer)
    r._semantic_reindex(str(tmp_path))             # returns, never raises


# ---------------------------------------------------------------------------
# Group 12: HTTP handler — never-raise (Gateway.query raises; live server)
# ---------------------------------------------------------------------------

def test_http_handler_never_raise(monkeypatch):
    def _raising(*a, **kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(Gateway, "query", _raising)

    server = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]

    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    body = _json.dumps({"op": "symbol", "target": "x"}).encode()
    req = _urllib_request.Request(
        f"http://127.0.0.1:{port}/code/query",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib_request.urlopen(req) as resp:
        data = _json.loads(resp.read())

    t.join(timeout=5)
    server.server_close()
    assert data["ok"] is True


# ---------------------------------------------------------------------------
# Group 13: Envelope shape — ok, op, target, result, engine, cached
# ---------------------------------------------------------------------------

_REQUIRED_ENVELOPE_KEYS = {"ok", "op", "target", "result", "engine", "cached"}


def test_envelope_shape_none_provider():
    p = NoneProvider()
    r = p.build_result("symbol", "x", [], 0, "")
    assert _REQUIRED_ENVELOPE_KEYS.issubset(r.keys())


def test_envelope_shape_graph_provider(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda *a: None)
    p = GraphProvider()
    r = p.build_result("symbol", "x", [], 0, "")
    assert _REQUIRED_ENVELOPE_KEYS.issubset(r.keys())


def test_envelope_shape_lsp_provider(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda *a: None)
    p = LspProvider()
    r = p.build_result("symbol", "x", [], 0, "")
    assert _REQUIRED_ENVELOPE_KEYS.issubset(r.keys())


def test_envelope_shape_semantic_provider(monkeypatch):
    monkeypatch.setattr(_sem_mod, "_DEPS_OK", False)
    p = SemanticProvider()
    r = p.build_result("search", "x", [], 0, "")
    assert _REQUIRED_ENVELOPE_KEYS.issubset(r.keys())


def test_envelope_shape_gateway():
    r = Gateway([]).query("symbol", "x")
    assert _REQUIRED_ENVELOPE_KEYS.issubset(r.keys())
