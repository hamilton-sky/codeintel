# Key the gateway cache on the index files, and report `reindexing` only for a real change

Type: task
Status: open
Blocked by: 01
Test first: yes
Level: 3
Builder: backend
Touches: src/codeintel/gateway/gateway.py, src/codeintel/gateway/cache.py, src/codeintel/gateway/reindexer.py, README.md, tests/test_cache.py, tests/test_reindexer.py, tests/test_reindex_lock.py, tests/test_gateway.py, tests/test_gateway_freshness.py

This ticket fixes T4 (`server-cache-survives-external-reindex`) and the reindexer half of T8 (`reindexing-flag-self-triggered`).

It is level 3 because it decides when `Reindexer.maybe_reindex` submits a minutes-long index pass (a run limit) and when a cached answer may be served. The fingerprint must never submit more passes than today's time debounce allows: a git command that fails or times out counts as "unchanged" for that call and falls back to the debounce, never to "submit". The `git` calls use an argv list, `cwd=root`, a timeout of at most 5 s, and no shell.

1. **Freshness key.**
   - In `Gateway.query`, the `freshness` passed to `ResultCache.get`/`put` becomes `f"{generation}:{graph_db_stamp(root)}:{semantic_db_mtime_ns}"`. `graph_db_stamp` is `core.paths.graph_db_stamp` from ticket 01. The semantic half is one `os.stat` of the semantic db path, read the way `engines/semantic/db.default_db_path` finds it; use 0 when the file is missing.
   - `ResultCache` already folds `freshness` into an f-string, so widen its annotation from `int` to `int | str`.
   - An external `codeintel index`, or a `reset` that deletes the db, then forces a miss.
2. **Change detection in `Reindexer.maybe_reindex`.**
   - Before submitting, compute a per-root fingerprint: HEAD sha plus the max mtime over `git ls-files -m -o --exclude-standard`. Use `subprocess` with an argv list and a short timeout.
   - Submit only when the fingerprint differs from the last submitted one. A non-git root keeps today's time debounce.
   - Return `True` when a pass was submitted, `False` otherwise.
3. **The `reindexing` flag.** In `Gateway.query`, read `reindex_pending(root)` BEFORE calling `maybe_reindex`. The answer is marked `reindexing` only when a pass was already running, or `maybe_reindex` returned `True` because the tree changed.
4. **Order.** `Reindexer._do_reindex` runs `_graph_reindex` before `_semantic_reindex`, so a blocked model download can't delay graph freshness. Each pass keeps its own try block, and the generation `finally` stays.
   - Tests that pin the old order change on purpose, and only their expected order: `tests/test_reindex_lock.py::test_a_pass_runs_when_the_lock_is_free` and `test_an_unusable_lock_directory_never_stops_indexing` assert `ran == ["semantic", "graph"]`; both become `["graph", "semantic"]`. Their lock assertions stay.
   - Keep the text `tests/test_index_failure_reason.py::test_the_background_reindexer_does_not_fail_silently` reads in `reindexer.py`: `if indexer.index(project_root) < 0:` and `semantic reindex failed`. `tests/test_reindexer.py::test_both_engines_failing_still_advances_freshness` also reads both `semantic reindex failed` and `graph reindex failed` from the log.
5. **Return value.** `maybe_reindex` now returns `bool`. `tests/test_reindexer.py::test_never_raises_on_bad_path` asserts `result is None`; it becomes `result is False` (the path does not exist, so nothing is submitted). Fake reindexers in other test files (`tests/test_gateway.py`, `tests/test_integration.py`, `tests/test_missing_target.py`, `tests/test_rbac.py`) return `None`; the gateway treats a falsy return as "no pass submitted", so those files need no change.
6. **README.**
   - Fix the `CODEINTEL_REINDEX=off` row: graph answers are not indexed inline; run `codeintel index`, and the cache notices it.
   - Fix the "the cache never lies … you never read a cached answer for code that moved on" claim so it states the new rule: the cache is invalidated by a reindex from any process.

## Not in this ticket

- The semantic failure record and the search fallback are ticket 10.
- `stale-index` inside answers is tickets 01 and 02.

## Done when

- New `tests/test_gateway_freshness.py` fails on main and passes here:
  - put a cached `callers` answer, touch the fake graph db (`os.utime` forward), and assert the next `Gateway.query` returns `cached: false`
  - delete the db and assert the same
- `tests/test_reindexer.py` gains these tests:
  - an unchanged git tree means `maybe_reindex` returns False, submits nothing, and the answer carries no `reindexing` flag
  - an edited file means it submits and returns True
  - `_do_reindex` calls the graph pass before the semantic pass
  - a `git` failure or timeout in the fingerprint submits no more than the time debounce allows (two calls inside the debounce window submit once)
- `tests/test_cache.py`: a different freshness string misses.
- Tests that need a graph db file point `CODEBASE_MEMORY_CACHE_DIR` at a `tmp_path` folder (`core.paths.graph_cache_dir` honours it), so the key never reads the machine's real cache.
- `tests/test_reindex_lock.py` passes with only the two order lists changed.
- `.venv/bin/pytest tests/test_gateway_freshness.py tests/test_reindexer.py tests/test_reindex_lock.py tests/test_cache.py tests/test_gateway.py tests/test_hardening.py tests/test_index_failure_reason.py tests/test_rbac.py tests/test_integration.py tests/test_missing_target.py tests/test_engine_adoption.py tests/test_cli_commands.py -q -o addopts=` passes, and all existing tests still pass. Only the test files on Touches change.

```check
$ .venv/bin/pytest tests/test_gateway_freshness.py tests/test_reindexer.py tests/test_reindex_lock.py tests/test_cache.py tests/test_gateway.py tests/test_hardening.py tests/test_index_failure_reason.py tests/test_rbac.py tests/test_integration.py tests/test_missing_target.py tests/test_engine_adoption.py tests/test_cli_commands.py -q -o addopts=
prints passed
```

## Reference

- spec.md, Decisions ("The gateway freshness key", "The reindexer's change detection")
- redblue-fixplan.md § T4 and § T8, last bullet
- The comment in `src/codeintel/gateway/gateway.py` about one-shot mode and a query reporting its own pass

## Answer
