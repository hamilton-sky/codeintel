"""The two places the semantic engine answered a question it had never actually asked.

Both are the same defect the `index-failed` reason was added to fix, in places it did not reach:

* `Searcher.search()` returns `[]` for a search that FAULTED, exactly as it does for a genuine
  miss. The provider read that as `below-floor` — "a non-empty index yielded no match" — which an
  agent is told to read as "this code does not exist". Two stages can fault: the query embedding
  (a cold model cache behind a proxy) and the vector search itself (a corrupt index, an unusable
  sqlite-vec extension). Both were a confident claim about the repository from a query that
  errored.
* `doctor` asked "installed? runnable? is THIS repo indexed?" and reported "not indexed" whether
  nobody had run `index` yet or the weights every index pass needs had never been fetched and the
  host that serves them is unreachable. Two states, opposite fixes, one row.
"""
from __future__ import annotations

import os

import pytest

from codeintel.providers.semantic import SemanticProvider
from codeintel.searcher import Searcher
from codeintel.semantic_db import DEFAULT_MODEL, model_cache_dir, model_is_cached


class ProxyError(Exception):
    pass


# --------------------------------------------------------------------------------------------- #
# A query that could not be embedded is a could-not-ask, not a finding
# --------------------------------------------------------------------------------------------- #

def test_searcher_records_why_a_search_could_not_run(monkeypatch):
    """An UNCLASSIFIED embed failure: the type is kept, and no model story is invented.

    The embedding-model explanation belongs only to `EmbeddingModelUnavailable`, which is raised
    where the model is loaded — see `tests/test_index_failure_reason.py`. A raw transport error
    from an embedder that already loaded is a different fault with a different fix, and attaching
    the download story to it is exactly the false positive that classification removed.
    """
    s = Searcher.__new__(Searcher)
    s.model_name = DEFAULT_MODEL
    s.last_query_error = None
    monkeypatch.setattr(
        Searcher, "_get_embedder",
        lambda self: (_ for _ in ()).throw(ProxyError("403 Forbidden")),
    )

    assert s._embed_query("anything") is None
    assert s.last_query_error is not None
    assert "ProxyError: 403 Forbidden" in s.last_query_error
    # Stage-qualified: two stages set this field and their fixes are unrelated, so "which step
    # failed" has to survive into the message a reader actually sees.
    assert "embedding the query failed" in s.last_query_error
    assert "huggingface.co" not in s.last_query_error


def test_a_faulted_vector_search_is_recorded_too(tmp_path, monkeypatch):
    """The KNN path: a corrupt index or unusable sqlite-vec is not "nothing matched"."""
    s = Searcher.__new__(Searcher)
    s.model_name = DEFAULT_MODEL
    s.last_stale = s.last_unverifiable = 0
    s.last_query_error = None
    s._embedder = None
    s._row_count = lambda root: 5                       # a non-empty index for this project
    monkeypatch.setattr(Searcher, "_embed_query", lambda self, q: b"\x00" * 4)

    class _Conn:
        def execute(self, *a, **k):
            raise RuntimeError("no such function: vec_distance_cosine")

    s.db = type("_Db", (), {"conn": lambda self: _Conn()})()

    assert s.search("where is auth handled", str(tmp_path)) == []
    assert s.last_query_error is not None
    assert "the vector search failed" in s.last_query_error
    assert "vec_distance_cosine" in s.last_query_error   # the cause, not just the stage
    # It must NOT be explained as a model download — that is the other stage's fix entirely.
    assert "huggingface.co" not in s.last_query_error


def test_a_previous_failure_does_not_leak_into_a_later_search(tmp_path, monkeypatch):
    """`last_query_error` must describe THIS search — a Searcher is deliberately reused."""
    s = Searcher.__new__(Searcher)
    s.model_name = DEFAULT_MODEL
    s.last_query_error = "ProxyError: stale, from an earlier query"
    s.last_stale = 3
    s.last_unverifiable = 2
    monkeypatch.setattr(Searcher, "_row_count", lambda self, root: 0)

    assert s.search("", str(tmp_path)) == []          # earliest possible return
    assert s.last_query_error is None


def _provider_with_searcher(monkeypatch, tmp_path, **searcher_state):
    """A SemanticProvider whose Searcher reports an index but returns no matches."""
    import codeintel.providers.semantic as sem

    class FakeSearcher:
        def __init__(self, db, model_name=DEFAULT_MODEL):
            self.last_stale = searcher_state.get("last_stale", 0)
            self.last_unverifiable = 0
            self.last_query_error = searcher_state.get("last_query_error")

        def has_index(self, project_root):
            return True

        def search(self, *a, **k):
            return []

    class FakeDb:
        def __init__(self, *a, **k):
            pass

        def init(self):
            pass

    monkeypatch.setattr(sem, "_DEPS_OK", True, raising=False)
    monkeypatch.setattr("codeintel.searcher.Searcher", FakeSearcher)
    monkeypatch.setattr("codeintel.semantic_db.SemanticDb", FakeDb)
    return SemanticProvider()


def test_an_unembeddable_query_is_query_failed_not_below_floor(monkeypatch, tmp_path):
    """The whole point: `below-floor` here would be a confident claim about the repository."""
    p = _provider_with_searcher(
        monkeypatch, tmp_path,
        last_query_error="ProxyError: 403 Forbidden — blocked download of the weights",
    )
    r = p.build_result("search", "parse_config", [], 0, str(tmp_path))

    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "query-failed"
    assert "403 Forbidden" in r["hint"]
    assert "NOT evidence" in r["hint"]


def test_the_provider_reports_either_failed_stage_the_same_way(monkeypatch, tmp_path):
    """`query-failed` is the reason for both; the stage travels in the searcher's own message."""
    p = _provider_with_searcher(
        monkeypatch, tmp_path,
        last_query_error=("the vector search failed — OperationalError: database disk image is "
                          "malformed. The index may be unreadable"),
    )
    r = p.build_result("search", "parse_config", [], 0, str(tmp_path))

    assert r["reason"] == "query-failed"
    assert "the vector search failed" in r["hint"]
    assert "malformed" in r["hint"]
    assert "NOT evidence" in r["hint"]


def test_a_genuine_miss_is_still_below_floor(monkeypatch, tmp_path):
    """The new reason must not swallow the case it was carved out of."""
    p = _provider_with_searcher(monkeypatch, tmp_path)
    r = p.build_result("search", "parse_config", [], 0, str(tmp_path))
    assert r["reason"] == "below-floor"


def test_a_failed_query_outranks_a_staleness_count(monkeypatch, tmp_path):
    """Staleness counts describe a search that ran; this one did not."""
    p = _provider_with_searcher(
        monkeypatch, tmp_path, last_query_error="ProxyError: 403", last_stale=4,
    )
    r = p.build_result("search", "parse_config", [], 0, str(tmp_path))
    assert r["reason"] == "query-failed"


def test_query_failed_is_in_the_gateways_unreachable_set():
    """A fan-out where it is the only outcome must summarise as engines-unavailable.

    Otherwise `context` reports `no-result` — "that symbol does not exist" — from a fan-out where
    the only engine asked could not embed the question.
    """
    from codeintel.gateway import Gateway

    class _Null:
        def __init__(self, engine, reason):
            self.engine, self.reason = engine, reason
            self.available = True

        def build_result(self, op, target, files, budget, project_root):
            return {"ok": True, "op": op, "target": target, "result": None,
                    "engine": self.engine, "cached": False, "reason": self.reason}

    gw = Gateway(graph=_Null("graph", "engine-unavailable"),
                 lsp=_Null("lsp", "boot-failed"),
                 semantic=_Null("semantic", "query-failed"))
    # `engine="all"` is what puts semantic in the `context` fan-out, so its reason reaches the set.
    r = gw.query(op="context", target="parse_config", engine="all", project_root=os.getcwd())
    assert r["reason"] == "engines-unavailable"
    assert "semantic: query-failed" in r["hint"]
    assert "NOT evidence the target does not exist" in r["hint"]


# --------------------------------------------------------------------------------------------- #
# doctor's fourth question: are the weights actually on this machine?
# --------------------------------------------------------------------------------------------- #

def test_model_cache_dir_matches_fastembeds_own_resolution(monkeypatch, tmp_path):
    """Pinned against fastembed rather than asserted from memory — it is duplicated on purpose.

    `define_cache_dir` mkdirs as a side effect, which a read-only probe must not do, so the
    resolution is re-implemented; this keeps the copy honest.
    """
    fastembed_utils = pytest.importorskip("fastembed.common.utils")

    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "explicit"))
    assert model_cache_dir() == str(tmp_path / "explicit")
    assert os.path.realpath(model_cache_dir()) == os.path.realpath(
        fastembed_utils.define_cache_dir()
    )

    monkeypatch.delenv("FASTEMBED_CACHE_PATH")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    assert os.path.realpath(model_cache_dir()) == os.path.realpath(
        fastembed_utils.define_cache_dir()
    )


def test_probing_the_cache_never_creates_it(monkeypatch, tmp_path):
    """A probe that mutates what it measures answers its own question wrongly next time."""
    target = tmp_path / "not-yet"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(target))

    assert model_is_cached() is False
    assert not target.exists(), "doctor must not bring the cache directory into existence"


def test_cached_weights_are_recognised(monkeypatch, tmp_path):
    """Both fastembed layouts carry the model slug in the directory name."""
    for layout in ("bge-small-en-v1.5",                        # GCS tarball
                   "models--qdrant--bge-small-en-v1.5-onnx-q/snapshots/abc"):  # HF snapshot
        cache = tmp_path / layout.replace("/", "_")[:12] / "cache"
        d = cache / layout
        d.mkdir(parents=True)
        (d / "model_optimized.onnx").write_bytes(b"\x00")
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
        assert model_is_cached() is True, layout


def test_a_different_models_weights_do_not_count_as_this_one(monkeypatch, tmp_path):
    """"Some .onnx exists" is the collapse this check is here to avoid."""
    cache = tmp_path / "cache"
    (cache / "models--qdrant--all-MiniLM-L6-v2-onnx").mkdir(parents=True)
    (cache / "models--qdrant--all-MiniLM-L6-v2-onnx" / "model.onnx").write_bytes(b"\x00")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))

    assert model_is_cached("BAAI/bge-small-en-v1.5") is False
    assert model_is_cached("sentence-transformers/all-MiniLM-L6-v2") is True


def test_an_unreadable_cache_is_unknown_not_missing(monkeypatch):
    """Claiming the weights are absent because we could not look sends the reader to the wrong fix."""
    import codeintel.semantic_db as sdb

    monkeypatch.setattr(sdb.os, "walk", lambda *a, **k: (_ for _ in ()).throw(OSError("EACCES")))
    monkeypatch.setattr(sdb.os.path, "isdir", lambda p: True)
    assert model_is_cached() is None


def test_doctor_separates_never_indexed_from_never_downloaded(monkeypatch, tmp_path):
    """The support burden this exists to end: "it's installed, why doesn't it work"."""
    from codeintel.doctor import _status_for

    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "empty"))
    probe = SemanticProvider().probe(str(tmp_path))

    assert probe["installed"] is True
    assert probe["model_cached"] is False
    assert "not cached yet" in probe["detail"]
    assert "huggingface.co" in probe["detail"]
    assert "FASTEMBED_CACHE_PATH" in probe["remediation"]
    # The repo is also unindexed here, so the row still fails — but it now says WHY the index
    # would fail, which is the part that was missing.
    assert _status_for(probe) == "fail"


def test_uncached_weights_alone_are_a_warn_not_a_failure():
    """On a connected machine this is a download that has not happened yet."""
    from codeintel.doctor import _status_for

    healthy = {"installed": True, "runnable": True, "repo_indexed": True, "model_cached": True}
    assert _status_for(healthy) == "ok"
    assert _status_for({**healthy, "model_cached": False}) == "warn"
    assert _status_for({**healthy, "model_cached": None}) == "ok"   # unknown makes no claim
