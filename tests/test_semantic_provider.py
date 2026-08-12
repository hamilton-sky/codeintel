"""Tests for SemanticProvider — covers all 4 USER_STORIES (7 test cases)."""
from __future__ import annotations

import hashlib
import struct
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from codeintel.providers.semantic import SemanticProvider
from codeintel.semantic_db import SemanticDb
from codeintel.indexer import Indexer
from codeintel.searcher import Searcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeTextEmbedding:
    """Deterministic stub: yields 384-dim numpy arrays filled with 0.1."""
    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        texts = list(texts)
        return [np.full(384, 0.1, dtype=np.float32) for _ in texts]


def _mem_db() -> SemanticDb:
    db = SemanticDb(":memory:")
    db.init()
    return db


class _ContentEmbedding:
    """Content-addressed stub: the vector depends on the text, so a chunk whose content changed
    yields a different embedding — lets a test detect whether a re-embed actually persisted
    (the constant-vector _FakeTextEmbedding above cannot)."""
    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        out = []
        for t in list(texts):
            h = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
            v = np.zeros(384, dtype=np.float32)
            v[0] = (h % 100000) / 100000.0
            v[1] = 1.0
            out.append(v)
        return out


def test_changed_chunk_reembeds_at_stable_chunk_id(tmp_path):
    # Regression: sqlite-vec's vec0 ignores INSERT OR REPLACE and raises UNIQUE, so re-embedding a
    # chunk whose content changed but whose chunk_id (def start line) is stable must go
    # DELETE-then-INSERT — else the stale vector is kept forever. Syntax chunking exposes this on
    # every function-body edit (the def line, hence the chunk_id, doesn't move).
    mod = tmp_path / "mod.py"
    mod.write_text("def f():\n    return 1\n")

    def _vec(db):
        row = db.conn().execute(
            "SELECT embedding FROM code_embeddings WHERE chunk_id LIKE ?", ("%:mod.py:0",)
        ).fetchone()
        return row[0] if row else None

    with patch("fastembed.TextEmbedding", _ContentEmbedding):
        db = _mem_db()
        idx = Indexer(db)
        assert idx.index(str(tmp_path)) > 0
        before = _vec(db)
        assert before is not None

        mod.write_text("def f():\n    return 999999\n")  # body changed, def line (chunk_id) stable
        assert idx.index(str(tmp_path)) > 0, "the changed chunk must re-embed, not silently fail"
        after = _vec(db)

    assert after is not None
    assert before != after, "the stored embedding must UPDATE for changed content on a vec0 table"


# ---------------------------------------------------------------------------
# Story 4: availability check
# ---------------------------------------------------------------------------

def test_available_when_deps_present(monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    assert SemanticProvider().available is True


# ---------------------------------------------------------------------------
# Story 1: op=search returns matches
# ---------------------------------------------------------------------------

def test_search_returns_matches(tmp_path, monkeypatch):
    (tmp_path / "sample.py").write_text("def greet():\n    return 'hello'\n")
    db_path = tmp_path / "semantic.db"
    monkeypatch.setattr("codeintel.providers.semantic._DB_PATH", db_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        result = SemanticProvider().build_result(
            "search", "greet function", [], 0, str(tmp_path)
        )

    assert result is not None
    assert result["result"] is not None
    assert "sample.py" in result["result"]


# ---------------------------------------------------------------------------
# Story 2: unchanged repo skips re-embed
# ---------------------------------------------------------------------------

def test_unchanged_repo_skips_embed(tmp_path):
    (tmp_path / "code.py").write_text("x = 1\n")

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        db = _mem_db()
        indexer = Indexer(db)
        first = indexer.index(str(tmp_path))
        second = indexer.index(str(tmp_path))

    assert first > 0, "first pass should embed at least one chunk"
    assert second == 0, "second pass should skip all unchanged chunks"


# ---------------------------------------------------------------------------
# Story 3a: empty index → safe null
# ---------------------------------------------------------------------------

def test_empty_index_safe_null(tmp_path, monkeypatch):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    db_path = tmp_path / "semantic.db"
    monkeypatch.setattr("codeintel.providers.semantic._DB_PATH", db_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        result = SemanticProvider().build_result(
            "search", "anything", [], 0, str(empty_dir)
        )

    assert result is not None
    assert result["result"] is None
    assert "reason" in result


# ---------------------------------------------------------------------------
# Story 3b: Searcher returns no matches → below-floor null
# ---------------------------------------------------------------------------

def test_below_floor_returns_none(tmp_path, monkeypatch):
    (tmp_path / "code.py").write_text("x = 1\n")
    db_path = tmp_path / "semantic.db"
    monkeypatch.setattr("codeintel.providers.semantic._DB_PATH", db_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding), \
         patch("codeintel.searcher.Searcher.search", return_value=[]):
        result = SemanticProvider().build_result(
            "search", "query", [], 0, str(tmp_path)
        )

    assert result is not None
    assert result["result"] is None
    assert result.get("reason") == "below-floor"


# ---------------------------------------------------------------------------
# Unsupported op → safe null
# ---------------------------------------------------------------------------

def test_unsupported_op_safe_null(monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    result = SemanticProvider().build_result("callers", "foo", [], 0, "/")
    assert result is not None
    assert result["result"] is None
    assert result.get("reason") == "op-not-supported"


def test_context_op_is_accepted(monkeypatch):
    # `context` (fan-out op) must be a valid semantic op — reaching the project-root check,
    # NOT rejected as op-not-supported (which would drop semantic from every context fan-out).
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    result = SemanticProvider().build_result("context", "foo", [], 0, "")
    assert result.get("reason") == "no-project-root"


# ---------------------------------------------------------------------------
# Never-raise invariant
# ---------------------------------------------------------------------------

def test_provider_never_raises(tmp_path, monkeypatch):
    db_path = tmp_path / "semantic.db"
    monkeypatch.setattr("codeintel.providers.semantic._DB_PATH", db_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("codeintel.semantic_db.SemanticDb.init", side_effect=RuntimeError("injected")):
        result = SemanticProvider().build_result(
            "search", "query", [], 0, str(tmp_path)
        )

    assert isinstance(result, dict)
    assert result.get("ok") is True
