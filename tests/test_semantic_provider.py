"""Tests for SemanticProvider — covers all 4 USER_STORIES (7 test cases)."""
from __future__ import annotations

import hashlib
import threading
import time
from unittest.mock import patch

import numpy as np
import pytest

from codeintel.indexer import Indexer
from codeintel.providers import semantic as semantic_mod
from codeintel.providers.semantic import SemanticProvider
from codeintel.semantic_db import SemanticDb

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
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
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
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
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
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
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
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("codeintel.semantic_db.SemanticDb.init", side_effect=RuntimeError("injected")):
        result = SemanticProvider().build_result(
            "search", "query", [], 0, str(tmp_path)
        )

    assert isinstance(result, dict)
    assert result.get("ok") is True


@pytest.mark.parametrize("snippet,expected", [
    ("---\n\nconnect(wsUrl, token)\n", "connect(wsUrl, token)"),
    ("\n\n  def handler():\n", "def handler():"),
    ("#\n===\nreal content here\n", "real content here"),
    ("first line wins\nsecond\n", "first line wins"),
])
def test_a_result_preview_shows_a_line_worth_reading(snippet, expected):
    """The preview used line one unconditionally, so a hit whose chunk opened with a blank line or
    a `---` fence rendered as `path:line | ---` — a result the reader cannot judge without opening
    the file, which is the one thing this output exists to avoid."""
    from codeintel.providers.semantic import _first_meaningful_line

    assert _first_meaningful_line(snippet) == expected


# ---------------------------------------------------------------------------
# Cold-index stall fix: a non-blocking provider (the MCP/HTTP server) must return promptly on a
# cold repo instead of running the full inline index pass, and the pass it kicks off in the
# background must actually land, be de-duplicated per project root, and survive a crash.
# ---------------------------------------------------------------------------

_DEFAULT_INDEXER_KWARGS = {
    "model_name": "BAAI/bge-small-en-v1.5", "window": 20, "stride": 10,
    "max_chunks": 500, "max_total_chunks": 100000, "chunk_strategy": "syntax",
}


def _wait_until_not_indexing(project_root: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while semantic_mod._background_index_elapsed_s(project_root) is not None:
        if time.monotonic() > deadline:
            raise AssertionError(f"background index for {project_root} never finished")
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _clear_background_index_state():
    """The in-flight registry is module-level (by design — see `_background_index_elapsed_s`'s
    docstring), so a test that fails mid-poll must not leave a stale entry for the next test."""
    yield
    with semantic_mod._BG_INDEX_LOCK:
        semantic_mod._BG_INDEX_STARTED.clear()


def test_cold_repo_non_blocking_returns_fast_then_serves_the_retry(tmp_path, monkeypatch):
    (tmp_path / "sample.py").write_text("def greet():\n    return 'hello'\n")
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        provider = SemanticProvider(blocking_index=False)
        result = provider.build_result("search", "greet function", [], 0, str(tmp_path))

        # Promptly: no `result`, a reason that says what's happening, and a hint with the fallback.
        assert result["ok"] is True
        assert result["result"] is None
        assert result["reason"] == "indexing-in-progress"
        assert "codeintel index" in result["hint"]

        _wait_until_not_indexing(str(tmp_path))

        retry = provider.build_result("search", "greet function", [], 0, str(tmp_path))

    assert retry["result"] is not None
    assert "sample.py" in retry["result"]


def test_start_background_index_dedupes_concurrent_calls_for_the_same_root(tmp_path, monkeypatch):
    started = threading.Event()
    proceed = threading.Event()
    calls = {"n": 0}

    def _fake_index(self, project_root):
        calls["n"] += 1
        started.set()
        proceed.wait(timeout=5)
        return 1

    monkeypatch.setattr("codeintel.indexer.Indexer.index", _fake_index)
    root = str(tmp_path)
    db_path = str(tmp_path / "semantic.db")

    first = semantic_mod._start_background_index(root, db_path, _DEFAULT_INDEXER_KWARGS)
    assert started.wait(timeout=5), "background thread never ran"
    second = semantic_mod._start_background_index(root, db_path, _DEFAULT_INDEXER_KWARGS)

    assert first is True
    assert second is False, "a second call for a project root already indexing must be a no-op"

    proceed.set()
    _wait_until_not_indexing(root)
    assert calls["n"] == 1, "only one background pass should have actually run"


def test_background_index_crash_does_not_wedge_the_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.indexer.Indexer.index",
                         lambda self, project_root: (_ for _ in ()).throw(RuntimeError("boom")))
    root = str(tmp_path)
    db_path = str(tmp_path / "semantic.db")

    assert semantic_mod._start_background_index(root, db_path, _DEFAULT_INDEXER_KWARGS) is True
    _wait_until_not_indexing(root)  # the crash must still clear the in-flight marker

    # Wedged state would make this return False forever; a fresh attempt must be allowed to start.
    monkeypatch.setattr("codeintel.indexer.Indexer.index", lambda self, project_root: 0)
    assert semantic_mod._start_background_index(root, db_path, _DEFAULT_INDEXER_KWARGS) is True
    _wait_until_not_indexing(root)


def test_blocking_provider_still_indexes_inline_on_a_cold_repo(tmp_path, monkeypatch):
    """Default construction (every existing caller, including the CLI) must be unaffected: a cold
    repo is still indexed synchronously, in the same call, with no background thread involved."""
    (tmp_path / "sample.py").write_text("def greet():\n    return 'hello'\n")
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        result = SemanticProvider(blocking_index=True).build_result(
            "search", "greet function", [], 0, str(tmp_path)
        )

    assert result["result"] is not None
    assert "sample.py" in result["result"]
    assert semantic_mod._background_index_elapsed_s(str(tmp_path)) is None


def test_probe_reports_indexing_in_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    monkeypatch.setattr("codeintel.semantic_db.default_db_path",
                         lambda *a, **k: str(tmp_path / "missing.db"))
    root = str(tmp_path)

    with semantic_mod._BG_INDEX_LOCK:
        semantic_mod._BG_INDEX_STARTED[semantic_mod._index_key(root)] = time.monotonic() - 5

    r = SemanticProvider().probe(root)

    assert r["installed"] is True and r["repo_indexed"] is False
    assert "indexing in progress" in r["detail"]
    assert "codeintel index" in r["remediation"]


# --- D2: an index pass that FAILED is not a repo nobody indexed ------------------------------
#
# `Indexer.index` returns -1 and parks the cause on `last_error` specifically so a caller can SHOW
# it rather than log it. The provider discarded both, so a blocked model download and a repo nobody
# had indexed yet produced the same `no-index` — with a hint telling the reader to run the very
# pass that had just failed. These two tests pin the pair, because the distinction only means
# something while BOTH sides hold.

class _FailingIndexer:
    """Stands in for a pass that ran and could not finish — a blocked model download, say."""

    def __init__(self, *_args, **_kwargs):
        self.last_error = "SSLError: certificate verify failed"

    def index(self, _project_root):
        return -1


def test_a_failed_inline_index_is_reported_as_index_failed_with_its_cause(tmp_path, monkeypatch):
    import codeintel.indexer as idx
    import codeintel.providers.semantic as sem

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("def make_widget(name):\n    return name\n")
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr(sem, "_DEPS_OK", True)
    monkeypatch.setattr(idx, "Indexer", _FailingIndexer)

    result = SemanticProvider().build_result("search", "widget factory", [], 0, str(repo))

    assert result["result"] is None
    assert result["reason"] == "index-failed", (
        "a pass that ran and failed must not be reported as `no-index` — that is the reason a "
        "reader takes as 'nobody has indexed this yet', which licenses the opposite next step"
    )
    # The cause travels in the answer, not only in a log line the reader already scrolled past.
    assert "certificate verify failed" in result["hint"]
    assert "NOT 'never indexed'" in result["hint"]


def test_an_index_pass_that_finds_nothing_still_reports_no_index(tmp_path, monkeypatch):
    """The other half. `no-index` keeps its documented meaning — the pass completed and there was
    nothing to embed — which is an answer about the repository, not a failure to ask."""
    import codeintel.providers.semantic as sem

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr(sem, "_DEPS_OK", True)
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        result = SemanticProvider().build_result("search", "x", [], 0, str(empty))

    assert result["result"] is None
    assert result["reason"] == "no-index"


def test_a_fanout_where_the_index_failed_is_not_evidence_the_symbol_is_absent() -> None:
    """The gateway's `_merge` is, by its own comment, "the one place the codebase throws away the
    could-not-ask / asked-and-found-nothing distinction it is otherwise careful to preserve".

    With `engine="all"` semantic joins the fan-out, so its reason reaches that set. Before
    `index-failed` was added to it, a run where the index pass simply failed summarised as
    `no-result` with no caveat — which an agent reads as "that symbol does not exist".
    """
    from codeintel.gateway import Gateway

    def stub(engine, reason):
        class _S:
            available = True

            def build_result(self, op, target, *_a, **_k):
                return {"ok": True, "op": op, "target": target, "engine": engine,
                        "result": None, "reason": reason, "cached": False}
        return _S()

    gw = Gateway(graph=stub("graph", "engine-unavailable"), lsp=stub("lsp", "boot-failed"),
                 semantic=stub("semantic", "index-failed"))
    r = gw.query(op="context", target="make_widget", engine="all", project_root="/x")

    assert r["reason"] == "engines-unavailable"
    assert "semantic: index-failed" in r["hint"]
    assert "NOT evidence the target does not exist" in r["hint"]


def test_a_fanout_where_the_repo_is_merely_unindexed_keeps_its_ordinary_summary() -> None:
    """The negative control, and the reason `no-index` was deliberately left OUT of that set: a
    completed pass that found nothing to embed IS an answer about the repository, so it must not
    borrow the "could not ask" caveat."""
    from codeintel.gateway import Gateway

    def stub(engine, reason):
        class _S:
            available = True

            def build_result(self, op, target, *_a, **_k):
                return {"ok": True, "op": op, "target": target, "engine": engine,
                        "result": None, "reason": reason, "cached": False}
        return _S()

    gw = Gateway(graph=stub("graph", "not-in-graph"), lsp=stub("lsp", "not-found"),
                 semantic=stub("semantic", "no-index"))
    r = gw.query(op="context", target="make_widget", engine="all", project_root="/x")

    assert r["reason"] == "no-result"
    assert "NOT evidence" not in (r.get("hint") or "")
