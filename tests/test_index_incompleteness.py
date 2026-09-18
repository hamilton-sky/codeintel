"""An index that was never finished being built must not answer like a whole one.

MEASURED 2026-09-18, by SIGKILLing `codeintel index` at 256 of 600 chunks and then asking what it
left behind:

    is the database torn?            NO — 256 chunk_hashes, 256 vectors, `integrity_check ok`.
                                     Embeddings commit per 32-chunk batch, each vector beside its
                                     hash, so a kill lands between batches and never inside one.
    is there a durable record?       YES, already. `project_index_meta` is written only when a
                                     pass COMPLETES, so rows-present-and-no-timestamp is not an
                                     ambiguous state: it is a pass that started and died.
    does the next pass resume?       YES, exactly — it embedded the missing 344, reached 600, and
                                     recorded completion. The index is self-healing.
    did anything SAY so?             No. The query came back `confidence: complete`, `gaps: None`,
                                     and a symbol that exists in the tree and had never been
                                     embedded was simply absent from the answer.

That last line is the defect, and it is the one this project has already closed twice in other
engines: `## References (0)` at `confidence: complete`, and an `Ok([])` that meant the call
succeeded rather than that the world was empty. Nothing had to be made durable to fix it — the
durable record was on disk the whole time and only `codeintel status` was reading it.

WHY IT IS WORSE THAN A STALE HIT. Staleness is detectable by looking at what came back: the
searcher re-reads each hit's span and drops the ones whose source moved. An absent chunk cannot be
detected that way at all, because there is nothing to look at. Ranking, scoring and verification
are all perfectly correct over a corpus that is missing an unknown fraction of the repository.

The cry-wolf guard is `test_a_complete_index_raises_no_gap`. A disclosure that fires on healthy
answers is how a real one stops being read, and this one would otherwise fire on every query.
"""
from __future__ import annotations

import os
import pathlib

import numpy as np
import pytest

from codeintel.providers.semantic import SemanticProvider


class _FlatEmbedding:
    """Deterministic and content-addressed, so hits are stable without being identical."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(384, dtype=np.float32)
            v[0] = 1.0
            v[1] = (abs(hash(t)) % 89) / 890.0
            out.append(v)
        return out


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A small indexed repository in an isolated CODEINTEL_HOME."""
    monkeypatch.setenv("CODEINTEL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    src = tmp_path / "repo"
    src.mkdir()
    for i in range(6):
        (src / f"mod{i}.py").write_text(
            f"def settle_ledger_{i}(payload):\n"
            f"    '''Reconcile the shipment ledger for batch {i}.'''\n"
            f"    return payload\n"
        )
    from unittest.mock import patch

    from codeintel.indexer import Indexer
    from codeintel.semantic_db import SemanticDb, default_db_path

    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = SemanticDb(str(default_db_path()))
        db.init()
        Indexer(db).index(str(src))
        db.close()
    return src


def _forget_completion(repo_path: pathlib.Path) -> None:
    """Exactly what the SIGKILL left: chunks present, completion record absent."""
    from codeintel.semantic_db import SemanticDb, default_db_path

    db = SemanticDb(str(default_db_path()))
    db.init()
    db.conn().execute("DELETE FROM project_index_meta WHERE project_root = ?",
                      (os.path.realpath(str(repo_path)),))
    db.conn().commit()
    db.close()


def _search(repo_path: pathlib.Path, query: str = "reconcile the shipment ledger") -> dict:
    from unittest.mock import patch
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        return SemanticProvider().build_result("search", query, [], 0, str(repo_path))


def _gap(env: dict, kind: str) -> dict | None:
    return next((g for g in (env.get("gaps") or []) if g.get("kind") == kind), None)


# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_a_half_built_index_makes_the_answer_partial(repo):
    """The fix. An answer drawn from an index that covers an unknown fraction of the repository
    is `partial`, and the gap says which fraction is known and what to run."""
    _forget_completion(repo)

    env = _search(repo)

    assert env["confidence"] == "partial", env.get("gaps")
    gap = _gap(env, "index-incomplete")
    assert gap is not None, env.get("gaps")
    assert gap["section"] == "coverage"
    assert "did not finish" in gap["detail"]
    assert "codeintel index" in gap["detail"]


def test_a_complete_index_raises_no_gap(repo):
    """The cry-wolf guard, and the reason this is keyed on the completion record rather than on
    anything cheaper. A disclosure that fires on every healthy query is how a real one stops being
    read — the argument `_evidence_headline` and the first screen both make for their own silence."""
    env = _search(repo)

    assert _gap(env, "index-incomplete") is None, env.get("gaps")
    assert env["confidence"] == "complete", env.get("gaps")


def test_the_gap_counts_the_chunks_the_query_actually_searched(repo):
    """The number in a disclosure has to be about the thing it is disclosing. It is read off the
    searcher's own row count, not a second count that could disagree with the corpus that answered."""
    _forget_completion(repo)

    env = _search(repo)
    gap = _gap(env, "index-incomplete")

    from codeintel.searcher import Searcher
    from codeintel.semantic_db import SemanticDb, default_db_path
    db = SemanticDb(str(default_db_path()))
    db.init()
    actual = Searcher(db)._row_count(os.path.realpath(str(repo)))
    db.close()

    assert f"{actual} chunk" in gap["detail"], (gap["detail"], actual)


def test_a_repository_that_was_never_indexed_is_not_reported_as_incomplete(tmp_path, monkeypatch):
    """`never indexed` and `half indexed` are different facts with different remedies, and the
    provider already has a `no-index` reason for the first. Reporting an empty index as an
    interrupted pass would send someone to finish a pass that never started."""
    monkeypatch.setenv("CODEINTEL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()

    from codeintel.semantic_db import SemanticDb, default_db_path
    db = SemanticDb(str(default_db_path()))
    db.init()

    class _NoIndex:
        def has_index(self, _root):
            return False

    assert SemanticProvider._incomplete_index(db, _NoIndex(), str(empty)) is None
    db.close()


def test_nothing_matched_from_a_half_built_index_says_unknown_rather_than_absent(
        monkeypatch, tmp_path):
    """The most dangerous sentence this provider can produce. A null result carries no `gaps` — by
    design, `reason` is the whole story there — so the caveat has to ride the hint, and it has to
    say UNKNOWN rather than letting `below-floor` read as a fact about the repository."""
    import codeintel.providers.semantic as sem

    class FakeSearcher:
        def __init__(self, db, model_name=None):
            self.last_stale = 0
            self.last_unverifiable = 0
            self.last_query_error = None

        def has_index(self, project_root):
            return True

        def _row_count(self, _root):
            return 256

        def search(self, *a, **k):
            return []

    class FakeDb:
        def __init__(self, *a, **k):
            pass

        def init(self):
            pass

        def close(self):
            pass

        def indexed_at(self, _root):
            return None          # the interrupted pass

    monkeypatch.setattr(sem, "_DEPS_OK", True, raising=False)
    monkeypatch.setattr("codeintel.searcher.Searcher", FakeSearcher)
    monkeypatch.setattr("codeintel.semantic_db.SemanticDb", FakeDb)

    env = SemanticProvider().build_result("search", "anything", [], 0, str(tmp_path))

    assert env["result"] is None and env["reason"] == "below-floor"
    hint = env.get("hint") or ""
    assert "did not finish" in hint and "UNKNOWN" in hint, hint
    assert "256 chunks" in hint, hint


def test_the_incompleteness_check_never_raises(tmp_path):
    """Never-raise, like every other provider path. A provider that cannot answer "is this index
    whole?" must still answer the query — returning None means "no gap", the prior behaviour."""
    class _Exploding:
        def has_index(self, _root):
            raise RuntimeError("boom")

    assert SemanticProvider._incomplete_index(object(), _Exploding(), str(tmp_path)) is None


def test_the_guard_can_actually_fail(repo):
    """A disclosure that cannot fire records "we did not check" as green. Prove it bites: the
    fixture's completion record is what separates the two states, and removing it must change the
    answer's confidence."""
    before = _search(repo)["confidence"]
    _forget_completion(repo)
    after = _search(repo)["confidence"]

    assert (before, after) == ("complete", "partial"), (before, after)
