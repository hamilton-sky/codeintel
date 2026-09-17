"""What one `search` COSTS, counted rather than timed.

`docs/benchmarks.md` carried a latency table and marked it stale, with an accurate diagnosis
attached: `_verify` now reads each candidate's file from disk inside the measured window, and
`rerank_candidates` defaults to 60 rather than the 30 the original design specified, so that read
set is twice what it was. Its own words for the position were "direction of the effect is knowable,
magnitude is not". Nothing failed when that happened. Nothing would fail if it happened again.

WHY THIS FILE COUNTS INSTEAD OF TIMING. A wall-clock assertion on a shared CI runner is a summary
whose referent is the RUNNER — true of that machine on that morning, and read as a claim about the
code. That is this repository's recurring defect with a stopwatch attached, and it buys flakiness
for it. The quantities below are the ones that actually regressed, and they are properties of the
algorithm: how many files a search opens, how many times it embeds, how many vector queries it
issues. They are identical on a laptop and on a cold shared runner, so they can be ASSERTED rather
than eyeballed, which is what "enforced" in the readiness doc's Phase 6 gate has to mean to be worth
writing.

WHAT THE BUDGET IS, and each line is one way the search path has been made slower before or could
be again:

    one file read per CANDIDATE      verification, rerank and the snippet share a single read of
                                     each candidate's span. A change that reads again for the
                                     snippet doubles the disk cost of every query, invisibly.
    reads flat in CORPUS SIZE        the doc's headline claim — "query latency is flat in corpus
                                     size" — is only true while the read set is bounded by the
                                     candidate limit rather than by the number of chunks indexed.
    one embed per SEARCH             the dominant cost on CPU (~230 ms of a ~250 ms query). Embedding
                                     per candidate rather than per query is a 60x regression that no
                                     functional test would notice.
    one vector query per SEARCH      an N+1 over candidates would not change a single result.

These are ceilings, not targets: the assertions are `<=` where a smaller number is an improvement,
and `==` only where any other value is a defect rather than a tuning choice.
"""
from __future__ import annotations

from typing import ClassVar
from unittest.mock import patch

import numpy as np
import pytest

from codeintel.indexer import Indexer
from codeintel.searcher import Searcher
from codeintel.semantic_db import SemanticDb


class _CountingEmbedding:
    """A deterministic embedder that records how many times it was asked to embed."""

    calls: ClassVar[list[int]] = []

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        batch = list(texts)
        _CountingEmbedding.calls.append(len(batch))
        out = []
        for t in batch:
            v = np.zeros(384, dtype=np.float32)
            # Content-addressed, so the cosine order is stable without being uniform — a corpus of
            # identical vectors would make the candidate set arbitrary and the counts meaningless.
            v[0] = 1.0
            v[1] = (hash(t) % 97) / 970.0
            out.append(v)
        return out


class _Counters:
    def __init__(self) -> None:
        self.reads = 0
        self.embeds = 0
        self.knn = 0


def _indexed(tmp_path, n_files: int):
    """A repo of `n_files` small modules, indexed into an in-memory db."""
    for i in range(n_files):
        (tmp_path / f"mod{i}.py").write_text(
            f"def handler_{i}(payload):\n"
            f"    '''Handle payload number {i} for the queue.'''\n"
            f"    return payload\n"
        )
    db = SemanticDb(":memory:")
    db.init()
    Indexer(db).index(str(tmp_path))
    return db


def _search_and_count(db, tmp_path, **kw) -> tuple[list[dict], _Counters]:
    """Run one search with every counted seam instrumented.

    Vector queries are counted with sqlite's own `set_trace_callback` rather than by wrapping
    `Cursor.execute`, which CPython refuses to patch (`immutable type`). The tracer is the
    supported seam and it sees every statement the connection runs, so an N+1 introduced anywhere
    beneath `search` is counted whether or not it goes through the code path a mock would cover.
    """
    import codeintel.searcher as searcher_mod

    counters = _Counters()
    real_open = searcher_mod.open_contained

    def counting_open(*a, **k):
        counters.reads += 1
        return real_open(*a, **k)

    def trace(sql: str) -> None:
        if "vec_distance_cosine" in (sql or ""):
            counters.knn += 1

    conn = db.conn()
    conn.set_trace_callback(trace)
    _CountingEmbedding.calls = []
    try:
        with patch.object(searcher_mod, "open_contained", counting_open):
            hits = Searcher(db).search("handle payload for the queue", str(tmp_path), **kw)
    finally:
        conn.set_trace_callback(None)
    counters.embeds = len(_CountingEmbedding.calls)
    return hits, counters


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The budget
# ══════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("candidates", [10, 20])
def test_a_search_reads_each_candidate_at_most_once(tmp_path, candidates):
    """Verification, rerank and the snippet share one read per candidate.

    `Searcher._verify` says so in its docstring — "each kept candidate carries its ``text``, so
    rerank and the snippet reuse this read instead of paying for their own" — and a docstring is
    not a guard. Reading again for the snippet would double the disk cost of every query and change
    no result, which is precisely the kind of regression that ships.
    """
    with patch("fastembed.TextEmbedding", _CountingEmbedding):
        db = _indexed(tmp_path, 40)
        _hits, c = _search_and_count(db, tmp_path, k=5, rerank_candidates=candidates)

    assert c.reads <= candidates, (
        f"{c.reads} file reads for a {candidates}-candidate search — the read set is no longer "
        f"one per candidate, so every query pays twice for the same span")


def test_the_read_set_does_not_grow_with_the_corpus(tmp_path_factory):
    """`docs/benchmarks.md` says query latency is flat in corpus size. This is that claim as a test.

    It holds only while the files opened per search are bounded by the CANDIDATE limit rather than
    by the number of chunks indexed. A change that verified every row, or that walked the index to
    build lexical statistics, would make the documented extrapolation to 100 k chunks false without
    failing anything else."""
    small, large = tmp_path_factory.mktemp("small"), tmp_path_factory.mktemp("large")
    with patch("fastembed.TextEmbedding", _CountingEmbedding):
        db_s = _indexed(small, 30)
        _h, cs = _search_and_count(db_s, small, k=5, rerank_candidates=20)
        db_l = _indexed(large, 90)
        _h, cl = _search_and_count(db_l, large, k=5, rerank_candidates=20)

    assert cl.reads == cs.reads, (
        f"a 3x larger corpus read {cl.reads} files where the smaller read {cs.reads} — the search "
        f"cost now scales with the index, and the extrapolation in docs/benchmarks.md does not hold")
    assert cl.knn == cs.knn == 1


def test_a_search_embeds_once_however_many_candidates_it_considers(tmp_path):
    """Embedding is ~230 ms of a ~250 ms query on CPU: it IS the latency, and the whole reason the
    documented figure is flat in corpus size. One embed per candidate would be a 60x regression
    that every functional test in this repository would still pass."""
    with patch("fastembed.TextEmbedding", _CountingEmbedding):
        db = _indexed(tmp_path, 40)
        _hits, c = _search_and_count(db, tmp_path, k=5, rerank_candidates=20)

    assert c.embeds == 1, f"{c.embeds} embedding passes for one query"
    assert _CountingEmbedding.calls == [1], (
        f"the query was embedded in batches of {_CountingEmbedding.calls}; one query is one text")


def test_a_search_issues_exactly_one_vector_query(tmp_path):
    """One KNN, not one per candidate. An N+1 here changes no result and no test — it only makes
    every query slower, which is the defect class this file exists for."""
    with patch("fastembed.TextEmbedding", _CountingEmbedding):
        db = _indexed(tmp_path, 40)
        _hits, c = _search_and_count(db, tmp_path, k=5, rerank_candidates=20)

    assert c.knn == 1, f"{c.knn} vector searches for one query"


def test_rerank_off_reads_only_what_it_returns(tmp_path):
    """The cheap path stays cheap. With rerank off the candidate set collapses to `k`, so the read
    set must too — otherwise turning rerank off buys nothing but a worse ordering."""
    with patch("fastembed.TextEmbedding", _CountingEmbedding):
        db = _indexed(tmp_path, 40)
        _hits, c = _search_and_count(db, tmp_path, k=5, rerank="off", rerank_candidates=60)

    assert c.reads <= 5, (
        f"{c.reads} reads for a rerank=off search returning 5 — the candidate widening is being "
        f"paid for even when it is switched off")


def test_the_budget_can_actually_fail(tmp_path):
    """A budget that cannot fail records "we did not check" as green.

    Prove the instrumentation bites by making the search path read each candidate a second time,
    exactly as a snippet re-read would, and showing the per-candidate assertion catches it."""
    import codeintel.searcher as searcher_mod

    with patch("fastembed.TextEmbedding", _CountingEmbedding):
        db = _indexed(tmp_path, 40)
        real_read = searcher_mod.Searcher._read_chunk

        def double_read(self, root_real, file_path, chunk_start, chunk_end=None):
            real_read(self, root_real, file_path, chunk_start, chunk_end)   # the regression
            return real_read(self, root_real, file_path, chunk_start, chunk_end)

        with patch.object(searcher_mod.Searcher, "_read_chunk", double_read):
            _hits, c = _search_and_count(db, tmp_path, k=5, rerank_candidates=20)

    assert c.reads > 20, (
        "the doubled read was not observed — this file's instrumentation is measuring nothing")
