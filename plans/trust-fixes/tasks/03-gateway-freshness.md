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

## Review findings (round 1, pair review)

Reviewed trust-fixes 03 (quality pass, level 3), range 03957a6b7daa..0b5c97c5cae4. That is also HEAD, so no later commits were left out. I read the ticket as it stood at the base commit, cut off at the Answer heading. I did not read the Answer. I re-ran the Done when pytest command: 320 passed. ruff on the touched files is clean, and mypy reports no issues. git status is clean, and the run left no files behind.

Inputs I probed as a hostile caller:
- project_root: it becomes git's cwd and also feeds graph_db_stamp and load_config.
- The repository's own .git/config and .gitattributes.
- Untracked and modified file names that are not valid UTF-8.
- git failing, timing out or missing.
- Two queries racing in maybe_reindex.
- A root that does not exist.

QUALITY
1. major src/codeintel/gateway/reindexer.py: `_tree_fingerprint` runs `git ls-files -m`. That command compares content for any tracked file whose stat changed but whose size did not, and the comparison runs the repository's own `filter.<driver>.clean`/`process` command (set in .git/config and switched on by .gitattributes). `-c core.fsmonitor=false` does not cover this. changed_range.py's docstring says a repository is untrusted input, which is why fsmonitor was turned off. Before this change only op=changed ran git. Now every server-mode query of any op, against any project_root, runs it. This is from my reading of git's ce_modified path; I did not run it, because I may not write files. Fix: drop `-m`. Take the fingerprint from the names in `git ls-files -z -c -o --exclude-standard` plus their stat mtimes, which needs no content comparison, plus the mtime of the git index file (`git rev-parse --git-path index`). If not, turn off attribute-driven filters some other way and add a test with a hostile clean filter.
2. major src/codeintel/gateway/reindexer.py: changes that are only staged are invisible. `-m` lists files where the worktree differs from the index, so starting from a clean tree, `edit + git add` leaves HEAD, the listing and the newest mtime unchanged. The fingerprint stays the same and no pass is ever submitted. `git restore --staged --worktree` after a pass behaves the same way. Before this change the time debounce refreshed within about 30s; now the index stays stale until some other change moves the fingerprint. Semantic search has no stale-index stamp to warn about it. Fix: fold the git index file's mtime into the fingerprint (the same change as finding 1), and add a test for edit + `git add` after a pass. If the graph backend rewrites .git/index during its own pass, check that this does not cause one extra pass every time.
3. minor src/codeintel/gateway/reindexer.py: when the fingerprint is unchanged, `_last_fired` is not advanced. Once the debounce window has passed, every query on an unchanged tree forks two git processes on the request thread, including a walk of untracked files, and each can take up to 5s. Fix: record when the fingerprint was last checked and skip checking again inside the debounce window.
4. minor src/codeintel/gateway/gateway.py: `_semantic_db_stamp` calls `load_config` (the global and project TOML) again on every query. The semantic db is one file per model for the whole machine, so indexing any project clears every project's cached answers. That errs on the safe side. Fix: reuse the config `_query` already loads, if there is one, and document that cached answers are invalidated across projects.

codeintel: changed 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f: wrong: it listed unrelated `.get` callers (Console._detect_color, bench._env_path) as ContentHashCache.get callers. The maybe_reindex callers it listed were right.

REVIEW: FAIL

Range reviewed: 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f..0b5c97c (END = HEAD, so no later commits were left out). I read the ticket at the base commit, cut off at the Answer heading. The Answer never reached me. The ticket's Status hunk goes from open to resolved.

SPEC
- New tests/test_gateway_freshness.py fails on main and passes here (utime-forward miss; deleted-db miss): MET. I ran `pytest tests/test_gateway_freshness.py ... -o addopts=` and all 4 passed. It sets CODEINTEL_REINDEX=off, so at base the key is the generation alone and never moves. That means `cached` stays true and the `cached is False` assertions fail on main, which matches the conductor seeing e2e7d8e fail.
- tests/test_reindexer.py gains the 4 tests (unchanged tree returns False and has no `reindexing` flag; an edit returns True and submits; graph runs before semantic; a git failure or timeout submits once inside the debounce): MET. All are present: test_an_unchanged_git_tree_submits_nothing_and_flags_nothing, test_an_edited_file_submits_a_pass_and_returns_true, test_do_reindex_runs_the_graph_pass_before_the_semantic_pass, and test_a_git_failure_in_the_fingerprint_submits_no_more_than_the_debounce[timeout|nonzero]. They pass.
- tests/test_cache.py: a different freshness string misses: MET. test_a_different_freshness_string_misses passes.
- Tests that need a graph db file point CODEBASE_MEMORY_CACHE_DIR at tmp_path: MET. The graph_cache fixture in test_gateway_freshness.py sets it to tmp_path and also deletes CODEBASE_MEMORY_HOME, which would otherwise take precedence. The gateway test in test_reindexer.py needs no db file. CODEINTEL_HOME is isolated by conftest.
- tests/test_reindex_lock.py passes with only the two order lists changed: MET. The diff is exactly the two `ran ==` lines, and the file passes (55 passed across the 4 files above).
- The check block pytest command passes, all existing tests still pass, and only test files on Touches change: MET (conductor) for the check block. I also ran the full `pytest tests -q -o addopts=`: 2212 passed, 31 skipped, 1 xfailed, 1 failed. The failure is tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read (`runnable` is None). It comes from SemanticProvider.probe's `_deep_answer`, and that code is untouched in this range. It reads None when the embedding model is not cached on this host, so it is an environment issue and not caused by this change. The changed test files (test_cache, test_gateway_freshness, test_reindex_lock, test_reindexer) are all on Touches. Acceptance plans/trust-fixes/acceptance/test_tf_freshness.py (which includes this ticket's cache-miss and reindexing-flag tests): 14 passed.
- scope: OK. src gateway/cache.py, gateway.py, reindexer.py and README.md are all on Touches. plans/trust-fixes/map.md and learnings.md are flow bookkeeping. Nothing touches ticket 10's semantic failure record or the stale-index work from tickets 01 and 02.
- test first: OK. e2e7d8e adds the tests before the code. The freshness tests and the True/False return assertions cannot pass against the base gateway and reindexer.

codeintel: callers maybe_reindex: helped

REVIEW: PASS
