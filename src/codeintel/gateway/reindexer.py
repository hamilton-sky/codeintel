from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# The fingerprint's git calls run on the query path, so they are bounded hard. A slow or broken git
# never turns into a pass: the caller falls back to the time debounce (see `maybe_reindex`).
_FINGERPRINT_TIMEOUT_S = 5
# The stat walk over the listing runs on the query path too, and git's timeout does not bound it. A
# tree listing more files than this is not fingerprinted (``None``): it keeps the time debounce.
_FINGERPRINT_MAX_FILES = 50_000
# Roots whose last fingerprint is remembered, newest use kept. A forgotten root only costs one
# extra pass the next time it is queried, still bounded by the debounce.
_FINGERPRINT_ROOTS_MAX = 256


def _git_out(root: str, *args: str) -> bytes | None:
    """stdout of one git call in *root*, or ``None`` when git could not run, failed or timed out.

    An argv list and no shell. ``core.fsmonitor=false`` and the scrubbed environment are the same
    guards ``changed_range._git`` uses: a repository's own config must not run a program here, and
    an inherited ``GIT_DIR`` must not point the call at some other repository."""
    from codeintel.engines.graph.changed_range import _GIT, _env
    try:
        done = subprocess.run(
            [*_GIT, *args], cwd=root, capture_output=True, timeout=_FINGERPRINT_TIMEOUT_S,
            env=_env(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _tree_fingerprint(root: str) -> str | None:
    """HEAD's sha plus the newest mtime and a digest over every file git lists for the work tree.

    The listing is ``git ls-files -c -o --exclude-standard``: the tracked files and the untracked,
    not ignored ones, read from the index and a directory walk alone. NOT ``-m``: that flag compares
    the content of a tracked file whose stat moved, and the comparison runs the clean filter the
    repository's own config and ``.gitattributes`` name, so any repository could run a program on
    every query. Each listed file's ``(path, mtime_ns, size)`` goes into the digest, so an edit
    (staged or not), a new file, a deletion or a restore all move it, while the index file itself
    does not: the graph indexes the work tree, and a ``git status`` refreshing the index is no edit.
    ``None`` for a root git cannot fingerprint: not a work tree, no commit yet, git missing, a
    failure, a timeout, or a listing longer than ``_FINGERPRINT_MAX_FILES``."""
    head = _git_out(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if head is None:
        return None
    listed = _git_out(root, "ls-files", "-z", "-c", "-o", "--exclude-standard")
    if listed is None:
        return None
    if listed.count(b"\0") > _FINGERPRINT_MAX_FILES:
        return None
    paths = sorted({p for p in listed.decode("utf-8", "surrogateescape").split("\0") if p})
    digest = hashlib.sha256()
    newest = 0
    for rel in paths:
        try:
            # lstat: a tracked symlink is dated as the link. Following it could block this request
            # thread on a hung mount, and the graph does not index what a link points at.
            st = os.lstat(os.path.join(root, rel))
        except OSError:
            stamp = "-"  # deleted since it was listed or committed; its name still counts
        else:
            newest = max(newest, st.st_mtime_ns)
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0" + stamp.encode() + b"\0")
    return f"{head.decode('ascii', 'replace').strip()}:{newest}:{digest.hexdigest()}"


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
        # The tree fingerprint each git root had when its last pass was submitted, for at most
        # `_FINGERPRINT_ROOTS_MAX` roots. Dropped again when that pass is skipped or raises, so the
        # same tree is retried rather than counted as indexed.
        self._last_fingerprint: OrderedDict[str, str] = OrderedDict()
        # Per-project index generation — bumped when a reindex completes. The gateway
        # folds it into the cache key so a structural answer is invalidated once the
        # index actually moves (a symbol/free-text target has no file content to hash).
        self._generation: dict[str, int] = {}
        # Roots with a reindex submitted but not yet finished. The index is KNOWN to be behind
        # while this holds, and no amount of cache invalidation fixes that — re-asking just
        # refetches the same stale index, more expensively. The only honest response is to say so.
        self._in_flight: set[str] = set()
        self._executor = _DaemonPool(max_workers=2)

    def generation(self, project_root: str) -> int:
        with self._lock:
            return self._generation.get(project_root, 0)

    def reindex_pending(self, project_root: str) -> bool:
        """Whether this root has a reindex running right now — in ANY process.

        Lets a caller distinguish "this is the current structure" from "this is the structure as
        of the last completed index, and a newer one is being built". An agent's loop is edit →
        ask what I broke, which lands exactly in that window.

        Our own set is checked first and answers most calls without touching the filesystem. The
        cross-process probe matters for the case that set cannot see: an MCP server rebuilding a
        repo while the user runs `codeintel query` in a terminal, or a restarted server whose
        predecessor's pass is still running under another pid. Before this, each process reported
        only on itself, so the terminal said nothing was happening while a full reindex was."""
        if not project_root:
            return False
        with self._lock:
            if project_root in self._in_flight:
                return True
        from codeintel.core.filelock import held_by_another_process, lock_path
        return held_by_another_process(lock_path(os.path.realpath(project_root)))

    def maybe_reindex(self, project_root: str) -> bool:
        """Submit a background pass for *project_root*; ``True`` when submitted for a changed tree.

        A git root submits only when its fingerprint (`_tree_fingerprint`) differs from the one
        last submitted, so a query on an unchanged tree starts nothing, and the gateway does not
        report that query's own pass as "a reindex is in progress". The time debounce still bounds
        every root: a changed tree inside the window waits for the next query after it. A root git
        cannot fingerprint (not a repository, or git failed or timed out) keeps the time debounce
        alone, so a broken git never submits more passes than before.

        Such a pass is submitted but returns ``False``: nothing showed its tree changed, so the
        query that submitted it does not report ``reindexing`` (it would, on every window, for a
        tree nobody touched). A later query that finds the pass still running reports it through
        `reindex_pending`, as for any pass."""
        if not self._enabled:
            return False
        if not project_root:
            return False
        if not os.path.isdir(project_root):
            return False  # nothing there to index; a pass would only fail

        now = time.monotonic()
        with self._lock:
            # Already running: skip. The debounce timestamp is set when a pass is SUBMITTED, not
            # when it finishes, so on a repo whose reindex outlasts the window every later query
            # stacked another concurrent pass — overlapping writers against one SQLite file and
            # one graph subprocess, for no benefit.
            if project_root in self._in_flight:
                return False
            last = self._last_fired.get(project_root, float("-inf"))
            if now - last < self._debounce_seconds:
                return False

        # Outside the lock: up to two git calls, each bounded by `_FINGERPRINT_TIMEOUT_S`.
        fingerprint = _tree_fingerprint(project_root)

        with self._lock:
            # Re-checked: another thread may have submitted while git ran.
            if project_root in self._in_flight:
                return False
            last = self._last_fired.get(project_root, float("-inf"))
            if now - last < self._debounce_seconds:
                return False
            if fingerprint is not None and fingerprint == self._last_fingerprint.get(project_root):
                # Checked and unchanged: the window restarts, so an unchanged tree costs its git
                # calls once per window rather than on every query once the window has passed.
                self._last_fired[project_root] = now
                return False
            self._last_fired[project_root] = now
            if fingerprint is not None:
                self._last_fingerprint[project_root] = fingerprint
                self._last_fingerprint.move_to_end(project_root)
                while len(self._last_fingerprint) > _FINGERPRINT_ROOTS_MAX:
                    self._last_fingerprint.popitem(last=False)

        # Passed the debounce gate — honor a per-project `reindex = "never"` opt-out before doing
        # expensive work, so that config key actually disables background reindexing (not only the
        # inline path). Checked post-debounce, so config is read at most once per window.
        if self._reindex_disabled(project_root):
            return False

        with self._lock:
            self._in_flight.add(project_root)
        self._executor.submit(self._do_reindex, project_root)
        return fingerprint is not None

    def _reindex_disabled(self, project_root: str) -> bool:
        try:
            from codeintel.core.config import load_config
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
        from codeintel.core.filelock import exclusive, lock_path

        completed = False
        try:
            # One pass per repository across the whole machine. Measured 2026-09-18: two passes
            # started together each embedded all 600 chunks of a 600-file repo and each reported
            # success — they read the same "what is new" answer before either had written, so the
            # entire embedding cost is paid twice while both contend for the same cores.
            #
            # Non-blocking, and losing is not an error: whoever holds the lock is already doing
            # precisely this work, so there is nothing to wait for and nothing to report. The
            # generation still advances in the `finally` below, because the index does move — just
            # under someone else's pass.
            with exclusive(lock_path(os.path.realpath(project_root))) as acquired:
                if not acquired:
                    logger.debug("reindex of %s skipped: another process holds the lock",
                                 project_root)
                    return
                # Graph first: a semantic pass can block for minutes on a model download, and the
                # graph's freshness must not wait behind it.
                try:
                    self._graph_reindex(project_root)
                    graph_ok = True
                except Exception as exc:
                    graph_ok = False
                    logger.warning("graph reindex failed for %s: %s", project_root, exc)
                try:
                    self._semantic_reindex(project_root)
                    semantic_ok = True
                except Exception as exc:
                    semantic_ok = False
                    logger.warning("semantic reindex failed for %s: %s", project_root, exc)
                completed = graph_ok and semantic_ok
        finally:
            with self._lock:
                if not completed:
                    # Skipped (another process held the lock) or a pass raised: this tree is not
                    # indexed, so forget its fingerprint and let the next query past the debounce
                    # submit again instead of reading the unchanged tree as done.
                    self._last_fingerprint.pop(project_root, None)
                self._generation[project_root] = self._generation.get(project_root, 0) + 1
                self._in_flight.discard(project_root)

    def _semantic_reindex(self, project_root: str) -> None:
        import pathlib

        from codeintel.core.config import load_config
        from codeintel.engines.semantic.db import SemanticDb, default_db_path
        from codeintel.engines.semantic.indexer import Indexer

        # Same per-model cache file the SemanticProvider reads — index and search must never diverge
        # onto different files. Honor the project's config so the background pass indexes exactly
        # like the inline and CLI paths (same model → same file, plus window/stride, ceilings).
        cfg = load_config(project_root)
        db_path = default_db_path(str(cfg.get("model") or ""))
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        db = SemanticDb(db_path)
        try:
            db.init()
            # Bound to a name, and its return CHECKED. `Indexer.index()` honours the never-raise
            # contract: it swallows the cause and returns -1. Discarding that made the
            # `except Exception` in `_do_reindex` unreachable for the single most likely
            # background failure — a blocked model download on a machine that has never warmed
            # the cache — so the pass logged nothing at all, while the generation bump in
            # `_do_reindex`'s `finally` went on invalidating every cached answer in favour of an
            # index that had not moved. The bump is right (it protects cache correctness); the
            # silence was not. `_graph_reindex` below already surfaces its backend's failures for
            # exactly this reason; this is the sibling that never got the same treatment.
            model_name = str(cfg.get("model") or "BAAI/bge-small-en-v1.5")
            indexer = Indexer(
                db,
                model_name=model_name,
                window=int(cfg.get("window", 20)),
                stride=int(cfg.get("stride", 10)),
                max_chunks=int(cfg.get("max_chunks", 500)),
                max_total_chunks=int(cfg.get("max_total_chunks", 100000)),
                chunk_strategy=str(cfg.get("chunk_strategy", "syntax")),
            )
            # The outcome is persisted like the provider's own passes, so the next `search` in
            # any process answers a failed model load at once and doctor/status can name it.
            from codeintel.engines.semantic.provider import (
                _clear_model_failure,
                _is_model_load_failure,
                _write_model_failure,
            )
            if indexer.index(project_root) < 0:
                logger.warning(
                    "semantic reindex failed for %s: %s",
                    project_root, indexer.last_error or "unrecoverable failure",
                )
                if _is_model_load_failure(indexer.last_error or ""):
                    _write_model_failure(model_name, indexer.last_error or "")
            else:
                _clear_model_failure(model_name)
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
            from codeintel.engines.graph.provider import GraphProvider
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
                # `_run` now records WHICH of those it was — a refusal carries the backend's own
                # words — so say that instead of listing every possibility. The list stays only for
                # the case where nothing was recorded.
                failure = gp._last_failure
                logger.warning("graph index_repository returned nothing for %s: %s", project_root,
                               failure.detail if failure is not None
                               else "backend timed out, crashed, or rejected the call")
        except Exception as exc:
            logger.warning("graph index_repository failed: %s", exc)
