from __future__ import annotations

import sqlite3

import sqlite_vec

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


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

        c.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS code_embeddings USING vec0(
                chunk_id TEXT PRIMARY KEY,
                embedding FLOAT[{self.dimension}]
            );

            CREATE TABLE IF NOT EXISTS chunk_hashes (
                chunk_id     TEXT PRIMARY KEY,
                file_path    TEXT NOT NULL,
                chunk_start  INT  NOT NULL,
                content_hash TEXT NOT NULL
            );
        """)
        c.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
