# Key the gateway cache on the index files, and report `reindexing` only for a real change

Type: task
Status: resolved
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

Work started at commit 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f. Test-only commit: e2e7d8e.

**Built**
- `src/codeintel/gateway/reindexer.py`: `_git_out` (argv list, `cwd=root`, `timeout=_FINGERPRINT_TIMEOUT_S` = 5, no shell, reusing `changed_range._GIT` with `core.fsmonitor=false` and its scrubbed `_env()`), `_tree_fingerprint(root)` = HEAD sha + newest `st_mtime_ns` over `git ls-files -z -m -o --exclude-standard` + a hash of the listed paths; `None` when git cannot answer. `maybe_reindex` now returns `bool`: False for disabled, empty or non-directory roots, in flight, inside the debounce window, an unchanged fingerprint, or `reindex = "never"`; True only when it submitted. `_do_reindex` runs `_graph_reindex` before `_semantic_reindex`, each in its own try; the `finally` is unchanged.
- `src/codeintel/gateway/cache.py`: `freshness: int | str` on `get`/`put`.
- `src/codeintel/gateway/gateway.py`: `_query` reads `reindex_pending` before `maybe_reindex`; `reindexing` = pending or `maybe_reindex(...)` returned True. Freshness = `f"{generation}:{graph_db_stamp(root)}:{_semantic_db_stamp(root)}"` (`"{generation}:0:0"` when no root). New `_semantic_db_stamp(root)`: `default_db_path(load_config(root)["model"])`, max mtime_ns of the db and its `-wal`, 0 when missing, never raises.
- `README.md`: the "cache never lies" bullet now says the cache is invalidated by a reindex from any process; the `CODEINTEL_REINDEX=off` row says graph answers are not indexed inline, run `codeintel index`, and the cache notices; the "honest caveat" paragraph's "only when a background reindex completes" now also names `codeintel index`.
- Tests: new `tests/test_gateway_freshness.py` (4 tests); `tests/test_reindexer.py` (6 new tests, `result is False`); `tests/test_cache.py` (1 new test); `tests/test_reindex_lock.py` (only the two order lists).

**Proof**
- New `tests/test_gateway_freshness.py` fails on main, passes here: at test-only commit e2e7d8e (src = 03957a6) `pytest tests/test_gateway_freshness.py tests/test_reindexer.py tests/test_reindex_lock.py tests/test_cache.py -q -o addopts=` gave 13 failed, 42 passed; the db-touch, db-delete and semantic-db tests failed on `assert True is False` (`cached` stayed true). After the change: same command 55 passed. `test_a_graph_db_rewritten_by_another_process_is_a_cache_miss` (`os.utime` forward) and `test_a_deleted_graph_db_is_a_cache_miss` both assert `cached is False` and that the engine was asked twice.
- `tests/test_reindexer.py` new tests, all passing: `test_an_unchanged_git_tree_submits_nothing_and_flags_nothing` (second call returns False, no extra pass, gateway answer has no `reindexing` key), `test_an_edited_file_submits_a_pass_and_returns_true`, `test_a_new_untracked_file_submits_a_pass`, `test_do_reindex_runs_the_graph_pass_before_the_semantic_pass`, `test_a_git_failure_in_the_fingerprint_submits_no_more_than_the_debounce[timeout|nonzero]` (in-flight cleared between the calls so only the debounce limits it; submitted == [root]), `test_the_fingerprint_git_calls_are_bounded_and_shell_free`. On main these failed (None returned / order / no `subprocess` in the module).
- `tests/test_cache.py::test_a_different_freshness_string_misses` passes.
- `CODEBASE_MEMORY_CACHE_DIR`: `test_gateway_freshness.py`'s `graph_cache` fixture deletes `CODEBASE_MEMORY_HOME` (it wins over CACHE_DIR in `graph_cache_dir`) and sets `CODEBASE_MEMORY_CACHE_DIR` to `tmp_path/graph-cache`; every test there uses it. The reindexer tests stub both passes and never stat a graph db.
- `tests/test_reindex_lock.py`: `git diff 03957a6 -- tests/test_reindex_lock.py` shows exactly the two `ran == [...]` lines changed; passes.
- Check block: `.venv/bin/pytest tests/test_gateway_freshness.py tests/test_reindexer.py tests/test_reindex_lock.py tests/test_cache.py tests/test_gateway.py tests/test_hardening.py tests/test_index_failure_reason.py tests/test_rbac.py tests/test_integration.py tests/test_missing_target.py tests/test_engine_adoption.py tests/test_cli_commands.py -q -o addopts=` -> `320 passed`, exit 0. Also every test file mentioning Gateway/Reindexer/code_query_handler/cached (35 files) -> 918 passed, 30 skipped, 1 xfailed. "All existing tests still pass" for the whole suite: left to the conductor's gate. Changed test files are all on Touches (`git diff --stat 03957a6 -- tests`: test_cache, test_gateway_freshness, test_reindex_lock, test_reindexer).
- Also: `ruff check src tests bench` all passed; `mypy` no issues in 87 files; compileall OK; `tests/test_architecture.py` 6 passed. Acceptance `test_tf_freshness.py` 14 passed (was 9 failing before 02/03); `test_tf_semantic_failure.py`'s graph-before-semantic test passes, its 6 failures are ticket 10's.
- `codeintel c4 . --check --out /tmp/codeintel-c4-check` prints `c4 failed: engine-unavailable` both with my change and with it stashed (the graph backend is unavailable in this container right now), so it is environmental and not from this ticket.

**Decisions**
- Non-existent root: `maybe_reindex` returns False without submitting (the ticket's reason for `result is False`). That forced five existing tests in `tests/test_reindexer.py` that called `maybe_reindex` on the non-existent `/tmp/test` or `/repo` and asserted a pass ran to use `tmp_path` instead; their assertions are unchanged. `test_disabled_via_env` keeps `/tmp/test` (it asserts nothing ran).
- The time debounce still applies to git roots too (fingerprint changed AND window elapsed), so the fingerprint can never submit more than before. A changed tree inside the window is not recorded and is picked up by the next query after it.
- The first query for a root in a process has no recorded fingerprint, so it submits and is flagged `reindexing` (as before; the acceptance test lets the first query settle). A non-git root keeps the time debounce, and its submit also returns True and flags the answer, since nothing can tell whether its tree changed.
- The fingerprint also hashes the listed path names, so a deletion (no mtime) or a file whose mtime is older still moves it. Spec said HEAD + max mtime; this only adds detection, still bounded by the debounce.
- Semantic half reads the db AND its `-wal` (max mtime), not one stat: `SemanticDb` sets `journal_mode=WAL`, so a pass's writes land in `-wal` until a checkpoint and a single stat of the db would miss them. The model comes from `load_config(root)` as the reindexer/provider do.
- Empty `project_root`: the stamps are `0:0` without touching the filesystem; resolving `""` read the process cwd, which `tests/test_rbac.py::test_the_http_transport_never_answers_from_the_servers_own_cwd` forbids (it caught this).
- git hygiene for the fingerprint reuses `changed_range._GIT` (`core.fsmonitor=false`) and `_env()` so a hostile repo config cannot run a program on every query.

**Shortcuts taken**
- `_semantic_db_stamp` calls `load_config(root)` on every non-empty-root query (two small TOML reads). Worth caching per root if query latency ever shows it.
- A semantic search that indexes inline writes the semantic db, so the next identical query is a cache miss. Correct (the index moved) but costs caching for semantic answers on a churning repo.
- The fingerprint stats every modified/untracked file on each check past the debounce; a repo with tens of thousands of un-ignored untracked files would pay for that. Bounded by the 5 s git timeouts for the listing itself, not for the stats.

**For later tickets**
- `Reindexer.maybe_reindex` returns `bool`; fakes returning None are read as "no pass". Tests driving the real one on a path must use an existing directory.
- `_do_reindex` order is now graph then semantic (ticket 10 builds on that).
- Docs outside Touches still describe the old cache rule / REINDEX=off behaviour: `docs/architecture.md` (~151, ~161), `docs/query-flow.md` (~36, ~90-92), `docs/semantic.md` (~61), `docs/deploy.md` (~27). Likely ticket 13.
