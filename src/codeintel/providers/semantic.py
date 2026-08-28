from __future__ import annotations

import os
import pathlib
import threading
import time
from typing import Any

from codeintel.loc import loc
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result
from codeintel.source_kind import partition_by_corpus

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


def _index_key(project_root: str) -> str:
    try:
        return os.path.realpath(project_root)
    except Exception:
        return project_root


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
        _BG_INDEX_STARTED[key] = time.monotonic()

    def _run() -> None:
        try:
            from codeintel.indexer import Indexer
            from codeintel.semantic_db import SemanticDb

            db = SemanticDb(db_path)
            try:
                db.init()
                Indexer(db, **indexer_kwargs).index(project_root)
            finally:
                db.close()
        except Exception as exc:
            log_swallowed("SemanticProvider._start_background_index", exc)
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
                "detail": "fastembed / sqlite-vec not importable",
                "remediation": "pip install fastembed sqlite-vec  (or: pip install -e .)",
            }
        import os
        import sqlite3

        try:
            from codeintel.config import load_config
            from codeintel.semantic_db import default_db_path
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
                "detail": f"cannot locate the index cache directory: {exc}",
                "remediation": "set HOME (or CODEINTEL_HOME) to a writable directory — this "
                               "environment has no resolvable home directory",
            }
        if not os.path.exists(db_path):
            return _not_indexed_probe(project_root, "no semantic index database yet")
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
            return {
                "installed": True, "runnable": False, "repo_indexed": False,
                "detail": f"semantic cache present but unreadable ({type(exc).__name__})",
                "remediation": f"codeintel reset {project_root} && codeintel index {project_root}",
            }
        if count > 0:
            return {
                "installed": True, "runnable": True, "repo_indexed": True,
                "detail": f"{count} indexed chunks for this repo", "remediation": None,
            }
        return _not_indexed_probe(project_root, "semantic.db present but 0 chunks for this repo")

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
            if background_reindex_off or (no_index and self._blocking_index):
                Indexer(db, **indexer_kwargs).index(project_root)
            elif no_index:
                # Non-blocking caller (the long-lived MCP/HTTP server): a cold pass over a real repo
                # is minutes long (~500s / 25k chunks per docs/benchmarks.md) plus a one-time ~50MB
                # model download — running it inline here would stall this request past any client
                # tool timeout, with zero output, for every op this repo will ever answer first. Kick
                # it off in the background (deduped per project root — see `_start_background_index`)
                # and return the safe-null envelope immediately so the caller can retry shortly
                # instead of hanging with nothing.
                _start_background_index(project_root, db_path, indexer_kwargs)
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
