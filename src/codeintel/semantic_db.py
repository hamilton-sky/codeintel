from __future__ import annotations

import pathlib
import sqlite3

import sqlite_vec

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

            CREATE INDEX IF NOT EXISTS idx_chunk_project
                ON chunk_hashes(project_root);
        """)
        c.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
