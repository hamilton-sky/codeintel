from __future__ import annotations

import logging
import pathlib
import sqlite3

import sqlite_vec

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def default_db_path() -> str:
    """The single, per-machine semantic index cache. Every entry point (the
    SemanticProvider, the Reindexer, and the CLI) MUST resolve to this one path — rows
    are partitioned by ``project_root`` inside it — so ``index`` and ``search`` can never
    diverge onto different files for the same repo.
    """
    return str(pathlib.Path.home() / ".codeintel" / "semantic.db")


class SemanticDb:
    """DB layer: opens a SQLite connection, loads sqlite-vec, and owns schema creation."""

    dimension: int = 384

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.enable_load_extension(True)
            self._conn.row_factory = sqlite3.Row
            # Concurrency: the background Reindexer writes on a daemon thread while a foreground
            # query indexes inline — two separate connections to this one file. With the SQLite
            # default (busy_timeout=0) the loser of that write race gets an immediate
            # "database is locked" and silently drops its work; a busy timeout makes it wait
            # instead, and WAL lets a search read while a reindex writes. (reset.py already
            # cleans up the -wal/-shm siblings WAL creates.) Never-raise: if the pragmas can't
            # be applied, fall back to default locking rather than fail to open the db.
            try:
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
        return self._conn

    def init(self) -> None:
        c = self.conn()
        try:
            sqlite_vec.load(c)
        except Exception as exc:
            raise RuntimeError(f"sqlite-vec extension failed to load: {exc}") from exc

        # Migration: caches created before the project_root partition column lack it. The
        # index is a regenerable cache, so on a schema mismatch we drop and rebuild rather
        # than ALTER — the next index pass repopulates it.
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(chunk_hashes)").fetchall()]
            if cols and "project_root" not in cols:
                c.executescript(
                    "DROP TABLE IF EXISTS code_embeddings;"
                    "DROP TABLE IF EXISTS chunk_hashes;"
                )
        except Exception:
            pass

        c.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS code_embeddings USING vec0(
                chunk_id TEXT PRIMARY KEY,
                embedding FLOAT[{self.dimension}]
            );

            CREATE TABLE IF NOT EXISTS chunk_hashes (
                chunk_id     TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                chunk_start  INT  NOT NULL,
                content_hash TEXT NOT NULL
            );

            -- Composite (project_root, file_path): serves the project-scoped scans
            -- (_cleanup_deleted, row-count, search KNN) via the leftmost prefix AND the
            -- per-file orphan reconcile / cleanup lookups, which would otherwise scan every
            -- row of the project once per file (O(files^2) on a large repo). Supersedes the
            -- old single-column idx_chunk_project, dropped here so migrated caches stay tidy.
            CREATE INDEX IF NOT EXISTS idx_chunk_project_file
                ON chunk_hashes(project_root, file_path);

            DROP INDEX IF EXISTS idx_chunk_project;
        """)
        c.commit()

    def delete_file_orphans(
        self, project_root: str, file_path: str, keep_ids: set[str]
    ) -> int:
        """Drop rows for one file whose ``chunk_id`` the file no longer produces.

        Syntax-aware chunking (and any edit that moves/removes a def) shifts chunk
        boundaries, so a re-index leaves stale rows behind — ``_cleanup_deleted`` only
        prunes whole *deleted files*, never a def that vanished from a file that still
        exists. Reconcile per file: everything indexed under (project_root, file_path)
        that isn't in ``keep_ids`` is an orphan and is removed from BOTH tables.

        Scoped by project_root AND file_path so it can only ever touch this one file's
        rows in this one project. Never raises — a reconcile failure logs and returns 0
        (the stale rows simply persist until the next successful pass; the cache is
        regenerable). Computes the delete set in Python rather than a ``NOT IN (...)``
        clause so a large ``keep_ids`` can't trip SQLite's bound-parameter limit.
        """
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT chunk_id FROM chunk_hashes"
                " WHERE project_root = ? AND file_path = ?",
                (project_root, file_path),
            ).fetchall()
            orphans = [r[0] for r in rows if r[0] not in keep_ids]
            for cid in orphans:
                conn.execute("DELETE FROM code_embeddings WHERE chunk_id = ?", (cid,))
                conn.execute("DELETE FROM chunk_hashes WHERE chunk_id = ?", (cid,))
            if orphans:
                conn.commit()
            return len(orphans)
        except Exception as exc:
            logger.warning("orphan reconcile failed for %s: %s", file_path, exc)
            return 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
