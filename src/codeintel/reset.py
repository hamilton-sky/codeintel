"""Recovery command — drop the semantic index cache for one project, or nuke the whole
per-machine db. This is the escape hatch for a corrupt index, so it must work even when the
db file itself can't be opened: never raises, never prompts. The CLI owns confirmation; this
module is pure (dry-run by default via ``apply=False``).
"""
from __future__ import annotations

import glob
import logging
import os
import sqlite3

import sqlite_vec

from codeintel.semantic_db import default_db_path

_logger = logging.getLogger("codeintel")


def _cache_files() -> list[str]:
    """Every per-model cache file (``semantic.db`` + ``semantic-<hash>.db``). Reset must sweep all
    of them: a repo's rows can live in any model's file, and a model switch leaves orphans behind."""
    base = os.path.dirname(default_db_path())
    try:
        return sorted(glob.glob(os.path.join(base, "semantic*.db")))
    except Exception:
        return []


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
            try:
                conn.execute(
                    "DELETE FROM code_embeddings WHERE chunk_id IN "
                    "(SELECT chunk_id FROM chunk_hashes WHERE project_root=?)", (real,))
            except Exception:
                pass  # code_embeddings is created lazily at first embed — may not exist yet
            conn.execute("DELETE FROM chunk_hashes WHERE project_root=?", (real,))
            conn.commit()

        verb = "removed" if apply else "found"
        return {"ok": True, "mode": "scoped", "target": real, "count": count,
                "applied": bool(apply),
                "detail": f"{verb} {count} indexed chunk(s) for this project"}
    except Exception as exc:
        # `reset` exists to "recover from a corrupt or stale DB", and this is precisely the
        # corrupt case — sqlite refuses to open the file at all, so there are no rows to DELETE.
        # Reporting "removed 0 chunks" left the user in a loop: doctor diagnoses the corruption
        # and prescribes reset, reset no-ops and claims success, doctor repeats forever. When the
        # cache is unreadable the only repair is to remove the file; it is a rebuildable cache,
        # not user data.
        if apply and _looks_unreadable(exc):
            removed = _discard_cache_file(path)
            outcome = ("removed it; run `codeintel index` to rebuild" if removed
                       else "could not remove it — delete it by hand")
            return {"ok": True, "mode": "scoped", "target": real, "count": 0,
                    "applied": True, "recovered": removed,
                    "detail": f"cache was unreadable ({type(exc).__name__}) — {outcome}: {path}"}
        return {"ok": True, "mode": "scoped", "target": real, "count": 0,
                "applied": bool(apply), "error": f"{type(exc).__name__}: {exc}",
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


def _looks_unreadable(exc: Exception) -> bool:
    """Whether *exc* means "this file is not a usable sqlite cache" (as opposed to e.g. a lock)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(s in text for s in ("not a database", "file is encrypted", "malformed",
                                   "databaseerror", "disk image"))


def _discard_cache_file(db_path: str) -> bool:
    """Delete an unreadable cache file (and its WAL/SHM siblings). Rebuildable by design."""
    ok = False
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
            ok = ok or suffix == ""
        except FileNotFoundError:
            continue
        except OSError as exc:
            _logger.warning("reset: could not remove %s%s: %s", db_path, suffix, exc)
    return ok


def run_reset(
    project_root: str,
    *,
    all_projects: bool = False,
    apply: bool = False,
    db_path: str | None = None,
) -> dict:
    """Drop indexed rows for ``project_root`` (or, with ``all_projects``, remove the whole cache —
    every per-model db file plus their -wal/-shm siblings). ``apply=False`` is a dry-run: count
    only, delete nothing. Never raises.

    An explicit ``db_path`` operates on exactly that one file (the test seam / a targeted reset);
    otherwise reset sweeps EVERY per-model cache file, so a repo's rows are cleared no matter which
    model's file they landed in, and model-switch orphans are reclaimed."""
    try:
        if db_path is not None:
            return _reset_all(db_path, apply) if all_projects else _reset_scoped(project_root, db_path, apply)

        files = _cache_files() or [default_db_path()]
        if all_projects:
            count = sum(_reset_all(p, apply)["count"] for p in files)
            verb = "removed" if apply else "would remove"
            return {"ok": True, "mode": "all", "target": "ALL", "count": count,
                    "applied": bool(apply), "detail": f"{verb} {count} index file(s) across all models"}
        real = os.path.realpath(str(project_root))
        results = [_reset_scoped(project_root, p, apply) for p in files]
        count = sum(r["count"] for r in results)
        verb = "removed" if apply else "found"
        # Surface per-file trouble instead of summing counts and synthesizing a success line —
        # discarding these is what made a failed reset indistinguishable from a clean one.
        problems = [r["detail"] for r in results if r.get("error") or r.get("recovered") is not None]
        detail = f"{verb} {count} indexed chunk(s) for this project"
        if problems:
            detail = "\n".join([*problems, detail])
        return {"ok": True, "mode": "scoped", "target": real, "count": count, "applied": bool(apply),
                "detail": detail,
                "failed": any(r.get("error") for r in results)}
    except Exception as exc:
        return {"ok": True, "applied": apply, "detail": f"reset-error: {type(exc).__name__}: {exc}"}
