# Implementation Plan — Debounced fire-and-forget incremental reindex seam

> Read FEATURE_INDEX.md first — it maps every source file to its conversation.
> Each phase has one file, one observable done-when, and a verify command.

---

## Conversation 1 — Core reindexer + wiring

### Phase 1 — Create `reindexer.py` with debounced fire-and-forget

File: `src/codeintel/reindexer.py`

Purpose: Central seam for on-demand incremental reindex. Callers fire-and-forget;
internal state gates how often a real reindex runs.

Done when: `Reindexer().maybe_reindex("/some/path")` returns immediately; a daemon thread
is enqueued to run graph + semantic incremental reindex; a second call within the debounce
window does NOT start another thread.

Depends on: `src/codeintel/indexer.py` (semantic), `src/codeintel/providers/graph.py`
(graph), `src/codeintel/semantic_db.py` (SemanticDb)

Enables: Phase 3 (gateway wiring), Phase 4 (server singleton)

Verify: `python3 -c "from codeintel.reindexer import Reindexer; r = Reindexer(); r.maybe_reindex('.'); print('ok')"`

**Builder prompt:**

Create `src/codeintel/reindexer.py`. Requirements:

1. `class Reindexer` with `__init__(self, debounce_seconds: int = 30, enabled: bool = True)`.
2. `def maybe_reindex(self, project_root: str) -> None`:
   - If `not self._enabled`, return immediately (no-op).
   - Thread-safe: use `threading.Lock` to guard a `dict[str, float]` of last-fired timestamps per `project_root`.
   - If `time.monotonic() - last_fired < debounce_seconds`, return immediately (debounced).
   - Otherwise update `last_fired[project_root]` and submit a daemon task to a `ThreadPoolExecutor(max_workers=2)`.
3. Background task `_do_reindex(project_root: str) -> None`:
   - Wrap the entire body in `try/except Exception: logger.warning(...)` — never raise.
   - **Semantic**: import `SemanticDb`, `Indexer`, construct with the default DB path
     (`Path.home() / ".codeintel" / "semantic.db"`), call `db.init()`, `Indexer(db).index(project_root)`.
   - **Graph**: if `shutil.which("codebase-memory-mcp")` is truthy, run
     `subprocess.run(["codebase-memory-mcp", "cli", "detect_changes",
     json.dumps({"project_root": project_root})], capture_output=True, timeout=30)` — ignore result.
4. `_enabled` reads `os.environ.get("CODEINTEL_REINDEX", "on").strip().lower() != "off"` at
   construction time (so tests can patch env before instantiation).
5. All methods honor the never-raise / safe-null contract — no exception may escape.

Do NOT touch any other file yet.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout src/codeintel/reindexer.py` and retry.

---

### Phase 2 — Remove blocking index call from `SemanticProvider`

File: `src/codeintel/providers/semantic.py`

Purpose: `SemanticProvider.build_result()` currently calls `Indexer(db).index(project_root)`
synchronously on every query. Now that `Reindexer` handles this off-thread, the blocking call
must be removed so queries are never delayed by indexing.

Done when: `SemanticProvider.build_result()` no longer calls `Indexer`. It only calls
`Searcher(db).search(...)`. An index must have been run at least once before results appear,
but that is now Reindexer's responsibility.

Depends on: Phase 1 (Reindexer exists and will handle indexing)

Enables: Phase 3 (gateway wiring makes Reindexer the sole index trigger)

Verify: `grep -n "Indexer" src/codeintel/providers/semantic.py` — should return 0 lines.

**Builder prompt:**

Edit `src/codeintel/providers/semantic.py`. Remove the `Indexer` import and the
`Indexer(db).index(project_root)` call inside `build_result()`. The `SemanticDb` construction,
`db.init()`, and `Searcher(db).search(target, project_root)` remain unchanged.

Do NOT touch `__init__`, `available`, or any other method.

After removing the Indexer call, if `matches` is empty return the existing `safe_null_result`
with `reason="below-floor"` — no change to the null-result path.

Do NOT touch any other file yet.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout src/codeintel/providers/semantic.py` and retry.

---

### Phase 3 — Wire `Reindexer` into `Gateway`

File: `src/codeintel/gateway.py`

Purpose: Every `Gateway.query()` call triggers `maybe_reindex` so the indexes are always
kept fresh relative to the debounce window, without blocking the response.

Done when: `Gateway.__init__` accepts an optional `reindexer` parameter; `Gateway.query()`
calls `self._reindexer.maybe_reindex(project_root)` before the cache lookup; the call is
wrapped in a `try/except` guard so no reindexer failure can propagate.

Depends on: Phase 1 (Reindexer API), Phase 2 (SemanticProvider no longer self-indexes)

Enables: Phase 4 (server uses a singleton Reindexer), AC2.1

Verify: `python3 -c "from codeintel.gateway import Gateway; g = Gateway(); print(g._reindexer)"`

**Builder prompt:**

Edit `src/codeintel/gateway.py`.

1. Add `from codeintel.reindexer import Reindexer` at the top.
2. In `Gateway.__init__`, add `reindexer: Reindexer | None = None` parameter. Set
   `self._reindexer = reindexer or Reindexer()`.
3. At the top of `Gateway.query()` — before the `op_str` parsing, inside the outer
   `try/except` — add:
   ```python
   try:
       self._reindexer.maybe_reindex(str(project_root or ""))
   except Exception:
       pass
   ```
4. That is the only change. Do NOT modify `_fan_out`, `_merge`, `_dispatch_single`, cache
   logic, or any other method.

Do NOT touch any other file yet.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout src/codeintel/gateway.py` and retry.

---

### Phase 4 — Wire singleton `Reindexer` into `server.py`

File: `src/codeintel/server.py`

Purpose: The MCP server creates a new `Gateway` on every request via `_build_gateway()`.
A module-level `Reindexer` singleton ensures debounce state persists across requests within
the same server process.

Done when: `server.py` has a module-level `_REINDEXER = Reindexer()` and `_build_gateway()`
passes it to `Gateway(..., reindexer=_REINDEXER)`. Setting `CODEINTEL_REINDEX=off` before
starting the server disables all background reindexing.

Depends on: Phase 3 (Gateway accepts `reindexer=`)

Enables: AC3.1, AC3.2 — runtime config gate

Verify: `CODEINTEL_REINDEX=off python3 -c "from codeintel.server import _REINDEXER; print(_REINDEXER._enabled)"` → prints `False`

**Builder prompt:**

Edit `src/codeintel/server.py`.

1. Add `from codeintel.reindexer import Reindexer` at the top.
2. Add a module-level singleton immediately after the imports:
   `_REINDEXER = Reindexer()`
3. In `_build_gateway()`, pass `reindexer=_REINDEXER` to the `Gateway(...)` constructor call.
4. No other changes.

Do NOT touch `run()`, the MCP tool registration, or the status handler.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout src/codeintel/server.py` and retry.

---

## Conversation 2 — Tests

### Phase 5 — Unit tests for `Reindexer`

File: `tests/test_reindexer.py`

Purpose: Verify debounce, off-thread behavior, config gate, and never-raise contract.

Done when: `pytest tests/test_reindexer.py` passes green with no warnings about blocking IO.

Depends on: Phase 1 (Reindexer exists)

Enables: Phase 6 (integration coverage), AC1–AC4 verification

Verify: `pytest tests/test_reindexer.py -v`

**Builder prompt:**

Create `tests/test_reindexer.py`. Write the following tests (use `unittest.mock.patch` to
avoid real filesystem or subprocess calls — mock `Indexer.index` and `subprocess.run`):

1. `test_maybe_reindex_fires_background_thread` — call `maybe_reindex("/tmp/test")` once,
   give the thread pool 0.5 s, verify `Indexer.index` (or `subprocess.run`) was called.
2. `test_debounce_suppresses_second_call` — call `maybe_reindex("/tmp/test")` twice in quick
   succession, verify index is called at most once.
3. `test_disabled_via_env` — patch `os.environ` with `CODEINTEL_REINDEX=off`, construct
   `Reindexer()`, call `maybe_reindex("/tmp/test")`, verify index is never called.
4. `test_never_raises_on_bad_path` — call `maybe_reindex("/nonexistent/path/xyz")`, assert
   no exception is raised and the function returns `None`.
5. `test_never_raises_on_indexer_exception` — patch `Indexer.index` to `raise RuntimeError`,
   call `maybe_reindex`, join threads, verify no exception propagates.

Do NOT touch any source file. Do NOT write tests for other modules.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.

---

### Phase 6 — Integration test: gateway calls reindexer on query

File: `tests/test_gateway.py`

Purpose: Confirm that `Gateway.query()` triggers `maybe_reindex` with the correct
`project_root`, and that reindexer failures do not propagate to the query result.

Done when: Existing `test_gateway.py` tests still pass; two new tests added green.

Depends on: Phase 3 (gateway wiring), Phase 4 (server singleton)

Enables: AC2.1 — non-blocking queries verified

Verify: `pytest tests/test_gateway.py -v`

**Builder prompt:**

Edit `tests/test_gateway.py`. Add two new test functions (do not modify existing tests):

1. `test_query_calls_maybe_reindex` — patch `Reindexer.maybe_reindex`, construct a
   `Gateway(reindexer=mock_reindexer)`, call `gw.query(op="search", target="foo",
   project_root="/tmp/proj")`, assert `mock_reindexer.maybe_reindex.called_with("/tmp/proj")`.
2. `test_reindexer_failure_does_not_affect_query_result` — make `Reindexer.maybe_reindex`
   raise `RuntimeError`, verify `gw.query(...)` still returns a `Result` dict with `"ok": True`.

Do NOT add or modify any other tests. Do NOT touch source files.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout tests/test_gateway.py` and retry.
