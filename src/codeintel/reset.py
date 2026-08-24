"""Recovery command — drop the semantic index cache for one project, or nuke the whole
per-machine db. This is the escape hatch for a corrupt index, so it must work even when the
db file itself can't be opened: never raises, never prompts. The CLI owns confirmation; this
module is pure (dry-run by default via ``apply=False``).
"""
from __future__ import annotations

import glob
import logging
import os
import re
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


def _graph_cache_dir() -> str:
    """Where codebase-memory-mcp keeps one sqlite file per indexed project.

    codeintel does not own this directory, which is exactly why `reset --all` never touched it —
    and why "start from a clean index" was not true for the graph engine. Honouring the backend's
    own override so a relocated cache is still cleared."""
    override = os.environ.get("CODEBASE_MEMORY_HOME") or os.environ.get("CODEBASE_MEMORY_CACHE_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".cache", "codebase-memory-mcp")


def _reset_graph_cache(apply: bool) -> dict:
    """Remove every per-project graph index (and the `.corrupt` files left behind by torn writes).

    Deliberately leaves `_config.db` alone: that is registration/config, not an index, and removing
    it is a different and more destructive operation than "clear the indexes"."""
    directory = _graph_cache_dir()
    count = 0
    corrupt = 0
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return {"count": 0, "corrupt": 0}
    except Exception as exc:
        return {"count": 0, "corrupt": 0, "unreachable": type(exc).__name__}

    for name in names:
        if name == "_config.db" or name.startswith("_config.db-"):
            continue
        if ".db" not in name:
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".corrupt"):
            corrupt += 1
        if apply:
            try:
                os.remove(path)
                count += 1
            except Exception:
                pass
        else:
            count += 1
    return {"count": count, "corrupt": corrupt}


def _graph_slug(canonical_root: str) -> str:
    """The stem the graph backend gives a project's cache file: every run of non-alphanumeric
    characters collapses to a single ``-``, with the ends trimmed. So ``/Users/me/aws-resource-tools``
    → ``Users-me-aws-resource-tools`` and ``/tmp/-Weird/repo`` → ``tmp-Weird-repo`` (NOT
    ``tmp--Weird-repo`` — a naive ``/``→``-`` replace leaves a double dash and misses the file,
    silently leaking the graph index a scoped reset was meant to remove). Verified against real
    on-disk caches. The collapse also flattens away every path separator, so the target can never
    escape the cache directory."""
    return re.sub(r"[^A-Za-z0-9]+", "-", canonical_root).strip("-")


def _reset_graph_project(project_root: str, apply: bool) -> dict:
    """Remove ONE project's graph index — the per-project sqlite the backend names ``<slug>.db`` (see
    :func:`_graph_slug`), plus its ``-wal``/``-shm``/``.corrupt`` siblings.

    This is the graph half of a *scoped* reset. Without it, ``reset <repo>`` cleared only the
    semantic side and left the repo listed and queryable in the graph engine — so "start as if never
    indexed" was true only for ``--all``. ``list_projects`` enumerates these files (``_config.db`` is
    key/value config, not a registry), so deleting them is exactly what makes one project vanish.
    Pure file removal like ``_reset_graph_cache``: it works with the backend down and never hangs."""
    directory = _graph_cache_dir()
    slug = _graph_slug(os.path.realpath(str(project_root)))
    found = 0
    removed = 0
    for suffix in (".db", ".db-wal", ".db-shm", ".db.corrupt"):
        path = os.path.join(directory, slug + suffix)
        if not os.path.isfile(path):
            continue
        found += 1
        if apply:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
    return {"slug": slug, "found": found, "removed": removed}


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
            graph = _reset_graph_cache(apply)
            verb = "removed" if apply else "would remove"
            detail = f"{verb} {count} semantic index file(s) across all models"
            # `--all` used to mean "all SEMANTIC models", which is not what it says and not what a
            # caller told to "start from a clean index" would assume. It left every graph project
            # in place — including, on the evaluated machine, an index of the PARENT directory of
            # every repository, which is what a later query silently fell back to. Say what was
            # actually cleared, and clear the graph too.
            detail += f"; {verb} {graph['count']} graph project file(s)"
            if graph.get("corrupt"):
                detail += f" (including {graph['corrupt']} previously-corrupt file(s))"
            if graph.get("unreachable"):
                detail += (f"\nnote: the graph cache directory could not be read "
                           f"({graph['unreachable']}) — graph projects were NOT cleared")
            return {"ok": True, "mode": "all", "target": "ALL", "count": count + graph["count"],
                    "applied": bool(apply), "detail": detail}
        real = os.path.realpath(str(project_root))
        results = [_reset_scoped(project_root, p, apply) for p in files]
        count = sum(r["count"] for r in results)
        # The graph half of a scoped reset — the piece that was missing, so `reset <repo>` now clears
        # BOTH engines and the repo is genuinely "never indexed", matching what `--all` does globally.
        graph = _reset_graph_project(project_root, apply)
        verb = "removed" if apply else "found"
        # Surface per-file trouble instead of summing counts and synthesizing a success line —
        # discarding these is what made a failed reset indistinguishable from a clean one.
        problems = [r["detail"] for r in results if r.get("error") or r.get("recovered") is not None]
        detail = f"{verb} {count} indexed chunk(s)"
        if graph["found"]:
            detail += " and the graph index" if apply else " and a graph index"
        detail += " for this project"
        if problems:
            detail = "\n".join([*problems, detail])
        return {"ok": True, "mode": "scoped", "target": real, "count": count, "applied": bool(apply),
                "detail": detail,
                "graph_found": graph["found"], "graph_removed": graph["removed"],
                "failed": any(r.get("error") for r in results)}
    except Exception as exc:
        return {"ok": True, "applied": apply, "detail": f"reset-error: {type(exc).__name__}: {exc}"}
