from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import sqlite3
import tempfile
import time

import sqlite_vec

from codeintel.paths import codeintel_home

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# Where `fastembed` fetches those weights on a cold cache, and the environment variable that
# redirects (or pre-seeds) the cache directory. Named here, beside the model itself, because the
# reason they need naming is a RUNTIME message: the download is the one non-local step in an
# otherwise local-first tool, and when a proxy blocks it the exception that surfaces is a bare
# `ProxyError: 403 Forbidden` — which says nothing about an embedding model, names no host, and
# offers no fix. docs/install.md has documented that exact failure, and the `FASTEMBED_CACHE_PATH`
# workaround, since the 2026-08-23 status eval; the knowledge simply never reached the user who
# was actually hitting it, because nobody reads the install doc while the install is what failed.
MODEL_HOST = "huggingface.co"
MODEL_CACHE_ENV = "FASTEMBED_CACHE_PATH"

def _one_line(text: str) -> str:
    """Collapse any run of whitespace — newlines included — to single spaces.

    `str.strip()` is not enough: it only touches the ends. Used on every field interpolated into
    a message that is contractually one line.
    """
    return " ".join(str(text).split())


class EmbeddingModelUnavailable(RuntimeError):
    """The embedding model could not be loaded, with the remedy that actually fits the cause.

    Raised where the model is loaded, NOT inferred afterwards from the exception text. That
    ordering is the point. An earlier version of this fix pattern-matched the raised exception
    against a list of network-ish substrings ("proxy", "403", "max retries", …) and attached the
    download story when one hit. It was wrong in both directions: an unrelated network failure
    during a long index pass got the embedding-model explanation, and a genuine blocked download
    whose message contained none of those words got nothing.

    But "classify at the operation" is not "assume one cause". `TextEmbedding(...)` fails for
    three materially different reasons, and telling someone whose *config* names a model fastembed
    does not ship to go and check their proxy is the same defect wearing different clothes. The
    remedy is therefore chosen from a STRUCTURAL signal — fastembed's own `ValueError` for an
    unsupported model, a `PermissionError` for a cache it cannot write — never from reading the
    message. Exception types are contracts; their text is not.

    ``str(...)`` is deliberately ONE line — `last_error` is rendered inline by `onboarding`
    (``f"indexing failed — {reason}"``), by the `index` CLI, and by the semantic engine's
    `index-failed` envelope, and a newline breaks the step table that exists to make the failure
    legible.
    """

    def __init__(self, model_name: str, cause: BaseException, remedy: str) -> None:
        self.model_name = model_name
        self.cause = cause
        self.remedy = remedy
        # EVERY interpolated field is flattened, not just the cause. `model_name` reaches here
        # straight from config, and `config._coerce` only `strip()`s it — which removes surrounding
        # whitespace but not an interior newline, so a TOML multi-line string
        # (`model = """BAAI/\nbge-small-en-v1.5"""`) put a line break in the middle of the
        # promise this class makes about itself. A direct `Indexer(model_name=...)` caller can do
        # the same. One-line is a contract with `onboarding`'s step table and the `index` CLI, so
        # it has to hold for every field rather than the one that happened to be untrusted first.
        text = _one_line(str(cause))
        detail = f"{type(cause).__name__}: {text}" if text else type(cause).__name__
        super().__init__(
            f"could not load embedding model '{_one_line(model_name)}' ({detail}) "
            f"— {_one_line(remedy)}"
        )


def load_embedder(model_name: str):
    """Construct fastembed's embedder, classifying a failed load at the operation.

    The single place both the indexer and the searcher build one, so neither can grow its own
    unclassified copy — the defect this replaces reached two call sites exactly that way.
    """
    from fastembed import TextEmbedding
    try:
        return TextEmbedding(model_name=model_name)
    except ValueError as exc:
        # fastembed's signal for a model it does not ship, raised from its own supported-model
        # list before any network call (measured: 0.000s, no request attempted). This is a
        # CONFIGURATION error — the `model` key names something that does not exist — and the
        # download remedy is actively misleading for it: there is no proxy to fix and no cache to
        # pre-seed, because nothing was ever going to be fetched.
        raise EmbeddingModelUnavailable(
            model_name, exc,
            "that is not a model fastembed ships — check the `model` key in your codeintel "
            "config; `TextEmbedding.list_supported_models()` lists the valid names",
        ) from exc
    except PermissionError as exc:
        # The cache directory exists and cannot be written. Also not a network problem: the
        # download would succeed and then have nowhere to land.
        raise EmbeddingModelUnavailable(
            model_name, exc,
            f"the fastembed cache at {model_cache_dir()} is not writable — fix its permissions, "
            f"or point {MODEL_CACHE_ENV} at a directory this user can write",
        ) from exc
    except Exception as exc:
        # What is left is the first-use fetch: this is the one with the network remedy, and the
        # one an external reviewer hit as a bare `403 Forbidden` naming neither model nor host.
        raise EmbeddingModelUnavailable(
            model_name, exc,
            f"fastembed downloads it (~50 MB) from {MODEL_HOST} on first use and codeintel makes "
            f"no other outbound request; check network/proxy access, or set {MODEL_CACHE_ENV} to "
            f"a directory pre-seeded with the model on a connected machine (see docs/install.md, "
            f"'Offline / air-gapped install'), then re-run",
        ) from exc


# Cap on the characters a single chunk contributes. `_maybe_split` splits on line boundaries, so a
# minified bundle or generated one-liner is one unsplittable chunk however large: a 20MB one-line
# .py peaked at 3.4GB RSS through the embedder, on the reindexer's daemon thread inside the
# long-lived MCP server. The head of a chunk carries its identifying content anyway.
MAX_CHUNK_CHARS = 200_000


def model_cache_dir() -> str:
    """Where `fastembed` keeps downloaded model weights, WITHOUT creating it.

    Mirrors `fastembed.common.utils.define_cache_dir` — `$FASTEMBED_CACHE_PATH`, else
    `$TMPDIR/fastembed_cache` — and deliberately does not call it, because that function
    `mkdir`s the directory as a side effect. `doctor` is documented as read-only and must not
    bring a cache into existence merely by asking whether one exists; a directory it created
    would also make the "does the model live here" question answer itself wrongly on the next
    run. The duplication is one `os.path.join` and is pinned by a test against the real
    fastembed resolution.
    """
    override = os.environ.get(MODEL_CACHE_ENV)
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "fastembed_cache")


# Ceiling on entries examined while looking for cached weights. A user is free to point
# FASTEMBED_CACHE_PATH at a large shared directory, and `doctor` is bounded (~3s) by contract:
# past this the honest answer is "could not determine", not a slow one.
_CACHE_SCAN_LIMIT = 20000


def model_is_cached(model_name: str = DEFAULT_MODEL) -> bool | None:
    """Whether *model_name*'s weights are already on disk. ``None`` means "could not determine".

    Answers the question `doctor` could not previously ask: an engine can be installed, runnable
    and pointed at an unindexed repo for two completely different reasons — nobody has indexed it
    yet, or the weights every index pass needs have never been fetched and this machine cannot
    reach the host that serves them. Those have different fixes, and collapsing them is why
    "it's installed, why doesn't it work" is the support burden it is.

    Deliberately a filesystem question, not a fastembed one. `ModelManagement.download_model(...,
    local_files_only=True)` would be authoritative, but it creates cache directories on the way to
    failing — a probe that mutates what it measures — and it logs its own errors to stderr, on top
    of the report. So: look for an ONNX weight file whose path names this model. fastembed's two
    layouts both carry the model's slug in the directory name (`bge-small-en-v1.5` for the GCS
    tarball, `models--qdrant--bge-small-en-v1.5-onnx-q` for the HuggingFace snapshot), which is
    what makes a slug match specific enough to not answer "yes" because some OTHER model is
    cached.

    Never raises: an unreadable cache directory is ``None`` (unknown), never ``False`` — claiming
    a model is missing because we could not look would send the reader to fix the wrong thing.
    """
    try:
        cache = model_cache_dir()
        if not os.path.isdir(cache):
            return False
        slug = str(model_name or DEFAULT_MODEL).rsplit("/", 1)[-1].lower()
        if not slug:
            return None
        seen = 0
        for dirpath, _dirnames, filenames in os.walk(cache):
            for name in filenames:
                seen += 1
                if seen > _CACHE_SCAN_LIMIT:
                    return None
                if not name.lower().endswith(".onnx"):
                    continue
                if slug in os.path.join(dirpath, name).lower():
                    return True
        return False
    except Exception:
        return None


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
                chunk_end    INT,

                -- Name of the definition this chunk sits inside, when it sits inside one. A def
                -- longer than `max_chunk_lines` is window-split, so most of its chunks start
                -- mid-body and their preview shows whatever line the window opened on — a bare
                -- `continue` or `except Exception as exc:`, which tells a reader nothing about
                -- where they are. Recording the enclosing symbol at index time (the parser
                -- already knows it) lets search render `search() … continue` instead. NULL for
                -- module-level chunks, and for rows written before this column existed.
                chunk_symbol TEXT
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
            for col, decl in (("chunk_end", "INT"), ("chunk_symbol", "TEXT")):
                if cols and col not in cols:
                    c.execute(f"ALTER TABLE chunk_hashes ADD COLUMN {col} {decl}")
                    logger.info("migrated semantic cache: added chunk_hashes.%s", col)
        except Exception as exc:
            # Never fatal: without the columns the searcher simply cannot verify staleness or name
            # a chunk's enclosing symbol — where this code stood before they existed.
            logger.warning("migrating chunk_hashes columns failed: %s", exc)

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
