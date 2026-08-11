# Plan Architecture — Debounced fire-and-forget incremental reindex seam

> This file records design decisions for this goal and maps them to implementation phases.
> The authoritative design lives here; IMPLEMENTATION_PLAN.md references these decisions.

## Key Design Decisions

### D1 — Module-level singleton in `server.py` (not inside `Gateway`)

`Reindexer` holds debounce state (`last_fired` dict). If it were constructed inside
`_build_gateway()` (which runs per-request), debounce state would reset on every request —
defeating the purpose. Moving it to a module-level `_REINDEXER = Reindexer()` gives it
process lifetime.

`Gateway.__init__` still accepts `reindexer=None` (defaulting to `Reindexer()`) so tests
can inject a mock without touching the server module.

### D2 — Environment-variable config, not a config file

The project has no config file system yet. `CODEINTEL_REINDEX=off` (env var) is the
simplest gate — zero new dependencies, compatible with all existing test harnesses, and
matches the "local-first, no network" principle. The flag is read at construction time so
test patching is straightforward.

### D3 — `ThreadPoolExecutor(max_workers=2)` in `Reindexer`

- `max_workers=2` allows concurrent reindex for two different roots without unbounded growth.
- Background threads are daemon threads — process exit does not hang.
- `executor.submit()` is non-blocking even when the work queue is full (Python's
  `ThreadPoolExecutor` uses an unbounded internal queue).

### D4 — Remove blocking `Indexer.index()` from `SemanticProvider` (Phase 2)

Before this goal, `SemanticProvider.build_result()` ran `Indexer(db).index(project_root)`
synchronously — meaning every query triggered a full (incremental) index walk. With
`Reindexer` taking over that responsibility, the call in `SemanticProvider` must be removed
to avoid double-indexing and to eliminate blocking on the query path.

**Trade-off:** The very first cold query will return a null result if no reindex has ever
run. The background reindex fires immediately on that first query; the second query (after
debounce) will see indexed data. This is acceptable per the SPEC ("change reflects within
one debounce window").

### D5 — Graph reindex via `codebase-memory-mcp detect_changes` subprocess

`GraphProvider` already uses subprocess calls to the CLI. `Reindexer` reuses the same
pattern for `detect_changes`. If the CLI is absent (`shutil.which` returns None), the graph
reindex step is silently skipped — consistent with `GraphProvider.available` behavior.

## Phase Mapping

### Phase 1 — `reindexer.py`
Implements D1 (state lives in Reindexer), D2 (env var flag), D3 (ThreadPoolExecutor), D5
(graph subprocess).

### Phase 2 — `semantic.py` (remove blocking index call)
Implements D4 — SemanticProvider becomes a pure search-only caller.

### Phase 3 — `gateway.py` wiring
Hooks `maybe_reindex` into the query path; isolates failure with try/except per D3's
never-raise contract.

### Phase 4 — `server.py` singleton
Implements D1's process-lifetime requirement.

### Phase 5–6 — Tests
Verifies D2 (env var gate), D3 (debounce, off-thread), D4 (no double-index), and the
never-raise contract.
