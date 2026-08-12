"""Reset command tests — real-boundary (actual temp SemanticDb files, no seam mocks), per
this repo's philosophy. Reset is a recovery command: it must never raise, even against a
corrupt db, since a corrupt db is exactly the scenario it exists to fix.
"""
from __future__ import annotations

import os
import sqlite3

from codeintel.reset import run_reset


def _seed(db_path, project_root_real, n=1):
    from codeintel.semantic_db import SemanticDb
    db = SemanticDb(str(db_path))
    db.init()
    c = db.conn()
    for i in range(n):
        c.execute(
            "INSERT INTO chunk_hashes(chunk_id,project_root,file_path,chunk_start,content_hash)"
            " VALUES (?,?,?,?,?)",
            (f"{project_root_real}:{i}", project_root_real, "f.py", i, "h"),
        )
    c.commit()
    db.close()


def _row_count(db_path, project_root_real):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM chunk_hashes WHERE project_root=?", (project_root_real,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


# --------------------------------------------------------------------------- #
# scoped reset
# --------------------------------------------------------------------------- #

def test_scoped_reset_deletes_only_target(tmp_path):
    db_path = tmp_path / "semantic.db"
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    real_a = os.path.realpath(str(repo_a))
    real_b = os.path.realpath(str(repo_b))
    _seed(db_path, real_a, n=2)
    _seed(db_path, real_b, n=3)

    r = run_reset(str(repo_a), apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert r["mode"] == "scoped"
    assert r["applied"] is True
    assert r["count"] == 2
    assert _row_count(db_path, real_a) == 0
    assert _row_count(db_path, real_b) == 3


def test_dry_run_counts_without_deleting(tmp_path):
    db_path = tmp_path / "semantic.db"
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    real_a = os.path.realpath(str(repo_a))
    _seed(db_path, real_a, n=4)

    r = run_reset(str(repo_a), apply=False, db_path=str(db_path))

    assert r["ok"] is True
    assert r["count"] > 0
    assert r["applied"] is False
    assert _row_count(db_path, real_a) == 4


def test_scoped_reset_on_corrupt_db_never_raises(tmp_path):
    db_path = tmp_path / "semantic.db"
    db_path.write_bytes(b"this is not a sqlite database")
    repo = tmp_path / "repo"
    repo.mkdir()

    r = run_reset(str(repo), apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert "error" in r["detail"].lower()


# --------------------------------------------------------------------------- #
# --all reset
# --------------------------------------------------------------------------- #

def test_reset_all_removes_db_files(tmp_path):
    db_path = tmp_path / "semantic.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(db_path, os.path.realpath(str(repo)))
    wal_path = tmp_path / "semantic.db-wal"
    shm_path = tmp_path / "semantic.db-shm"
    wal_path.write_bytes(b"")
    shm_path.write_bytes(b"")

    r = run_reset(str(repo), all_projects=True, apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert r["mode"] == "all"
    assert r["applied"] is True
    assert r["count"] == 3
    assert not db_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_reset_all_on_corrupt_db(tmp_path):
    db_path = tmp_path / "semantic.db"
    db_path.write_bytes(b"\x00\x01garbage-not-a-db\xff")

    r = run_reset("/any/project", all_projects=True, apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert not db_path.exists()


# --------------------------------------------------------------------------- #
# missing db — idempotent
# --------------------------------------------------------------------------- #

def test_reset_missing_db_idempotent(tmp_path):
    db_path = tmp_path / "does-not-exist.db"

    scoped = run_reset("/any/project", apply=True, db_path=str(db_path))
    assert scoped["ok"] is True
    assert scoped["count"] == 0

    all_reset = run_reset("/any/project", all_projects=True, apply=True, db_path=str(db_path))
    assert all_reset["ok"] is True
    assert all_reset["count"] == 0
