from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class _DaemonPool:
    """Runs background work on DAEMON threads so it can never block interpreter shutdown.

    A one-shot ``codeintel query`` must return immediately — with a non-daemon pool, Python's
    shutdown joins the workers, so a first query on a large repo hung for minutes while the
    repo-wide reindex finished (and, with buffered/piped stdout, the result never even flushed
    until the join completed). Daemon threads exit with the process; on the persistent server
    they run to completion normally. Rate is bounded by the Reindexer's debounce, so a
    thread-per-task is fine. ``shutdown(wait=)`` is kept for deterministic test draining."""

    def __init__(self, max_workers: int = 2) -> None:
        self._max_workers = max_workers  # advisory; the Reindexer debounce is the real rate limit
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(self, fn, *args) -> None:
        t = threading.Thread(target=fn, args=args, daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()]  # drop finished
            self._threads.append(t)
        t.start()

    def shutdown(self, wait: bool = True) -> None:
        if not wait:
            return
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join()


class Reindexer:
    def __init__(self, debounce_seconds: float = 30, enabled: bool = True) -> None:
        self._debounce_seconds = debounce_seconds
        self._enabled = (
            os.environ.get("CODEINTEL_REINDEX", "on").strip().lower() != "off"
            and enabled
        )
        self._lock = threading.Lock()
        self._last_fired: dict[str, float] = {}
        # Per-project index generation — bumped when a reindex completes. The gateway
        # folds it into the cache key so a structural answer is invalidated once the
        # index actually moves (a symbol/free-text target has no file content to hash).
        self._generation: dict[str, int] = {}
        self._executor = _DaemonPool(max_workers=2)

    def generation(self, project_root: str) -> int:
        with self._lock:
            return self._generation.get(project_root, 0)

    def maybe_reindex(self, project_root: str) -> None:
        if not self._enabled:
            return
        if not project_root:
            return

        now = time.monotonic()
        with self._lock:
            last = self._last_fired.get(project_root, 0.0)
            if now - last < self._debounce_seconds:
                return
            self._last_fired[project_root] = now

        # Passed the debounce gate — honor a per-project `reindex = "never"` opt-out before doing
        # expensive work, so that config key actually disables background reindexing (not only the
        # inline path). Checked post-debounce, so config is read at most once per window.
        if self._reindex_disabled(project_root):
            return

        self._executor.submit(self._do_reindex, project_root)

    def _reindex_disabled(self, project_root: str) -> bool:
        try:
            from codeintel.config import load_config
            return str(load_config(project_root).get("reindex") or "").strip().lower() == "never"
        except Exception:
            return False

    def _do_reindex(self, project_root: str) -> None:
        """Run both passes independently, then always advance the generation.

        These used to share one try block, semantic first. A semantic failure — a blocked model
        download on an air-gapped host, a full disk, a corrupt vector DB — therefore skipped the
        graph pass AND skipped the generation bump. The bump is the ONLY cache invalidation for
        non-file targets (`callers`, `impact`, `chain`, `hotspots`: `_compute_hash` of a symbol
        name never changes), so the counter stayed pinned at 0 for the life of the process and
        every cached answer was served `ok: true, cached: true` forever, however far the code
        moved on. A persistent failure retries every debounce window and fails identically, so
        this was permanent rather than transient.

        Silent, confident staleness is the worst failure this codebase can produce, so the
        generation now advances in a `finally`: one engine's outage degrades that engine's
        freshness, never the cache's correctness."""
        try:
            try:
                self._semantic_reindex(project_root)
            except Exception as exc:
                logger.warning("semantic reindex failed for %s: %s", project_root, exc)
            try:
                self._graph_reindex(project_root)
            except Exception as exc:
                logger.warning("graph reindex failed for %s: %s", project_root, exc)
        finally:
            with self._lock:
                self._generation[project_root] = self._generation.get(project_root, 0) + 1

    def _semantic_reindex(self, project_root: str) -> None:
        import pathlib

        from codeintel.config import load_config
        from codeintel.indexer import Indexer
        from codeintel.semantic_db import SemanticDb, default_db_path

        # Same per-model cache file the SemanticProvider reads — index and search must never diverge
        # onto different files. Honor the project's config so the background pass indexes exactly
        # like the inline and CLI paths (same model → same file, plus window/stride, ceilings).
        cfg = load_config(project_root)
        db_path = default_db_path(str(cfg.get("model") or ""))
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        db = SemanticDb(db_path)
        try:
            db.init()
            Indexer(
                db,
                model_name=str(cfg.get("model") or "BAAI/bge-small-en-v1.5"),
                window=int(cfg.get("window", 20)),
                stride=int(cfg.get("stride", 10)),
                max_chunks=int(cfg.get("max_chunks", 500)),
                max_total_chunks=int(cfg.get("max_total_chunks", 100000)),
                chunk_strategy=str(cfg.get("chunk_strategy", "syntax")),
            ).index(project_root)
        finally:
            db.close()

    def _graph_reindex(self, project_root: str) -> None:
        # Route through the graph provider's single subprocess/JSON seam (piped stdin, with a
        # deprecated raw-JSON fallback) rather than duplicating the deprecated raw-JSON call here.
        #
        # This called `detect_changes` with a `project_root` argument, and was wrong twice over:
        # the backend takes `project` (a name from list_projects), so every call returned an
        # argument error that `_run` folded into None — and `detect_changes` only REPORTS
        # uncommitted drift, it never reindexes. The graph therefore never refreshed, silently,
        # and queries answered from whatever the index held when it was first built.
        try:
            from codeintel.providers.graph import GraphProvider
            gp = GraphProvider()
            if not gp.available:
                return
            # `repo_path`, NOT `project_root`. The backend answers a wrong argument name with
            # "Indexing worker crashed on a file", which reads like a parser bug in some source
            # file and sent an earlier fix looking in the wrong place entirely; only the worker
            # log says `repo_path is required`. Verified against the real backend, not a mock —
            # a stub will happily confirm whichever name you assumed.
            result = gp._run("index_repository", {"repo_path": project_root}, 300_000)
            # Surface a backend-reported failure. Swallowing it is what let a broken reindex look
            # exactly like a working one for as long as nobody compared the graph to the source.
            if isinstance(result, dict) and result.get("status") == "error":
                logger.warning("graph index_repository reported an error for %s: %s",
                               project_root, result.get("hint") or result)
            elif result is None:
                logger.warning("graph index_repository returned nothing for %s "
                               "(backend timed out, crashed, or rejected the call)", project_root)
        except Exception as exc:
            logger.warning("graph index_repository failed: %s", exc)
