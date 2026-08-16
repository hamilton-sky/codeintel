"""Unit tests for Reindexer: debounce, off-thread, config gate, never-raise."""
from __future__ import annotations

import logging
import os
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Test 6: background reindex threads are DAEMON — a one-shot CLI must not hang on
# exit waiting for a repo-wide reindex to finish.
# ---------------------------------------------------------------------------

def test_reindex_runs_on_daemon_threads():
    import threading as _threading

    slow_started = _threading.Event()
    release = _threading.Event()

    def _slow_index(_root):
        slow_started.set()
        release.wait(5)  # simulate a long reindex still in flight

    with patch("codeintel.indexer.Indexer") as MockIndexer, \
         patch("codeintel.semantic_db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        MockIndexer.return_value.index.side_effect = _slow_index
        r = Reindexer(debounce_seconds=0)
        r.maybe_reindex("/tmp/test")
        assert slow_started.wait(2), "reindex thread did not start"
        with r._executor._lock:
            live = [t for t in r._executor._threads if t.is_alive()]
        assert live, "expected a running reindex thread"
        assert all(t.daemon for t in live), "reindex threads must be daemon (must not block CLI exit)"
        release.set()
        _drain(r)


# --------------------------------------------------------------------------- graph reindex

class _FakeGraph:
    """Records what the reindexer asks the backend to do."""

    available = True

    def __init__(self, reply=None):
        self.calls: list[tuple] = []
        self._reply = reply

    def _run(self, tool, payload, timeout_ms):
        self.calls.append((tool, payload, timeout_ms))
        return self._reply


def test_graph_reindex_calls_the_tool_that_actually_reindexes(monkeypatch):
    """This called `detect_changes` with a `project_root` argument, and was wrong twice over: the
    backend takes `project` (a name from list_projects), so every call returned an argument error
    that `_run` folded into None — and `detect_changes` only REPORTS uncommitted drift, it never
    reindexes. The graph therefore never refreshed, silently, and queries kept answering from
    whatever the index held when it was first built.
    """
    from codeintel.reindexer import Reindexer

    fake = _FakeGraph({"status": "ok"})
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: fake)
    Reindexer()._graph_reindex("/repo")

    assert [c[0] for c in fake.calls] == ["index_repository"]
    # `repo_path` is the backend's actual parameter name. A mock cannot tell you that — this
    # assertion is only as good as the live check in test_graph_stdin.py that pins it.
    assert fake.calls[0][1] == {"repo_path": "/repo"}
    assert fake.calls[0][2] >= 120_000          # indexing a real repo outlasts a query budget


def test_graph_reindex_does_nothing_without_a_backend(monkeypatch):
    from codeintel.reindexer import Reindexer

    fake = _FakeGraph()
    fake.available = False
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: fake)
    Reindexer()._graph_reindex("/repo")

    assert fake.calls == []


@pytest.mark.parametrize("reply,expected", [
    ({"status": "error", "hint": "worker crashed on a file"}, "worker crashed on a file"),
    (None, "returned nothing"),
])
def test_graph_reindex_logs_a_backend_failure_instead_of_swallowing_it(monkeypatch, caplog, reply,
                                                                      expected):
    """Swallowing the backend's own error is what let a broken reindex look exactly like a working
    one for as long as nobody compared the graph to the source."""
    from codeintel.reindexer import Reindexer

    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: _FakeGraph(reply))
    with caplog.at_level(logging.WARNING):
        Reindexer()._graph_reindex("/repo")

    assert expected in caplog.text


def test_graph_reindex_never_raises(monkeypatch):
    """It runs on a background thread after an index; an exception here must not take that down."""
    from codeintel.reindexer import Reindexer

    monkeypatch.setattr("codeintel.providers.graph.GraphProvider",
                        lambda: (_ for _ in ()).throw(RuntimeError("backend exploded")))
    Reindexer()._graph_reindex("/repo")          # must return normally
