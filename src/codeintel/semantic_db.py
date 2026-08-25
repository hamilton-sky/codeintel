from __future__ import annotations

import hashlib
import logging
import pathlib
import re
import sqlite3
import time

import sqlite_vec

from codeintel.paths import codeintel_home

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# Cap on the characters a single chunk contributes. `_maybe_split` splits on line boundaries, so a
# minified bundle or generated one-liner is one unsplittable chunk however large: a 20MB one-line
# .py peaked at 3.4GB RSS through the embedder, on the reindexer's daemon thread inside the
# long-lived MCP server. The head of a chunk carries its identifying content anyway.
MAX_CHUNK_CHARS = 200_000


def chunk_content_hash(text: str) -> str:
    """The content hash identifying one chunk's text.

    Lives here, beside the schema, because BOTH the indexer (writing ``chunk_hashes.content_hash``)
    and the searcher (verifying a hit still describes the code it was indexed from) must compute it
    identically. Two copies of this rule in two modules is a latent staleness bug: any drift makes
    every verified hit look stale, or none of them. Applies the same truncation the indexer embeds
    under, so an oversized chunk hashes consistently on both sides. Idempotent."""
    if len(text) > MAX_CHUNK_CHARS:
        text = text[:MAX_CHUNK_CHARS]
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _base_dir() -> pathlib.Path:
    """The per-machine cache directory. Kept as a named seam so tests can redirect every model's db
    file at once (patch this, not each computed path); the resolution itself lives in
    ``codeintel.paths`` because config and auth need the identical answer."""
    return codeintel_home()


def _model_slug(model: str) -> str:
    # errors="replace" keeps this total (default_db_path promises it) even for a pathological
    # model string with unpaired surrogates — unreachable via config, but the docstring says total.
    return hashlib.sha256(model.strip().encode("utf-8", "replace")).hexdigest()[:12]


def default_db_path(model: str | None = None) -> str:
    """The per-machine semantic cache file for a given embedding ``model``. A sqlite-vec vec0 table
    is single-dimension and different models' vectors are incompatible, so each model gets its OWN
    file — different-model repos then coexist as separate files and can never corrupt or wipe each
    other. The default model (and ``None``) map to the legacy ``semantic.db`` (zero migration); any
    other model maps to ``semantic-<hash(model)>.db``.

    Index and search for one repo MUST pass the same model → same file. Rows are still partitioned
    by ``project_root`` WITHIN a shared-model file. Pure + total: any string yields a filename."""
    base = _base_dir()
    m = (model or "").strip()
    if not m or m == DEFAULT_MODEL:
        return str(base / "semantic.db")
    return str(base / f"semantic-{_model_slug(m)}.db")


class SemanticDb:
    """DB layer: opens a SQLite connection, loads sqlite-vec, and owns schema creation."""

    _DIM_RE = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # The vec0 embedding dimension, discovered lazily from the table / the first real vector
        # (see ensure_embeddings_table) rather than hardcoded — so any model's size just works.
        self.dimension: int | None = None

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

        # chunk_hashes + indexes are created now; code_embeddings is created LAZILY at the first
        # write, sized to the embedding model's real vector length (ensure_embeddings_table) — a
        # vec0 table is single-dimension, so it can't be created before the model's size is known.
        c.executescript("""
            CREATE TABLE IF NOT EXISTS chunk_hashes (
                chunk_id     TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                chunk_start  INT  NOT NULL,
                content_hash TEXT NOT NULL,
                -- Exclusive end line of the chunk's span. Nullable ONLY because caches predating
                -- this column are migrated with ALTER (below) rather than rebuilt; a NULL means
                -- "end unknown", and the searcher then cannot verify that row for staleness.
                -- The indexer backfills it in place on the next pass, without re-embedding.
                chunk_end    INT
            );

            -- Composite (project_root, file_path): serves the project-scoped scans
            -- (_cleanup_deleted, row-count, search KNN) via the leftmost prefix AND the
            -- per-file orphan reconcile / cleanup lookups, which would otherwise scan every
            -- row of the project once per file (O(files^2) on a large repo). Supersedes the
            -- old single-column idx_chunk_project, dropped here so migrated caches stay tidy.
            CREATE INDEX IF NOT EXISTS idx_chunk_project_file
                ON chunk_hashes(project_root, file_path);

            DROP INDEX IF EXISTS idx_chunk_project;

            -- When each project was last indexed. A separate table rather than a column on
            -- chunk_hashes, so adding it needs no migration and costs nothing per chunk.
            --
            -- `codeintel status` reported "Index age" from the mtime of the shared per-model
            -- database FILE, which every project writes to: indexing any other repository made a
            -- months-stale index look freshly built, and the number was most misleading exactly
            -- when a user was checking it because an answer looked wrong.
            CREATE TABLE IF NOT EXISTS project_index_meta (
                project_root TEXT PRIMARY KEY,
                indexed_at   REAL NOT NULL
            );
        """)

        # Migration: caches created before `chunk_end` lack it. Deliberately an ALTER and NOT the
        # drop-and-rebuild used above for `project_root`. That column partitions the cache, so a
        # cache without it is unusable and worth rebuilding; `chunk_end` only enables staleness
        # verification, and dropping the tables to gain it would force a full re-embed of every
        # project sharing this file (85k+ chunks on a working machine) to fix a bug that only
        # affects edited files. Existing rows get NULL — unverifiable, exactly the old behaviour —
        # and the indexer backfills each one in place on the next pass, without re-embedding.
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(chunk_hashes)").fetchall()]
            if cols and "chunk_end" not in cols:
                c.execute("ALTER TABLE chunk_hashes ADD COLUMN chunk_end INT")
                logger.info("migrated semantic cache: added chunk_hashes.chunk_end")
        except Exception as exc:
            # Never fatal: without the column the searcher simply cannot verify staleness, which
            # is where this code stood before the column existed.
            logger.warning("adding chunk_hashes.chunk_end failed: %s", exc)

        c.commit()

    def _table_dim(self) -> int | None:
        """The existing code_embeddings vec0 dimension from the live schema (``FLOAT[N]``), or None
        if the table is absent / unparseable."""
        try:
            row = self.conn().execute(
                "SELECT sql FROM sqlite_master WHERE name = 'code_embeddings'"
            ).fetchone()
            if not row or not row[0]:
                return None
            m = self._DIM_RE.search(str(row[0]))
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def ensure_embeddings_table(self, dim: int) -> int | None:
        """Ensure ``code_embeddings`` exists sized to ``dim`` (the embedding's true length). Returns
        the table dimension (== dim) on success, or ``None`` when it already exists at a DIFFERENT
        dimension — the caller then skips the write, never mixing dimensions and never wiping data.
        The table self-dimensions from the real vector, so any model (incl. future/unknown ones)
        just works. Never raises.

        A dimension mismatch is only reachable on the default-model file when a release bumps
        ``DEFAULT_MODEL`` to a new-sized model (a non-default file is keyed by model, so its dim is
        fixed); that release directs the user to ``codeintel reset`` once. Non-destructive here."""
        try:
            dim = int(dim)
            if self.dimension is None:
                self.dimension = self._table_dim()
            if self.dimension == dim:
                return dim
            if self.dimension is not None:
                logger.warning(
                    "embedding dimension %d != cache dimension %d — skipping write; run "
                    # `reset` alone cannot fix this: the vec0 table's dimension is fixed at
                    # creation and the table is SHARED across every project in this cache file,
                    # so a project-scoped reset (which only DELETEs that project's rows) leaves
                    # it in place and the warning repeats forever. `--all` drops the file.
                    "`codeintel reset --all` to rebuild the semantic index for the new model",
                    dim, self.dimension,
                )
                return None
            self.conn().execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS code_embeddings USING vec0("
                f"chunk_id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
            )
            self.conn().commit()
            self.dimension = dim
            return dim
        except Exception as exc:
            logger.warning("ensure_embeddings_table failed: %s", exc)
            return None

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
                # Tolerate the lazily-created embeddings table not existing yet: otherwise the
                # first orphan raises, the handler below swallows it, and every stale row survives
                # — a reconcile that silently does nothing is worse than one that reports failure.
                try:
                    conn.execute("DELETE FROM code_embeddings WHERE chunk_id = ?", (cid,))
                except sqlite3.OperationalError:
                    pass
                conn.execute("DELETE FROM chunk_hashes WHERE chunk_id = ?", (cid,))
            if orphans:
                conn.commit()
            return len(orphans)
        except Exception as exc:
            logger.warning("orphan reconcile failed for %s: %s", file_path, exc)
            return 0

    def mark_indexed(self, project_root_real: str, when: float | None = None) -> None:
        """Record that *project_root_real* finished an index pass. Never raises — a failure to
        record the timestamp must not fail the index that just succeeded."""
        try:
            conn = self.conn()
            conn.execute(
                "INSERT OR REPLACE INTO project_index_meta(project_root, indexed_at) VALUES (?, ?)",
                (project_root_real, time.time() if when is None else when),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("recording index time for %s failed: %s", project_root_real, exc)

    def indexed_at(self, project_root_real: str) -> float | None:
        """When this project was last indexed, or None if it never was (or the row predates this
        table — an index built before the table existed has no timestamp, and saying "unknown" is
        the honest answer rather than inventing one from a file mtime)."""
        try:
            row = self.conn().execute(
                "SELECT indexed_at FROM project_index_meta WHERE project_root = ?",
                (project_root_real,),
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception:
            return None

    def forget_project(self, project_root_real: str) -> int:
        """Drop every trace of a project — chunks, embeddings and its index timestamp.

        Used when a repository's root no longer exists. Its rows previously survived forever,
        because the index pass returned early when the root was missing and never reached the
        cleanup, so `doctor` went on reporting a deleted repository as indexed and healthy."""
        removed = 0
        try:
            conn = self.conn()
            chunk_ids = [r[0] for r in conn.execute(
                "SELECT chunk_id FROM chunk_hashes WHERE project_root = ?",
                (project_root_real,),
            ).fetchall()]
            for cid in chunk_ids:
                # `code_embeddings` is created LAZILY at the first write, sized to the model's
                # vector length — so on a database that has recorded chunk hashes but never
                # embedded (or one reset between passes) this table does not exist yet. Letting
                # that abort the loop left every chunk_hashes row in place, which is the exact
                # stale-row state this method exists to clear.
                try:
                    conn.execute("DELETE FROM code_embeddings WHERE chunk_id = ?", (cid,))
                except sqlite3.OperationalError:
                    pass
                conn.execute("DELETE FROM chunk_hashes WHERE chunk_id = ?", (cid,))
                removed += 1
            conn.execute("DELETE FROM project_index_meta WHERE project_root = ?",
                         (project_root_real,))
            conn.commit()
        except Exception as exc:
            logger.warning("forgetting project %s failed: %s", project_root_real, exc)
        return removed

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
