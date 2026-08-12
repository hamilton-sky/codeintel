"""Recovery command — drop the semantic index cache for one project, or nuke the whole
per-machine db. This is the escape hatch for a corrupt index, so it must work even when the
db file itself can't be opened: never raises, never prompts. The CLI owns confirmation; this
module is pure (dry-run by default via ``apply=False``).
"""
from __future__ import annotations

import os
import sqlite3

import sqlite_vec

from codeintel.semantic_db import default_db_path


def _reset_scoped(project_root: str, path: str, apply: bool) -> dict:
    real = os.path.realpath(str(project_root))
    if not os.path.exists(path):
        return {"ok": True, "mode": "scoped", "target": real, "count": 0,
                "applied": bool(apply), "detail": "no index db found — nothing to reset"}

    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA busy_timeout=2000")
        try:
            # Only needed to delete from the vec0 virtual table below — the count query
            # against the plain chunk_hashes table works fine without it.
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except Exception:
            pass

        row = conn.execute(
            "SELECT COUNT(*) FROM chunk_hashes WHERE project_root=?", (real,)
        ).fetchone()
        count = int(row[0]) if row else 0

        if apply:
            conn.execute(
                "DELETE FROM code_embeddings WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunk_hashes WHERE project_root=?)", (real,))
            conn.execute("DELETE FROM chunk_hashes WHERE project_root=?", (real,))
            conn.commit()

        verb = "removed" if apply else "found"
        return {"ok": True, "mode": "scoped", "target": real, "count": count,
                "applied": bool(apply),
                "detail": f"{verb} {count} indexed chunk(s) for this project"}
    except Exception as exc:
        return {"ok": True, "mode": "scoped", "target": real, "count": 0,
                "applied": bool(apply),
                "detail": f"reset-error: db unreadable/locked ({type(exc).__name__}: {exc})"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _reset_all(path: str, apply: bool) -> dict:
    candidates = [path, path + "-wal", path + "-shm"]
    if apply:
        removed = 0
        for p in candidates:
            try:
                os.remove(p)
                removed += 1
            except FileNotFoundError:
                pass
            except Exception:
                pass
        count = removed
        detail = f"removed {removed} index file(s)"
    else:
        count = sum(1 for p in candidates if os.path.exists(p))
        detail = f"{count} index file(s) would be removed"

    return {"ok": True, "mode": "all", "target": "ALL", "count": count,
            "applied": bool(apply), "detail": detail}


def run_reset(
    project_root: str,
    *,
    all_projects: bool = False,
    apply: bool = False,
    db_path: str | None = None,
) -> dict:
    """Drop indexed rows for ``project_root`` (or, with ``all_projects``, remove the whole
    cache db file plus its -wal/-shm siblings). ``apply=False`` is a dry-run: count only,
    delete nothing. Never raises."""
    try:
        path = db_path if db_path is not None else default_db_path()
        if all_projects:
            return _reset_all(path, apply)
        return _reset_scoped(project_root, path, apply)
    except Exception as exc:
        return {"ok": True, "applied": apply, "detail": f"reset-error: {type(exc).__name__}: {exc}"}
