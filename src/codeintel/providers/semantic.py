from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
from typing import Any

from codeintel.loc import loc
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result
from codeintel.source_kind import partition_by_corpus

logger = logging.getLogger(__name__)

try:
    import fastembed  # noqa: F401
    import sqlite_vec  # noqa: F401
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False

# Cold-index background bookkeeping, module-level (not per-instance): `code.status`/`code.doctor`
# build their own ephemeral `SemanticProvider` to probe, and they must see the SAME in-flight job
# the query path started, not an empty dict on a throwaway instance. Keyed by the realpath of the
# project root so a trailing slash or a relative path can't be tracked as a second, independent job.
_BG_INDEX_LOCK = threading.Lock()
_BG_INDEX_STARTED: dict[str, float] = {}  # project_root (realpath) -> time.monotonic() at start
# The last background pass that FAILED for a root: (time.monotonic() at failure, cause).
#
# Without this, a pass that fails is indistinguishable from one that has not run. The thread's
# `finally` clears `_BG_INDEX_STARTED`, so the next request finds no index and no job in flight,
# starts another pass, and answers `indexing-in-progress` again — forever, about work that keeps
# dying, while the only actionable text goes to a server log the calling agent cannot see. An
# agent told "indexing is in progress" retries; it does not go and fix its proxy.
_BG_INDEX_FAILED: dict[str, tuple[float, str]] = {}

# How long a recorded failure suppresses both the retry and the `indexing-in-progress` answer.
# Sized to match `providers/lsp.py`'s `_COOLDOWN_SECONDS`, and deliberately the same shape as that
# one rather than a second invention: a failed LSP boot is already remembered, reported, and
# retried exactly once per window. The window is what makes a TRANSIENT failure self-clearing (the
# next request after it elapses tries again) while a PERMANENT one stays legible instead of being
# reported as progress.
_BG_INDEX_COOLDOWN_S = 60.0


def _index_key(project_root: str) -> str:
    try:
        return os.path.realpath(project_root)
    except Exception:
        return project_root


def _record_background_failure(key: str, cause: str) -> None:
    with _BG_INDEX_LOCK:
        # Sweep every expired entry, not just this key's. Pruning only on lookup of the SAME root
        # left a long-lived server accumulating one entry per one-off repo whose pass failed and
        # which nobody ever queried again — a claim of cleanup that the code did not keep. Recording
        # happens only on failure, so an O(n) sweep here costs nothing on the healthy path.
        now = time.monotonic()
        for stale in [k for k, (at, _) in _BG_INDEX_FAILED.items()
                      if now - at >= _BG_INDEX_COOLDOWN_S]:
            del _BG_INDEX_FAILED[stale]
        _BG_INDEX_FAILED[key] = (now, cause)


def _unexpired_failure_locked(key: str) -> str | None:
    """The standing failure for *key*, dropping it if its window has passed.

    Callers MUST already hold `_BG_INDEX_LOCK`. It exists as a separate function precisely so the
    "is there a failure?" question and the "start a pass" decision can happen under one
    acquisition — see `_start_background_index`.
    """
    entry = _BG_INDEX_FAILED.get(key)
    if entry is None:
        return None
    failed_at, cause = entry
    if time.monotonic() - failed_at < _BG_INDEX_COOLDOWN_S:
        return cause
    del _BG_INDEX_FAILED[key]
    return None


def _background_index_failure(project_root: str) -> str | None:
    """The cause of the last background pass for *project_root*, while its cooldown holds.

    ``None`` once the window has elapsed — which is what licenses exactly one retry, so a
    transient failure (a proxy that came back, a disk that was freed) clears itself without
    anyone intervening.
    """
    with _BG_INDEX_LOCK:
        return _unexpired_failure_locked(_index_key(project_root))


def _background_index_elapsed_s(project_root: str) -> float | None:
    """Seconds since a background cold-index for *project_root* started, or None if none is
    running. Used to make the in-progress state observable from `probe()` (doctor/status)."""
    with _BG_INDEX_LOCK:
        started = _BG_INDEX_STARTED.get(_index_key(project_root))
    return None if started is None else time.monotonic() - started


def _start_background_index(project_root: str, db_path: str, indexer_kwargs: dict) -> bool:
    """Kick off ONE cold-index pass for *project_root* on a daemon thread, unless one is already
    running for it. Returns True iff this call actually started a new pass.

    Daemon so it can never block interpreter shutdown. Runs on its OWN `SemanticDb` connection
    (sqlite3 connections aren't shared across threads) rather than the caller's — and is wrapped in
    its own try/except so a crash here (a blocked model download, a full disk) can only cost this
    one background pass, never the request thread or the server process."""
    key = _index_key(project_root)
    with _BG_INDEX_LOCK:
        if key in _BG_INDEX_STARTED:
            return False
        # The cooldown is enforced HERE, under the same lock that marks the start — not by the
        # caller's earlier check, which is a check-then-act race with a real losing interleaving:
        # a request looks up the failure and sees none because the thread has not recorded yet;
        # the thread then records its failure and clears `_BG_INDEX_STARTED`; the request, finding
        # no job in flight, starts another pass — and the `pop` that used to live here destroyed
        # the fresh cause on the way past. Concurrent polling (exactly what "retry shortly" tells
        # an agent to do) could bypass the cooldown indefinitely and keep answering
        # `indexing-in-progress`. Refusing here makes the decision atomic; the caller re-reads the
        # failure after a refusal to find out which kind it was.
        if _unexpired_failure_locked(key) is not None:
            return False
        # Only an EXPIRED entry is cleared, and `_unexpired_failure_locked` has already done it.
        # This is the one retry the window licenses.
        _BG_INDEX_STARTED[key] = time.monotonic()

    def _run() -> None:
        try:
            from codeintel.indexer import Indexer
            from codeintel.semantic_db import SemanticDb

            db = SemanticDb(db_path)
            try:
                db.init()
                # The return is CHECKED, not discarded. `index()` honours the never-raise
                # contract — it swallows the cause and returns -1 — so the `except` below can
                # never see the most likely failure here, a blocked model download on a machine
                # that has never warmed the cache. Discarding the result made this pass silent
                # end to end: the request that started it had already returned
                # `indexing-in-progress`, and every later query got the same answer forever,
                # because nothing ever recorded that the pass had failed.
                indexer = Indexer(db, **indexer_kwargs)
                if indexer.index(project_root) < 0:
                    cause = indexer.last_error or "unrecoverable failure"
                    logger.warning("background cold index failed for %s: %s", project_root, cause)
                    # RECORDED, not just logged. The log reaches an operator; the caller of this
                    # engine is an agent on the far side of MCP or HTTP, and the envelope is the
                    # only channel it has.
                    _record_background_failure(key, cause)
            finally:
                # `close()` cannot be allowed to raise out of this `finally`. An exception raised
                # in a `finally` REPLACES the one already propagating (the original is demoted to
                # `__context__`), so a failing close would hand the handler below a database-close
                # error in place of `db.init()`'s — recording a downstream symptom as the cause,
                # and losing the only sentence that tells the caller what to fix. It would equally
                # overwrite a cause already recorded above.
                #
                # Guarding the close at its own site fixes both directions at once, and is why no
                # "already recorded" flag is needed: nothing after the recording can raise.
                # Swallowed but never silent — a close that fails while the pass itself succeeded
                # is still worth an operator seeing.
                try:
                    db.close()
                except Exception as close_exc:
                    log_swallowed("SemanticProvider._start_background_index.close", close_exc)
        except Exception as exc:
            log_swallowed("SemanticProvider._start_background_index", exc)
            # A crash before or around the pass is a failed pass too. `index()` itself never
            # raises, but `db.init()` and the imports above it can, and a caller that gets
            # `indexing-in-progress` forever cannot tell the two apart.
            _record_background_failure(key, f"{type(exc).__name__}: {exc}")
        finally:
            with _BG_INDEX_LOCK:
                _BG_INDEX_STARTED.pop(key, None)

    threading.Thread(target=_run, daemon=True, name="codeintel-cold-index").start()
    return True


def _not_indexed_probe(project_root: str, detail: str) -> dict:
    """The `probe()` shape for 'this repo has no usable index yet' — with a distinct message when
    that gap is because a background cold-index is already filling it in (see
    `_start_background_index`), so `code.doctor`/`code.status` can say so instead of repeating
    'not indexed' unremediated on every check while it works."""
    # Same order as the query path, and for the same reason: `doctor` saying "indexing in
    # progress" about a pass that died is the diagnostic command repeating the misdiagnosis.
    bg_error = _background_index_failure(project_root)
    if bg_error is not None:
        return {
            # NOT runnable. A pass ran and could not complete, which is the same kind of statement
            # as "semantic cache present but unreadable" below — both of which this probe already
            # reports as `runnable: False`. Reporting `True` beside "a background index pass
            # failed: could not load embedding model … check network/proxy access" is a
            # contradiction on the face of one payload, and `code.status` hands those raw fields
            # to an agent that reads them rather than the prose.
            #
            # Scoped to the cooldown, like everything else here: once the entry expires this
            # branch is not taken, the probe falls through to its normal answer, and `runnable`
            # returns to True — the same one-retry policy the query path follows. The rolled-up
            # status was already `fail` via `repo_indexed`, so this corrects the field a consumer
            # reads directly, not the verdict.
            "installed": True, "runnable": False, "repo_indexed": False,
            "detail": f"a background index pass failed: {bg_error}",
            "remediation": f"fix the cause above, then run: codeintel index {project_root}",
        }
    elapsed = _background_index_elapsed_s(project_root)
    if elapsed is not None:
        return {
            "installed": True, "runnable": True, "repo_indexed": False,
            "detail": f"indexing in progress (started ~{elapsed:.0f}s ago) — {detail}",
            "remediation": "wait and retry, or run: "
                           f"codeintel index {project_root}  (indexes synchronously with progress)",
        }
    return {
        "installed": True, "runnable": True, "repo_indexed": False,
        "detail": detail, "remediation": f"codeintel index {project_root}",
    }


def _with_model_cache(probe: dict, model_cached: bool | None, model: str) -> dict:
    """Stamp a probe with whether the embedding weights are on disk, and say so when they are not.

    This is the question `doctor` could not previously ask. "Installed, runnable, repo not
    indexed" was reported identically whether nobody had run `index` yet or the weights every
    index pass needs had never been fetched and this machine cannot reach the host that serves
    them — two states with completely different fixes, and the second is the one that produces
    "it's installed, why doesn't it work". `doctor` distinguishing installed / runnable / indexed
    is the thing this tool is praised for; this is the fourth question, in the same style.

    Not a failure on its own: on a connected machine an uncached model is simply a download that
    has not happened yet, so it warns rather than failing (see `doctor._status_for`).
    """
    probe["model_cached"] = model_cached
    if model_cached is not False:
        return probe
    # Imported here, not at module scope: `semantic_db` imports `sqlite_vec`, and a top-level
    # import would raise on exactly the machines `_DEPS_OK` exists to degrade gracefully for —
    # turning a clean "engine-unavailable" into an import error at startup.
    from codeintel.semantic_db import MODEL_CACHE_ENV, MODEL_HOST
    note = (f"embedding weights for {model} are not cached yet — the next index downloads "
            f"~50 MB from {MODEL_HOST}")
    detail = str(probe.get("detail") or "")
    probe["detail"] = f"{detail}; {note}" if detail else note
    blocked = (f"blocked download? point {MODEL_CACHE_ENV} at a pre-seeded cache — see "
               f"docs/install.md, 'Offline / air-gapped install'")
    rem = str(probe.get("remediation") or "")
    probe["remediation"] = f"{rem}  ({blocked})" if rem else blocked
    return probe


def _plural(n: int, noun: str) -> str:
    """``3 matching chunks`` / ``1 matching chunk`` — these strings are read by a human deciding
    whether to re-index, and "1 matching chunks were dropped" reads like a formatting bug in the
    tool rather than a fact about their repo."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _preview(match: dict) -> str:
    """The one line of a hit a reader actually judges it by.

    A def longer than ``max_chunk_lines`` is window-split, so most of its chunks open mid-body and
    the first meaningful line is whatever the window happened to start on — real, correctly located
    text that nevertheless says nothing: `continue`, `except Exception as exc:`. Measured with
    ``ast`` across five indexed repositories, 11-33% of Python chunks start strictly inside a
    definition rather than at one.

    So when the indexer recorded an enclosing symbol and the line does not already name it, lead
    with the symbol. `searcher.py:373 | search() … continue` places the reader immediately; the
    bare `continue` never could. The check is a containment test rather than a start-of-line one
    because a def's own opening line (`def search(`) and a decorated or multi-line signature should
    not be prefixed with the name they already carry."""
    line = _first_meaningful_line(match.get("snippet", ""))
    symbol = match.get("symbol")
    if symbol and symbol not in line:
        return f"{symbol}() … {line}"
    return line


def _first_meaningful_line(snippet: str) -> str:
    """The first line of *snippet* that says something.

    The preview used line one unconditionally, so a hit whose chunk opened with a blank line, a
    `---` fence or a bare `#` rendered as `path:line | ---` — a result the reader cannot judge
    without opening the file, which is the one thing this output exists to avoid."""
    for line in snippet.splitlines():
        stripped = line.strip()
        if stripped and any(ch.isalnum() for ch in stripped):
            return stripped
    return snippet.strip().splitlines()[0] if snippet.strip() else ""


class SemanticProvider:
    """Real semantic search provider backed by SemanticDb and Searcher."""

    def __init__(self, blocking_index: bool = True) -> None:
        # True (the default) preserves today's behavior for every existing caller — the CLI (a
        # one-shot process that can afford to wait, and has no other mechanism to build a cold
        # index) and every direct/test construction of this class. The long-lived MCP/HTTP server
        # is the one caller that passes False: a full cold-index pass can run minutes past any
        # client tool timeout, so it must return promptly instead of blocking the request thread.
        self._blocking_index = bool(blocking_index)

    @property
    def available(self) -> bool:
        return _DEPS_OK

    def probe(self, project_root: str) -> dict:
        """Never-raise health check for the doctor. READ-ONLY: it opens the db read-only and counts
        this repo's chunks — it must NOT call SemanticDb.init() (a schema write) or LOAD fastembed.
        It does resolve the project's ``model`` *name* (a cheap config read, no model load) to pick
        the per-model cache file. ``repo_indexed`` is project-scoped (mirrors Searcher.has_index)."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": False,
                "model_cached": None,  # fastembed is absent — its cache is not the gap to report
                "detail": "fastembed / sqlite-vec not importable",
                "remediation": "pip install fastembed sqlite-vec  (or: pip install -e .)",
            }
        import os
        import sqlite3

        try:
            from codeintel.config import load_config
            from codeintel.semantic_db import DEFAULT_MODEL, default_db_path, model_is_cached
            model = str(load_config(project_root).get("model") or "")
            db_path = default_db_path(model)
        except Exception as exc:
            # Do NOT fold this into "no index database yet". Resolving the cache path fails when
            # the environment has no resolvable home directory — routine in a container running as
            # a UID with no passwd entry, which is how agents are often run — and reporting it as
            # "not indexed yet" sent the user to `codeintel index`, which fails the same way for
            # the same reason. They then loop between two commands, neither of which names the
            # actual problem. Say what broke and how to override it.
            return {
                "installed": True, "runnable": False, "repo_indexed": False,
                "model_cached": None,  # the model name never resolved — no claim to make
                "detail": f"cannot locate the index cache directory: {exc}",
                "remediation": "set HOME (or CODEINTEL_HOME) to a writable directory — this "
                               "environment has no resolvable home directory",
            }
        # Resolved once and stamped on every path below. A filesystem check by contract: `probe`
        # must not load the model, and this must not create the cache directory it is inspecting.
        named_model = model or DEFAULT_MODEL
        cached = model_is_cached(named_model)
        if not os.path.exists(db_path):
            return _with_model_cache(
                _not_indexed_probe(project_root, "no semantic index database yet"),
                cached, named_model,
            )
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                real = os.path.realpath(project_root) if project_root else ""
                row = conn.execute(
                    "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?", (real,)
                ).fetchone()
            finally:
                conn.close()
            count = int(row[0]) if row else 0
        except Exception as exc:
            return _with_model_cache({
                "installed": True, "runnable": False, "repo_indexed": False,
                "detail": f"semantic cache present but unreadable ({type(exc).__name__})",
                "remediation": f"codeintel reset {project_root} && codeintel index {project_root}",
            }, cached, named_model)
        if count > 0:
            return _with_model_cache({
                "installed": True, "runnable": True, "repo_indexed": True,
                "detail": f"{count} indexed chunks for this repo", "remediation": None,
            }, cached, named_model)
        return _with_model_cache(
            _not_indexed_probe(project_root, "semantic.db present but 0 chunks for this repo"),
            cached, named_model,
        )

    def build_result(
        self,
        op: str,
        target: str,
        files: list[str],
        budget: int,
        project_root: str,
    ) -> Result:
        # `context` (fan-out op) → semantic's contribution is a similarity search on the target.
        if op not in ("search", "context"):
            return safe_null_result(op, target, engine="semantic", reason="op-not-supported")
        if not self.available:
            return safe_null_result(op, target, engine="semantic", reason="engine-unavailable")
        if not project_root:
            return safe_null_result(op, target, engine="semantic", reason="no-project-root")

        try:
            from codeintel.config import load_config
            from codeintel.indexer import Indexer
            from codeintel.searcher import _RERANK_CANDIDATES_CAP, Searcher
            from codeintel.semantic_db import SemanticDb, default_db_path

            cfg = load_config(project_root)
            model = str(cfg.get("model") or "BAAI/bge-small-en-v1.5")

            # Per-model cache file: index and search for this repo use the SAME model → same file,
            # so a repo configured with a different model can never corrupt or wipe another's rows.
            db_path = default_db_path(model)
            pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            db = SemanticDb(db_path)
            db.init()

            searcher = Searcher(db, model_name=model)

            # A full index pass walks and hashes every file — too expensive to run on every query.
            # The background Reindexer (gated by the CODEINTEL_REINDEX env) already keeps a warm repo
            # fresh, so we only pay the inline pass on a COLD repo (nothing indexed yet). The one
            # exception: when that background reindexer is turned off, the inline pass is the only
            # thing keeping the index current, so we run it every query to preserve freshness — that
            # case always blocks, `self._blocking_index` or not, since nothing else will ever build it.
            background_reindex_off = (
                os.environ.get("CODEINTEL_REINDEX", "on").strip().lower() == "off"
            )
            indexer_kwargs: dict[str, Any] = {
                "model_name": model,
                "window": int(cfg.get("window", 20)),
                "stride": int(cfg.get("stride", 10)),
                "max_chunks": int(cfg.get("max_chunks", 500)),
                "max_total_chunks": int(cfg.get("max_total_chunks", 100000)),
                "chunk_strategy": str(cfg.get("chunk_strategy", "syntax")),
            }
            no_index = not searcher.has_index(project_root)
            # Why the inline pass failed, when it did. `Indexer.index` returns -1 and parks the
            # cause on `last_error` precisely so a caller can SHOW it instead of logging it; this
            # call site discarded both, so a failed pass and a repo nobody has indexed yet came
            # back as the same `no-index` — with a hint telling the reader to run the very thing
            # that had just failed, and the real cause (a blocked model download, an unwritable
            # cache) left in a stderr line they had no reason to connect to the answer.
            index_error: str | None = None
            if background_reindex_off or (no_index and self._blocking_index):
                indexer = Indexer(db, **indexer_kwargs)
                if indexer.index(project_root) < 0:
                    index_error = indexer.last_error or "unrecoverable failure"
            elif no_index:
                # Non-blocking caller (the long-lived MCP/HTTP server): a cold pass over a real repo
                # is minutes long (~500s / 25k chunks per docs/benchmarks.md) plus a one-time ~50MB
                # model download — running it inline here would stall this request past any client
                # tool timeout, with zero output, for every op this repo will ever answer first. Kick
                # it off in the background (deduped per project root — see `_start_background_index`)
                # and return the safe-null envelope immediately so the caller can retry shortly
                # instead of hanging with nothing.
                #
                # A pass that already FAILED for this repo is reported as a failure, not as
                # progress. Checked before starting another one: the previous behaviour restarted
                # the same doomed pass on every request and answered `indexing-in-progress` each
                # time, so a repo whose model download is blocked told the caller work was
                # underway forever while the cause sat in a log no agent can read. "Retry shortly"
                # is advice an agent follows; it is not advice that fixes a proxy.
                bg_error = _background_index_failure(project_root)
                if bg_error is not None:
                    return safe_null_result(
                        op, target, engine="semantic", reason="index-failed",
                        hint=(f"a background index pass ran for this repo and failed: {bg_error} "
                              f"— this is NOT 'still indexing'. Nothing is retried for "
                              f"{int(_BG_INDEX_COOLDOWN_S)}s; fix the cause, or run "
                              f"`codeintel index {project_root}` to index synchronously with "
                              f"progress output"),
                    )
                if not _start_background_index(project_root, db_path, indexer_kwargs):
                    # It refused. Either a pass is genuinely in flight, or a failure landed
                    # between the check above and the start — the race the helper closes. Re-read
                    # to find out which, so a refusal cannot be reported as progress.
                    bg_error = _background_index_failure(project_root)
                    if bg_error is not None:
                        return safe_null_result(
                            op, target, engine="semantic", reason="index-failed",
                            hint=(f"a background index pass ran for this repo and failed: "
                                  f"{bg_error} — this is NOT 'still indexing'. Nothing is retried "
                                  f"for {int(_BG_INDEX_COOLDOWN_S)}s; fix the cause, or run "
                                  f"`codeintel index {project_root}` to index synchronously with "
                                  f"progress output"),
                        )
                elapsed = _background_index_elapsed_s(project_root) or 0.0
                return safe_null_result(
                    op, target, engine="semantic", reason="indexing-in-progress",
                    hint=(f"first-time indexing of this repo started in the background "
                          f"~{elapsed:.0f}s ago (a cold pass can take several minutes on a large "
                          f"repo, plus a one-time embedding-model download) — retry this query "
                          f"shortly, or run `codeintel index {project_root}` to index synchronously "
                          f"with progress output"),
                )

            if not searcher.has_index(project_root):
                if index_error:
                    # Distinct from `no-index` because it licenses a different conclusion: the
                    # engine was asked and could not answer, rather than asked and found nothing.
                    return safe_null_result(
                        op, target, engine="semantic", reason="index-failed",
                        hint=(f"an inline index pass ran for this repo and failed: {index_error} "
                              f"— this is NOT 'never indexed'. Run `codeintel index "
                              f"{project_root}` to see the failure with progress output, or "
                              f"`codeintel doctor` to check the engine"),
                    )
                return safe_null_result(
                    op, target, engine="semantic", reason="no-index",
                    hint=f"run: codeintel index {project_root}  (or: codeintel doctor)",
                )

            # Over-retrieve, then fill from the code corpus first. Partitioning the FINAL ten was
            # not enough: on a doc-heavy repository all ten candidates were prose, so re-ordering
            # had nothing to promote. The bias is in retrieval, so the widening has to happen there
            # — ask for several times the display budget and let the code corpus claim its share.
            #
            # That widening is `rerank_candidates`, NOT a private multiplier. It used to be a
            # hardcoded `_display_k * 6`, which silently overrode the config key: `Searcher.search`
            # takes `max(k, rerank_candidates)` (it can never return k results from fewer than k
            # candidates), so a k of 60 swallowed every configured value at or below it — the
            # documented default of 30 changed nothing, and the `_RERANK_CANDIDATES_CAP` DoS guard
            # was bypassed by our own over-retrieval. One knob now owns candidate breadth, clamped
            # to that cap here so a large configured value can't reintroduce the bypass.
            #
            # `_display_k` is a CEILING on what gets shown, never a floor under the knob: flooring
            # the width at 10 would silently override a configured 7 and reintroduce, in miniature,
            # the exact override being removed here. Someone who asks for 7 candidates gets at most
            # 7 results.
            _display_k = 10
            width = min(int(cfg.get("rerank_candidates", 60)), _RERANK_CANDIDATES_CAP)
            matches = searcher.search(
                target, project_root, k=width,
                cosine_floor=float(cfg.get("cosine_floor", 0.25)),
                rerank=str(cfg.get("rerank", "on")),
                rerank_candidates=width,
            )
            if not matches:
                # Distinguish "nothing was similar enough" from "everything similar was stale".
                # Both used to report `below-floor`, which reads as "this code does not exist" —
                # the single most damaging thing to tell an agent about a repo it just edited.
                #
                # A search that FAULTED is the third member of that family and was the last one
                # still collapsed. `search()` returns `[]` for it exactly as it does for a genuine
                # miss, so a repo whose model cache is cold behind a proxy — or whose index is
                # unreadable — answered every single query with `below-floor`: a confident, wrong
                # statement about the repository, produced by an engine that was never able to ask
                # it anything. It is checked FIRST because it outranks the others: staleness
                # counts describe a search that ran, and this one did not.
                #
                # The message is stage-qualified by the searcher (embedding vs. vector search)
                # because those have unrelated fixes; this layer does not need to know which.
                if searcher.last_query_error:
                    return safe_null_result(
                        op, target, engine="semantic", reason="query-failed",
                        hint=(f"no search ran — {searcher.last_query_error}. This is NOT evidence "
                              f"that nothing matches; run `codeintel doctor {project_root}` to "
                              f"check the engine"),
                    )
                if searcher.last_stale:
                    return safe_null_result(
                        op, target, engine="semantic", reason="index-stale",
                        # Phrased to avoid subject-verb agreement entirely rather than hardcoding
                        # one number's verb: `_plural` exists so these strings read as facts about
                        # the repo, and "3 matching chunks ... was withheld" reads as a bug in the
                        # tool printing it.
                        hint=f"withheld {_plural(searcher.last_stale, 'matching chunk')} that "
                             f"could not be verified against the current source; "
                             f"run: codeintel index {project_root}",
                    )
                return safe_null_result(op, target, engine="semantic", reason="below-floor")

            # `m['line']` is the chunk's `chunk_start`, which is 0-based by construction in the
            # indexer (`start0 = max(0, start - 1)`). Emitting it raw put every semantic hit one
            # line above the truth and rendered anything at the top of a file as `path:0` — a line
            # number that does not exist. `loc()` owns the conversion for every engine.
            # Code first, prose second. Semantic search embeds implementations and the prose that
            # DESCRIBES them into one vector space, and prose about a subject is written in the
            # language of a question about that subject — so on a doc-heavy repository the docs
            # systematically outranked the code, and a query whose answer was a specific function
            # returned ten markdown files and zero source. Ranking within each corpus is unchanged;
            # only the interleaving is. Prose is kept, not dropped: it is often the right answer to
            # "how does this work", just not to "where is this done".
            code_hits, prose_hits = partition_by_corpus(matches)
            # Code first, but never a pure-code wall: prose genuinely answers "how does this work".
            # Reserve up to a third of the slots for prose when both corpora have hits.
            #
            # The prose reservation is a CEILING on prose, not a quota that must be met. Capping
            # code at two thirds unconditionally dropped slots whenever prose couldn't fill the
            # rest: 58 code hits and 2 prose hits returned 6 + 2 = 8 results, discarding 52
            # qualifying code hits to leave four slots empty. That fired on 8 of 18 sampled
            # queries against code-heavy repositories — losing up to 3 of 10 results — and it fired
            # hardest exactly where code hits are most plentiful, which inverts the intent of
            # ranking code first. Giving code at least `_display_k - len(prose_hits)` slots keeps
            # the one-third prose reservation whenever there IS prose to fill it, and hands the
            # remainder back to code when there isn't.
            if code_hits and prose_hits:
                keep_code = min(
                    len(code_hits),
                    max(_display_k - len(prose_hits), (_display_k * 2) // 3),
                )
                ordered = code_hits[:keep_code] + prose_hits[:_display_k - keep_code]
            else:
                ordered = (code_hits + prose_hits)[:_display_k]
            lines = [f"{loc(m['path'], m['line'])} | {_preview(m)}" for m in ordered]
            result: Result = {
                "ok": True,
                "op": op,
                "target": target,
                "result": "\n".join(lines),
                "engine": "semantic",
                "cached": False,
            }
            # The corpus mix is now a REPORTED gap rather than an unmodelled one. A caller that
            # asked "where is X done" and received only prose needs to know that no code matched —
            # otherwise an empty code corpus reads as "the implementation does not exist".
            gaps = []
            # A thinned list must never be passed off as a whole one. These hits were dropped
            # because the file no longer holds the code they were indexed from, so the answer is
            # "some of this repo is not currently searchable", not "this is everything".
            if searcher.last_stale:
                gaps.append({
                    "section": "freshness",
                    "kind": "stale-chunks-dropped",
                    "detail": f"{_plural(searcher.last_stale, 'matching chunk')} withheld: the "
                              f"source has changed since indexing, so the recorded location no "
                              f"longer points at the code that matched. Results here are "
                              f"incomplete — re-index to restore them "
                              f"(codeintel index {project_root}).",
                })
            # An index that CANNOT be checked must not be reported like one that passed. Rows
            # written before this release carry no chunk span, so verification silently does
            # nothing for them — and that is not a rare state, it is what every existing cache
            # looks like on the first query after upgrading, until a pass backfills the spans.
            # Without this the answer came back `confidence: complete` with no gap while offering
            # exactly the stale hit the verification was added to withhold, and the enclosing-symbol
            # preview made it read as MORE authoritative: a deleted `charge_credit_card` rendered
            # as `app.py:1 | charge_credit_card() … import logging`. Saying "unknown" is the whole
            # point of the gaps contract; a verification you cannot perform is unknown, not clean.
            if searcher.last_unverifiable:
                gaps.append({
                    "section": "freshness",
                    "kind": "unverified-chunks",
                    "detail": f"{_plural(searcher.last_unverifiable, 'hit')} could not be checked "
                              f"against the current source: this index predates staleness "
                              f"verification and has no recorded chunk spans, so a hit may point "
                              f"at code that has since moved or been deleted. Treat these "
                              f"locations as unconfirmed — one re-index enables checking "
                              f"(codeintel index {project_root}).",
                })
            if not code_hits and prose_hits:
                gaps.append({
                    "section": "corpus",
                    "kind": "no-code-matches",
                    "detail": f"no source file matched this query; all {len(prose_hits)} hits are "
                              f"documentation or fixtures. Absence of code hits here is NOT evidence "
                              f"the implementation is missing — try a symbol name, or `callers`.",
                })
            elif prose_hits and len(prose_hits) > len(code_hits):
                gaps.append({
                    "section": "corpus",
                    "kind": "prose-heavy",
                    "detail": f"{len(prose_hits)} of {len(ordered)} hits are documentation rather "
                              f"than code; code hits are listed first.",
                })
            return attach_confidence(result, gaps)
        except Exception as exc:
            log_swallowed("SemanticProvider.build_result", exc)
            return safe_null_result(op, target, engine="semantic", reason="provider-error")
