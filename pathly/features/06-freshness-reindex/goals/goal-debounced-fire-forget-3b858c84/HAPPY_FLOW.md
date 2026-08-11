# Happy Flow — Debounced fire-and-forget incremental reindex seam

> Ideal runtime path: user edits a file, issues a query, sees fresh results within one debounce window.

## Phase 1 — Developer edits a source file

1. Developer edits `src/myproject/foo.py` in their editor.
2. No codeintel action yet — the edit is local.

## Phase 2 — Query arrives at the MCP server

1. Claude (or another MCP client) issues `code.query` with `op="search"`, `target="MyClass"`,
   `project_root="/path/to/myproject"`.
2. `server.py: code_query_handler` receives the args dict.

## Phase 3 — Gateway fires `maybe_reindex` (non-blocking)

1. `_build_gateway()` returns a `Gateway` with the module-level `_REINDEXER` singleton.
2. `Gateway.query()` calls `self._reindexer.maybe_reindex("/path/to/myproject")`.
3. `Reindexer.maybe_reindex`:
   a. Checks the enabled flag → `True`.
   b. Acquires the lock, checks `last_fired` — more than `debounce_seconds` have passed →
      updates `last_fired`, releases lock.
   c. Submits `_do_reindex` to the `ThreadPoolExecutor` → returns immediately.
4. `query()` continues to the cache lookup and provider dispatch.

## Phase 4 — Background reindex runs concurrently

1. `_do_reindex("/path/to/myproject")` executes in a daemon thread:
   a. Constructs `SemanticDb`, calls `db.init()`.
   b. Calls `Indexer(db).index("/path/to/myproject")` — walks files, hashes chunks, embeds
      only the changed chunk from `foo.py`, commits.
   c. If `codebase-memory-mcp` is on PATH, runs `detect_changes` via subprocess.
   d. No exception escapes the `try/except` wrapper.

## Phase 5 — Query result returned immediately

1. `SemanticProvider.build_result()` calls `Searcher(db).search("MyClass", ...)` against the
   current DB (may use slightly stale data if reindex hasn't finished yet on this first call —
   that is acceptable per the debounce contract).
2. `Gateway` caches the result, returns `{"ok": True, "result": "...", "engine": "semantic"}`.
3. MCP server returns the result to the client. No delay from reindex.

## Phase 6 — Subsequent query sees fresh results

1. Reindex thread has committed the new chunk for `foo.py` by the time the next query arrives.
2. `maybe_reindex` is debounced — no new thread starts.
3. `Searcher` finds the updated chunk and returns fresh results.
4. Developer sees the change reflected.
