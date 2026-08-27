"""Staleness verification: a hit must still describe the code it was indexed from.

`chunk_hashes` stores a chunk's START LINE, and the snippet has always been re-read from the
CURRENT file at that line. Once a file was edited, a hit therefore pointed at whatever now occupies
those lines — a deleted `charge_credit_card()` at line 1 made "charge the credit card" return
`app.py:1 | import logging`, ranked first and reported as `confidence: complete`. An agent has no
way to doubt that, which makes it worse than an empty result.

The fix records each chunk's end line and re-hashes the real span at query time. These tests pin
the behaviour that matters: stale hits are withheld rather than shown, valid neighbours in the same
edited file survive, the omission is reported rather than hidden, and a cache written before the
column existed still answers (unverifiable, not broken).
"""
from __future__ import annotations

import math
from unittest.mock import patch

import numpy as np

from codeintel.indexer import Indexer
from codeintel.providers.semantic import SemanticProvider
from codeintel.searcher import Searcher
from codeintel.semantic_db import SemanticDb, chunk_content_hash, default_db_path


class _FlatEmbedding:
    """Every text at the same angle: cosine is uniform, so ordering never masks a drop/keep."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        v = np.zeros(384, dtype=np.float32)
        v[0] = 1.0
        return [v for _ in texts]


def _mem_db() -> SemanticDb:
    db = SemanticDb(":memory:")
    db.init()
    return db


_ORIGINAL = (
    "def charge_credit_card(token, amount):\n"      # line 0
    '    """Charge the customer\'s saved card."""\n'  # line 1
    "    return gateway.charge(token, amount)\n"    # line 2
    "\n"                                            # line 3
    "\n"                                            # line 4
    "def refund_payment(token, amount):\n"          # line 5
    '    """Refund a previous charge."""\n'         # line 6
    "    return gateway.refund(token, amount)\n"    # line 7
)

# charge_credit_card deleted; refund_payment kept at the SAME lines so its chunk stays valid.
_EDITED = (
    "import logging\n"                              # line 0  <- what the stale row now points at
    "logger = logging.getLogger(__name__)\n"        # line 1
    "\n"                                            # line 2
    "\n"                                            # line 3
    "\n"                                            # line 4
    "def refund_payment(token, amount):\n"          # line 5  <- unchanged span
    '    """Refund a previous charge."""\n'         # line 6
    "    return gateway.refund(token, amount)\n"    # line 7
)


# --------------------------------------------------------------------- the reported bug

def test_stale_chunk_is_not_returned_as_a_hit(tmp_path):
    """The regression. A deleted def must not resurface as whatever now sits on its old lines."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        before = Searcher(db).search("charge", str(tmp_path), cosine_floor=-1.0)
        assert 0 in {r["line"] for r in before}, "charge_credit_card should be indexed at line 0"

        (tmp_path / "app.py").write_text(_EDITED)  # edited, NOT re-indexed
        s = Searcher(db)
        after = s.search("charge", str(tmp_path), cosine_floor=-1.0)

    assert 0 not in {r["line"] for r in after}, "stale line-0 row must be withheld, not shown"
    assert all("import logging" not in r["snippet"] for r in after), \
        "a stale row must never render the unrelated code that now occupies its lines"
    assert s.last_stale == 1, "the drop must be counted so callers can report it"


def test_unchanged_chunk_in_an_edited_file_survives(tmp_path):
    """Verification must be per-chunk, not per-file — otherwise one edit blinds a whole module."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        (tmp_path / "app.py").write_text(_EDITED)
        res = Searcher(db).search("payment", str(tmp_path), cosine_floor=-1.0)

    kept = [r for r in res if r["line"] == 5]
    assert kept, "refund_payment's span is byte-identical and must still be searchable"
    assert "def refund_payment" in kept[0]["snippet"]


def test_provider_reports_the_omission_rather_than_hiding_it(tmp_path):
    """A thinned list served as a whole one is the failure this engine's gap contract exists for."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding), \
            patch("codeintel.semantic_db._base_dir", lambda: tmp_path / "cache"):
        p = SemanticProvider()
        p.build_result("search", "charge", [], 2000, str(tmp_path))  # cold pass builds the index
        (tmp_path / "app.py").write_text(_EDITED)
        res = p.build_result("search", "charge", [], 2000, str(tmp_path))

    assert res["confidence"] == "partial", "dropping hits makes the answer incomplete, by definition"
    freshness = [g for g in res["gaps"] if g["section"] == "freshness"]
    assert freshness, "the dropped chunk must be named in gaps, not silently swallowed"
    assert "index" in freshness[0]["detail"], "the gap must say how to fix it"


def test_all_hits_stale_is_reported_as_stale_not_as_absent(tmp_path):
    """`below-floor` reads as 'this code does not exist' — the worst thing to tell an agent here."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding), \
            patch("codeintel.semantic_db._base_dir", lambda: tmp_path / "cache"):
        p = SemanticProvider()
        p.build_result("search", "charge", [], 2000, str(tmp_path))
        (tmp_path / "app.py").write_text("x = 1\n")  # every indexed span now invalid
        res = p.build_result("search", "charge", [], 2000, str(tmp_path))

    assert res["result"] is None
    assert res["reason"] == "index-stale", "must not be conflated with 'nothing matched'"
    assert "codeintel index" in (res["hint"] or "")


# --------------------------------------------------------------------- migration / robustness

def test_legacy_rows_without_a_span_still_answer(tmp_path):
    """A cache written before `chunk_end` existed is unverifiable, NOT broken: it must keep
    serving results rather than treating every row as stale and going silent."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        db.conn().execute("UPDATE chunk_hashes SET chunk_end = NULL")  # simulate a pre-0.18 cache
        db.conn().commit()
        s = Searcher(db)
        res = s.search("charge", str(tmp_path), cosine_floor=-1.0)

    assert res, "legacy rows must still return hits"
    assert s.last_stale == 0, "an unverifiable row is not a stale row"
    assert s.last_unverifiable == len(res)


def test_index_pass_backfills_spans_without_re_embedding(tmp_path):
    """The migration path. Existing caches gain verification from an ordinary index pass — a
    drop-and-rebuild would force a full re-embed of every project sharing the cache file."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        db.conn().execute("UPDATE chunk_hashes SET chunk_end = NULL")
        db.conn().commit()

        idx = Indexer(db)
        embedded = idx.index(str(tmp_path))  # nothing changed → nothing should be re-embedded

    rows = db.conn().execute("SELECT chunk_end FROM chunk_hashes").fetchall()
    assert rows and all(r[0] is not None for r in rows), "every span must be backfilled"
    assert embedded == 0, "backfill must not re-embed unchanged content"
    assert idx._backfilled == len(rows)


def test_indexer_and_searcher_agree_on_the_hash(tmp_path):
    """Both sides must compute the chunk hash identically — drift makes every hit look stale (or
    none), which is why the rule lives in one place."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))

    lines = (tmp_path / "app.py").read_text().splitlines(keepends=True)
    for start, end, stored in db.conn().execute(
        "SELECT chunk_start, chunk_end, content_hash FROM chunk_hashes"
    ).fetchall():
        assert chunk_content_hash("".join(lines[start:end])) == stored, (
            f"re-hashing the stored span [{start},{end}) must reproduce the indexed hash"
        )


def test_verification_failure_degrades_to_unverified_not_empty(tmp_path):
    """Never-raise: if verification itself faults, serving unverified results beats serving none."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        with patch.object(Searcher, "_verify", side_effect=RuntimeError("boom")):
            res = s.search("charge", str(tmp_path), cosine_floor=-1.0)

    assert res, "a verification fault must not empty the result set"
    assert math.isclose(res[0]["score"], 1.0, abs_tol=1e-6)


# ------------------------------------------------------- the upgrade path, which is not an edge case

def test_an_index_that_cannot_be_verified_is_reported_as_unconfirmed(tmp_path):
    """The state EVERY existing cache is in on the first query after upgrading.

    `chunk_end` arrives by ALTER and is NULL until a pass backfills it, so verification silently
    does nothing for those rows. Reported as `complete`, that is the original bug back with no
    signal — and the enclosing-symbol preview makes it read as MORE authoritative, rendering a
    deleted `charge_credit_card` as `app.py:1 | charge_credit_card() … import logging`.

    The hits are KEPT rather than dropped: withholding everything would leave an upgrading user
    with no results at all until they happen to re-index. Kept, and marked unconfirmed."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding), \
            patch("codeintel.semantic_db._base_dir", lambda: tmp_path / "cache"):
        p = SemanticProvider()
        p.build_result("search", "charge", [], 2000, str(tmp_path))

        db = SemanticDb(default_db_path(None))
        db.conn().execute("UPDATE chunk_hashes SET chunk_end = NULL")  # post-ALTER, pre-backfill
        db.conn().commit()
        db.close()

        (tmp_path / "app.py").write_text(_EDITED)
        res = p.build_result("search", "charge", [], 2000, str(tmp_path))

    assert res["result"], "an upgrading user must still get results, not silence"
    assert res["confidence"] == "partial", "an unverifiable index is not a clean one"
    freshness = [g for g in res["gaps"] if g["kind"] == "unverified-chunks"]
    assert freshness, "the inability to verify must be named, not swallowed"
    assert "codeintel index" in freshness[0]["detail"], "and it must say how to fix it"


def test_a_verified_index_is_not_labelled_unconfirmed(tmp_path):
    """The flip side: once spans exist, a clean answer must still read as clean."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding), \
            patch("codeintel.semantic_db._base_dir", lambda: tmp_path / "cache"):
        p = SemanticProvider()
        p.build_result("search", "charge", [], 2000, str(tmp_path))
        res = p.build_result("search", "charge", [], 2000, str(tmp_path))

    assert res["confidence"] == "complete"
    assert not [g for g in (res.get("gaps") or []) if g["section"] == "freshness"]


def test_search_stats_describe_this_search_and_not_the_last_one(tmp_path):
    """`Searcher` caches its embedder on the instance so it CAN be reused; the per-search counters
    have to be reset on every path out, including the ones that return before verification."""
    (tmp_path / "app.py").write_text(_ORIGINAL)
    with patch("fastembed.TextEmbedding", _FlatEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        s.last_stale, s.last_unverifiable = 7, 9  # as if a previous query had found them

        assert s.search("", str(tmp_path)) == []          # blank query: returns before _verify
        assert (s.last_stale, s.last_unverifiable) == (0, 0)

        s.last_stale, s.last_unverifiable = 7, 9
        assert s.search("charge", "/nonexistent-project") == []   # unindexed: also early
        assert (s.last_stale, s.last_unverifiable) == (0, 0)
