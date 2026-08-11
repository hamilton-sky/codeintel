# Edge Cases — Debounced fire-and-forget incremental reindex seam

## Phase 1 — Reindexer edge cases

### EC-1.1 — `project_root` is empty string
- `maybe_reindex("")` — `Indexer.index("")` returns `0` (early exit). Graph detect_changes
  with empty root is safe: subprocess call is fire-and-forget and result is ignored.
- Expected: no exception, no thread if `project_root` is falsy. Add a guard:
  `if not project_root: return`.

### EC-1.2 — `project_root` does not exist
- `Indexer._index()` checks `root.exists()` and returns `0` — safe.
- `detect_changes` subprocess will exit non-zero; result is ignored.
- Expected: background thread completes silently.

### EC-1.3 — Concurrent calls for the same root
- Two MCP requests arrive simultaneously within the debounce window for the same
  `project_root`.
- Expected: the lock in `maybe_reindex` ensures only one thread is submitted; the second
  call returns immediately after the lock check.

### EC-1.4 — Concurrent calls for different roots
- Two requests for `/proj/a` and `/proj/b` arrive simultaneously.
- Expected: each gets its own debounce slot; both may fire independent threads (up to
  `max_workers=2` in the ThreadPoolExecutor).

### EC-1.5 — ThreadPoolExecutor queue overflow
- If more than `max_workers` background tasks are pending (e.g., many distinct roots),
  additional submissions queue internally. The caller is never blocked.
- Expected: `executor.submit()` is non-blocking regardless of queue depth.

### EC-1.6 — Embedder model not installed (fastembed import fails)
- `Indexer._get_embedder()` raises `ImportError` inside the background thread.
- Expected: `_do_reindex` `try/except` catches it; warning logged; thread exits cleanly.

## Phase 2 — SemanticProvider after removing Indexer call

### EC-2.1 — First query on a fresh install (DB never indexed)
- `Searcher.search()` returns empty list → `safe_null_result(reason="below-floor")`.
- This is the same behavior as before (previously the Indexer call would have run first
  synchronously, but with empty DB the result was the same for the very first call).
- Expected: null result on first cold query; Reindexer fires in background; second query
  (after debounce) sees indexed data.

### EC-2.2 — SemanticProvider `available` is False (deps missing)
- Short-circuits before `Searcher` call — no change to this path.

## Phase 3 — Gateway wiring

### EC-3.1 — `maybe_reindex` raises unexpectedly
- The `try/except Exception: pass` guard in `Gateway.query()` catches it.
- The query continues to cache lookup and dispatch — result is unaffected.

### EC-3.2 — `project_root` is `None`
- `str(None)` = `"None"` — not a useful root. Guard: `maybe_reindex(str(project_root or ""))`.
- `Reindexer.maybe_reindex("")` hits the empty-string guard and returns immediately.

## Phase 4 — Server singleton

### EC-4.1 — `CODEINTEL_REINDEX=off` set after process start
- `_REINDEXER` is constructed at import time. Changing the env var after import has no effect.
- Expected: documented behavior — set before process start.

### EC-4.2 — Server restarted
- `_REINDEXER` is fresh with empty `last_fired`; first query after restart triggers a reindex.
- Expected: one reindex per root after each server start, then debounced.

## Phase 5–6 — Tests

### EC-5.1 — Test isolation for debounce state
- `Reindexer` instances are per-test; no shared state between tests.
- Expected: each test constructs its own `Reindexer()`.

### EC-5.2 — Thread timing in tests
- Background thread may not finish before assertion. Use `executor._work_queue.join()` or
  a short `time.sleep(0.1)` guarded with a timeout, or mock `executor.submit` to run
  synchronously.
