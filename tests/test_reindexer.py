"""Unit tests for Reindexer: debounce, off-thread, config gate, never-raise."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from codeintel.reindexer import Reindexer


@pytest.fixture(autouse=True)
def _clear_reindex_env(monkeypatch):
    monkeypatch.delenv("CODEINTEL_REINDEX", raising=False)


def _drain(r: Reindexer) -> None:
    """Wait for all submitted background work to finish."""
    r._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Test 1: background thread fires and calls Indexer.index
# ---------------------------------------------------------------------------

def test_maybe_reindex_fires_background_thread():
    with patch("codeintel.indexer.Indexer") as MockIndexer, \
         patch("codeintel.semantic_db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        r = Reindexer(debounce_seconds=0)
        r.maybe_reindex("/tmp/test")
        _drain(r)

        MockIndexer.return_value.index.assert_called_once_with("/tmp/test")


# ---------------------------------------------------------------------------
# Test 2: second call within debounce window is a no-op
# ---------------------------------------------------------------------------

def test_debounce_suppresses_second_call():
    with patch("codeintel.indexer.Indexer") as MockIndexer, \
         patch("codeintel.semantic_db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        r = Reindexer(debounce_seconds=1000)
        r.maybe_reindex("/tmp/test")
        r.maybe_reindex("/tmp/test")
        _drain(r)

        assert MockIndexer.return_value.index.call_count <= 1


# ---------------------------------------------------------------------------
# Test 3: CODEINTEL_REINDEX=off disables indexing entirely
# ---------------------------------------------------------------------------

def test_disabled_via_env():
    with patch.dict(os.environ, {"CODEINTEL_REINDEX": "off"}), \
         patch("codeintel.indexer.Indexer") as MockIndexer, \
         patch("shutil.which", return_value=None):
        r = Reindexer()
        r.maybe_reindex("/tmp/test")
        _drain(r)

        MockIndexer.return_value.index.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: bad path does not raise
# ---------------------------------------------------------------------------

def test_never_raises_on_bad_path():
    with patch("codeintel.semantic_db.SemanticDb") as MockDb, \
         patch("codeintel.indexer.Indexer"), \
         patch("shutil.which", return_value=None):
        MockDb.return_value.init.side_effect = FileNotFoundError("no such path")

        r = Reindexer(debounce_seconds=0)
        result = r.maybe_reindex("/nonexistent/path/xyz")
        _drain(r)

        assert result is None


# ---------------------------------------------------------------------------
# Test 5: Indexer.index raising does not propagate
# ---------------------------------------------------------------------------

def test_never_raises_on_indexer_exception():
    with patch("codeintel.indexer.Indexer") as MockIndexer, \
         patch("codeintel.semantic_db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        MockIndexer.return_value.index.side_effect = RuntimeError("boom")

        r = Reindexer(debounce_seconds=0)
        r.maybe_reindex("/tmp/test")
        _drain(r)
        # reaching here without exception confirms the never-raise contract
