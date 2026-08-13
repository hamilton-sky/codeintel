"""Embedding-model / vector-dimension handling (0.8.3).

Different embedding models produce incompatible vectors (often different lengths), and a
sqlite-vec vec0 table is single-dimension. The design isolates them PHYSICALLY: each model gets
its own cache file (``semantic-<hash>.db``), and the vec0 table self-dimensions from the first
real vector. Test #1 is the regression that caught the reverted global-wipe fix.
"""
from __future__ import annotations

import glob
import os
from unittest.mock import patch

import numpy as np

from codeintel.indexer import Indexer
from codeintel.reset import run_reset
from codeintel.searcher import Searcher
from codeintel.semantic_db import DEFAULT_MODEL, SemanticDb, default_db_path


def _fake(dim):
    class _F:
        def __init__(self, model_name=None):
            pass

        def embed(self, texts):
            return [np.full(dim, 0.1, dtype=np.float32) for _ in list(texts)]
    return _F


def _seed(path, project_root_real, cid="c1"):
    db = SemanticDb(path)
    db.init()
    db.conn().execute(
        "INSERT INTO chunk_hashes(chunk_id, project_root, file_path, chunk_start, content_hash)"
        " VALUES (?,?,?,?,?)", (cid, project_root_real, "f.py", 0, "h"))
    db.conn().commit()
    db.close()


# --------------------------------------------------------------------------- #1 THE regression

def test_different_model_repos_do_not_wipe_each_other(tmp_path, monkeypatch):
    # The reverted fix wiped the whole shared cache on a model mismatch; with two repos on
    # different models that meant they perpetually destroyed each other. Per-model files make it
    # physically impossible.
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    (tmp_path / "a").mkdir(); (tmp_path / "a" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b").mkdir(); (tmp_path / "b" / "b.py").write_text("def b():\n    return 2\n")

    def index(repo, model, dim):
        db = SemanticDb(default_db_path(model)); db.init()
        with patch("fastembed.TextEmbedding", _fake(dim)):
            Indexer(db, model_name=model).index(str(tmp_path / repo))
        return db

    # ping-pong: A(default,384) → B(other,768) → A → B, asserting each survives the other
    for _ in range(2):
        db_a = index("a", DEFAULT_MODEL, 384)
        db_b = index("b", "other-model-768", 768)
        assert Searcher(db_a, model_name=DEFAULT_MODEL).has_index(str(tmp_path / "a")), \
            "repo A must survive repo B indexing under a different model"
        assert Searcher(db_b, model_name="other-model-768").has_index(str(tmp_path / "b"))

    # they live in DIFFERENT files
    assert default_db_path(DEFAULT_MODEL) != default_db_path("other-model-768")
    assert os.path.exists(tmp_path / "semantic.db")                    # A → default file
    assert len(glob.glob(str(tmp_path / "semantic-*.db"))) == 1        # B → its own file


# --------------------------------------------------------------------------- self-dimensioning

def test_table_self_dimensions_from_first_vector(tmp_path):
    db = SemanticDb(str(tmp_path / "y.db")); db.init()
    assert db._table_dim() is None                       # init does NOT create code_embeddings
    assert db.ensure_embeddings_table(768) == 768
    assert db._table_dim() == 768                         # created at the real vector size, not 384


def test_ensure_embeddings_table_skips_on_dimension_mismatch(tmp_path):
    # the only in-file mismatch case (a DEFAULT_MODEL size bump): skip the write, never wipe
    db = SemanticDb(str(tmp_path / "x.db")); db.init()
    assert db.ensure_embeddings_table(384) == 384
    assert db.ensure_embeddings_table(768) is None       # refuses
    assert db._table_dim() == 384                         # table left intact, not recreated/wiped


# --------------------------------------------------------------------------- path invariants

def test_default_db_path_invariants(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    d = default_db_path
    legacy = str(tmp_path / "semantic.db")
    assert d() == d(None) == d("") == d("  ") == d(DEFAULT_MODEL) == legacy   # default → legacy file
    assert d("other") != legacy and d("other").startswith(str(tmp_path / "semantic-"))
    assert d("a") != d("b") and d("a") == d("a")          # distinct models distinct, same stable
    assert d("wéird/model:v2") .endswith(".db")           # total: any string yields a filename


# --------------------------------------------------------------------------- reset across files

def test_scoped_reset_sweeps_all_model_files_but_spares_other_projects(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    real_a = os.path.realpath(str(tmp_path / "a"))
    real_b = os.path.realpath(str(tmp_path / "b"))
    # project A has rows in TWO model files; project B has rows in the default file
    _seed(default_db_path(DEFAULT_MODEL), real_a, "a-default")
    _seed(default_db_path("other"), real_a, "a-other")
    db_b = SemanticDb(default_db_path(DEFAULT_MODEL))   # same default file as A's default rows
    db_b.conn().execute(
        "INSERT INTO chunk_hashes(chunk_id, project_root, file_path, chunk_start, content_hash)"
        " VALUES (?,?,?,?,?)", ("b1", real_b, "g.py", 0, "h"))
    db_b.conn().commit(); db_b.close()

    res = run_reset(str(tmp_path / "a"), apply=True)      # no db_path → sweep every model file
    assert res["count"] == 2                              # A's rows removed from BOTH files

    surviving = SemanticDb(default_db_path(DEFAULT_MODEL)).conn().execute(
        "SELECT COUNT(*) FROM chunk_hashes WHERE project_root=?", (real_b,)).fetchone()[0]
    assert surviving == 1, "a scoped reset of A must leave project B untouched"


def test_probe_and_build_result_resolve_the_same_file(tmp_path, monkeypatch):
    # regression for the divergence the CHANGELOG says it closed (build_result used a module
    # _DB_PATH, probe used default_db_path()): with a NON-default model both must hit the same file,
    # or probe reports "not indexed" for a repo build_result just indexed.
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    from codeintel.providers.semantic import SemanticProvider
    repo = tmp_path / "r"; repo.mkdir()
    (repo / ".codeintel.toml").write_text('model = "custom-384-model"\n')
    (repo / "m.py").write_text("def f():\n    return 1\n")

    with patch("fastembed.TextEmbedding", _fake(384)):
        SemanticProvider().build_result("search", "f", [], 0, str(repo))   # writes the model's file
        assert os.path.exists(default_db_path("custom-384-model"))          # not the default file
        assert SemanticProvider().probe(str(repo))["repo_indexed"] is True  # probe found the SAME file


def test_reset_tolerates_absent_code_embeddings(tmp_path):
    # code_embeddings is created lazily, so a seed-only db has none — reset must not choke on it
    p = str(tmp_path / "x.db")
    real = os.path.realpath(str(tmp_path))
    _seed(p, real)
    assert SemanticDb(p)._table_dim() is None             # confirms no code_embeddings table
    res = run_reset(str(tmp_path), apply=True, db_path=p)
    assert res["count"] == 1 and "removed" in res["detail"]
