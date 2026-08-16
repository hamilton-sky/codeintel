"""Hybrid reranking (0.7.0, Phase 2 of docs/roadmap-semantic.md).

Cosine alone under-ranks exact lexical/symbol matches; a lexical + RRF fusion rerank fixes the
ordering with no model dependency. These tests use a content-addressed fake embedder that places
each fixture chunk at a known angle, so the *cosine* order is controlled and we can prove the
rerank flips a literal-symbol match above a closer-but-lexically-distant one — and that
``rerank="off"`` restores the pure-cosine order.
"""
from __future__ import annotations

import math
from unittest.mock import patch

import numpy as np

from codeintel.indexer import Indexer
from codeintel.searcher import _SYMBOL_BOOST, Searcher, _tokenize
from codeintel.semantic_db import SemanticDb


def _angle_vec(theta_deg: float) -> np.ndarray:
    v = np.zeros(384, dtype=np.float32)
    r = math.radians(theta_deg)
    v[0], v[1] = math.cos(r), math.sin(r)
    return v


class _AngleEmbedding:
    """Deterministic, content-addressed: each fixture chunk / the query is placed at a fixed angle,
    so cosine similarity between any two is a known cos(Δθ). Lets a test dictate the cosine order."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        out = []
        for t in texts:
            if "def parse_config" in t:
                out.append(_angle_vec(60))   # literal-symbol chunk — FARTHER from the query
            elif "def load_settings" in t:
                out.append(_angle_vec(20))    # semantic chunk — NEARER the query
            else:
                out.append(_angle_vec(0))     # the query (and anything else) at the origin
        return out


def _mem_db() -> SemanticDb:
    db = SemanticDb(":memory:")
    db.init()
    return db


_FIXTURE = (
    "def parse_config(path):\n"                                       # line 0  (literal symbol)
    "    return open(path).read()\n"                                  # line 1
    "\n"                                                              # line 2
    "def load_settings():\n"                                         # line 3  (semantic match)
    '    """Read and parse configuration values from disk."""\n'      # line 4
    "    return {}\n"                                                 # line 5
)


# --------------------------------------------------------------------------- core acceptance

def test_exact_symbol_ranks_literal_match_above_semantic(tmp_path):
    (tmp_path / "mod.py").write_text(_FIXTURE)
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        on = s.search("parse_config", str(tmp_path), rerank="on")
        off = s.search("parse_config", str(tmp_path), rerank="off")

    # pure cosine puts the nearer (semantic) chunk first; rerank pulls the literal symbol up
    assert next(r["line"] for r in off) == 3, "cosine order should lead with the semantic chunk"
    assert next(r["line"] for r in on) == 0, "rerank should lead with the literal parse_config"
    assert {r["line"] for r in on} == {0, 3}, "rerank reorders, it does not drop candidates"


def test_rerank_off_matches_pure_cosine(tmp_path):
    (tmp_path / "mod.py").write_text(_FIXTURE)
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        off = s.search("parse_config", str(tmp_path), rerank="off")
    # cosine order: load_settings (20°, score .94) then parse_config (60°, score .5), scores intact
    assert [(r["line"], r["score"]) for r in off] == [
        (3, round(1 - (1 - math.cos(math.radians(20))), 6)),
        (0, round(1 - (1 - math.cos(math.radians(60))), 6)),
    ]


def test_no_lexical_signal_preserves_cosine_order(tmp_path):
    # a query with zero token overlap must leave the cosine order untouched (rerank reorders only
    # on real lexical signal — otherwise the lexical rank mirrors the semantic rank)
    (tmp_path / "mod.py").write_text(_FIXTURE)
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        on = s.search("zzz_totally_unrelated_qqq", str(tmp_path), rerank="on")
        off = s.search("zzz_totally_unrelated_qqq", str(tmp_path), rerank="off")
    assert [r["line"] for r in on] == [r["line"] for r in off]


# --------------------------------------------------------------------------- never-raise / bounds

def test_rerank_never_raises_on_missing_file(tmp_path):
    (tmp_path / "gone.py").write_text("def target():\n    return 1\n")
    (tmp_path / "keep.py").write_text("def other():\n    return 2\n")
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        (tmp_path / "gone.py").unlink()  # deleted AFTER indexing → DB row survives, file is gone
        s = Searcher(db)
        res = s.search("target", str(tmp_path), rerank="on", cosine_floor=-1.0)

    assert isinstance(res, list) and res  # no crash; still returns
    gone = [r for r in res if r["path"] == "gone.py"]
    assert gone and gone[0]["snippet"] == "[file not found]"  # missing file → sentinel, scored 0


def test_rerank_does_not_bleed_boost_into_neighbouring_def(tmp_path):
    # load_settings (nearer cosine) sits right above parse_config; a naive 40-line read from
    # load_settings' start bleeds into `def parse_config` and would steal its symbol boost. The
    # next-stored-chunk bound must confine each candidate to its own span, so the chunk that
    # actually DEFINES the queried symbol wins despite its worse cosine.
    (tmp_path / "mod.py").write_text(
        "def load_settings():\n"           # 0  near cosine, does NOT define the query symbol
        "    return 1\n"                   # 1
        "\n"                               # 2
        "def parse_config(path):\n"        # 3  far cosine, but DEFINES parse_config
        "    return open(path).read()\n"   # 4
    )
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        res = Searcher(db).search("parse_config", str(tmp_path), rerank="on")
    assert next(r["line"] for r in res) == 3, "the chunk that actually defines the symbol must win"


def test_search_survives_bad_param_types(tmp_path):
    (tmp_path / "mod.py").write_text(_FIXTURE)
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        # non-int k / rerank_candidates must degrade to defaults, never raise (public API)
        res = Searcher(db).search("parse_config", str(tmp_path), k="oops", rerank_candidates="lots")
    assert isinstance(res, list) and {r["line"] for r in res} == {0, 3}


def test_rerank_falsey_values_treated_as_off(tmp_path):
    (tmp_path / "mod.py").write_text(_FIXTURE)
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        off = s.search("parse_config", str(tmp_path), rerank="off")
        false_bool = s.search("parse_config", str(tmp_path), rerank=False)
    assert [r["line"] for r in false_bool] == [r["line"] for r in off] == [3, 0]


def test_candidate_limit_is_capped(tmp_path, monkeypatch):
    import codeintel.searcher as sm
    for i in range(6):
        (tmp_path / f"f{i}.py").write_text(f"def fn{i}():\n    return {i}\n")
    monkeypatch.setattr(sm, "_RERANK_CANDIDATES_CAP", 2)  # tiny cap for the test
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        calls = {"n": 0}
        orig = s._read_chunk

        def counting(fp, cs):
            calls["n"] += 1
            return orig(fp, cs)

        monkeypatch.setattr(s, "_read_chunk", counting)
        s.search("fn1", str(tmp_path), k=1, rerank="on", rerank_candidates=1000)
    assert calls["n"] <= 2, "candidate set must be clamped to the cap regardless of config"


def test_large_k_is_not_shrunk_by_cap(tmp_path, monkeypatch):
    # the cap bounds only the extra rerank breadth (rerank_candidates); a large k must still be
    # honored, so rerank-on never returns fewer results than pure cosine would for that k
    import codeintel.searcher as sm
    for i in range(6):
        (tmp_path / f"f{i}.py").write_text(f"def fn{i}():\n    return {i}\n")
    monkeypatch.setattr(sm, "_RERANK_CANDIDATES_CAP", 2)  # tiny cap, but k below must still win
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        res = Searcher(db).search(
            "fn1", str(tmp_path), k=5, rerank="on", rerank_candidates=1, cosine_floor=-1.0
        )
    assert len(res) == 5, "k must be honored despite a smaller cap/rerank_candidates"


def test_rerank_reads_are_bounded(tmp_path, monkeypatch):
    for i in range(40):
        (tmp_path / f"f{i}.py").write_text(f"def fn{i}():\n    return {i}\n")
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        s = Searcher(db)
        calls = {"n": 0}
        orig = s._read_chunk

        def counting(fp, cs):
            calls["n"] += 1
            return orig(fp, cs)

        monkeypatch.setattr(s, "_read_chunk", counting)
        s.search("fn1", str(tmp_path), k=3, rerank="on", rerank_candidates=5)

    assert calls["n"] <= 5, "rerank must read at most rerank_candidates chunks, not the whole repo"


# --------------------------------------------------------------------------- scoring units

def test_tokenize_splits_camel_and_snake():
    t = _tokenize("parseConfig load_settings HTTPServer")
    assert {"parseconfig", "parse", "config",
            "load_settings", "load", "settings",
            "httpserver", "http", "server"} <= t


def test_lexical_score_is_query_term_fraction():
    q = _tokenize("parse_config")  # {parse_config, parse, config}
    assert Searcher._lexical_score(q, "def parse_config(): pass") == 1.0
    assert Searcher._lexical_score(q, "def unrelated(): pass") == 0.0
    assert Searcher._lexical_score(q, "please parse the text") == 1 / 3  # only 'parse' overlaps
    assert Searcher._lexical_score(set(), "anything") == 0.0


def test_symbol_boost_prefers_def_name():
    assert Searcher._symbol_boost("parse_config", "def parse_config(x): ...") == _SYMBOL_BOOST
    assert Searcher._symbol_boost("parse_config", "y = parse_config()") == _SYMBOL_BOOST * 0.5
    assert Searcher._symbol_boost("parse_config", "unrelated text") == 0.0
    assert Searcher._symbol_boost("two words", "def two(): ...") == 0.0     # multi-word → no boost
    assert Searcher._symbol_boost("", "def f(): ...") == 0.0
    # case-insensitive, to match the lexical score's lowercasing
    assert Searcher._symbol_boost("Parse_Config", "def parse_config(x): ...") == _SYMBOL_BOOST


def test_rerank_default_is_on(tmp_path):
    # the method default must match the config default so a direct caller gets reranking too
    (tmp_path / "mod.py").write_text(_FIXTURE)
    with patch("fastembed.TextEmbedding", _AngleEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        default = Searcher(db).search("parse_config", str(tmp_path))          # no rerank kwarg
        explicit = Searcher(db).search("parse_config", str(tmp_path), rerank="on")
    assert [r["line"] for r in default] == [r["line"] for r in explicit] == [0, 3]
