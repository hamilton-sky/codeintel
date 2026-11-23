# Key the gateway cache on the index files, and report `reindexing` only for a real change

Type: task
Status: open
Blocked by: 01, 14
Test first: yes
Level: 3
Builder: backend
Touches: src/codeintel/gateway/gateway.py, src/codeintel/gateway/cache.py, src/codeintel/gateway/reindexer.py, README.md, tests/test_cache.py, tests/test_reindexer.py, tests/test_reindex_lock.py, tests/test_gateway.py, tests/test_gateway_freshness.py, src/codeintel/c4/check.py, src/codeintel/c4/model.py, docs/c4.md, tests/test_c4.py, tests/test_c4_check.py

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
- The c4 allowlist fix this ticket's gate needs (the Lint command's `codeintel c4 . --check`) is ticket 14. The c4 paths in Touches are there only because resuming this ticket after the repair counts ticket 14's commits (eb217d8, 474ee7f) in its range; this ticket does not change them, and its review covers them as ticket 14's code.

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

Work started at commit 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f. Test-only commit: e2e7d8e (round 2's failing tests: 2cdfcf0).

**Built**
- `src/codeintel/gateway/reindexer.py`: `_git_out` (argv list, `cwd=root`, `timeout=_FINGERPRINT_TIMEOUT_S` = 5, no shell, reusing `changed_range._GIT` with `core.fsmonitor=false` and its scrubbed `_env()`), `_tree_fingerprint(root)` = HEAD sha + newest `st_mtime_ns` + a sha256 over `(path, mtime_ns, size)` of every file `git ls-files -z -c -o --exclude-standard` lists (round 2: `-c` replaced `-m`, see Review fixes); `None` when git cannot answer. `maybe_reindex` now returns `bool`: False for disabled, empty or non-directory roots, in flight, inside the debounce window, an unchanged fingerprint, or `reindex = "never"`; True only when it submitted. `_do_reindex` runs `_graph_reindex` before `_semantic_reindex`, each in its own try; the `finally` is unchanged.
- `src/codeintel/gateway/cache.py`: `freshness: int | str` on `get`/`put`.
- `src/codeintel/gateway/gateway.py`: `_query` reads `reindex_pending` before `maybe_reindex`; `reindexing` = pending or `maybe_reindex(...)` returned True. Freshness = `f"{generation}:{graph_db_stamp(root)}:{_semantic_db_stamp(root)}"` (`"{generation}:0:0"` when no root). New `_semantic_db_stamp(root)`: max mtime_ns of the semantic db and its `-wal`, 0 when missing, never raises; the path comes from `_semantic_db_path(root)`, which remembers `default_db_path(load_config(root)["model"])` per root and re-reads it only when the global `config.toml` or the project `.codeintel.toml` stat changes.
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
- `_semantic_db_path` still stats the two config files on every query (no TOML parse). The memo `_SEMANTIC_DB_PATHS` is module-level, one entry per root ever queried; unbounded only in the number of distinct roots.
- A semantic search that indexes inline writes the semantic db, so the next identical query is a cache miss. Correct (the index moved) but costs caching for semantic answers on a churning repo.
- The fingerprint stats every tracked and untracked file, at most once per debounce window per root (round 2). On a 100k-file repo that is roughly 100k `stat` calls on the request thread once per 30 s; the 5 s git timeouts bound the listing, not the stats. Worth moving off the request thread if that latency shows up.

**For later tickets**
- `Reindexer.maybe_reindex` returns `bool`; fakes returning None are read as "no pass". Tests driving the real one on a path must use an existing directory.
- `_do_reindex` order is now graph then semantic (ticket 10 builds on that).
- Docs outside Touches still describe the old cache rule / REINDEX=off behaviour: `docs/architecture.md` (~151, ~161), `docs/query-flow.md` (~36, ~90-92), `docs/semantic.md` (~61), `docs/deploy.md` (~27). Likely ticket 13.

**Review fixes** (round 1; failing tests committed alone in 2cdfcf0, all four seen failing first: staged edit returned False, the filter marker existed, 3 fingerprint checks instead of 1, 4 config reads instead of <= 1)
1. Clean filter run by `ls-files -m`: reproduced by hand in a scratch repo (a `filter.evil.clean` that touches a marker; a same-size edit; `git -c core.fsmonitor=false ls-files -m -o` created the marker, `ls-files -c -o` did not). Fix: the fingerprint now lists with `-z -c -o --exclude-standard` (index read and directory walk only, no content compare) and hashes each file's `(path, mtime_ns, size)`. Test `test_the_fingerprint_never_runs_the_repositorys_own_clean_filter` (hostile clean filter, same-size edit) passes.
2. Staged-only changes invisible: fixed by the same change, since every tracked file's stat is in the digest whether or not it is staged. Test `test_a_staged_edit_submits_a_pass` (edit + `git add` after a pass returns True) passes. I did NOT fold in `.git/index`'s mtime as the finding suggested: the graph and semantic passes index the work tree, which the per-file stats already cover; the index file is rewritten by a plain `git status` (stat refresh) or by any git call the backend makes during its pass, which would submit an extra pass with no source change. `git restore --staged --worktree` rewrites the file, so its mtime moves the digest.
3. Unchanged fingerprint did not advance the window: `maybe_reindex` now sets `_last_fired[root] = now` when it checked and found the tree unchanged, so git runs at most once per debounce window per root. Test `test_an_unchanged_tree_is_fingerprinted_at_most_once_per_debounce_window` (three calls past the window, one fingerprint) passes.
4. `load_config` on every query: `_query` loads no config itself (grep: only `_semantic_db_stamp` called `load_config` in gateway.py), so there was nothing to reuse; instead `_semantic_db_path` remembers the path per root keyed on the stats of the two config files. Test `test_repeated_queries_do_not_reread_the_config` (4 queries, <= 1 read) and `test_a_model_change_in_the_project_config_is_followed` pass. Cross-project invalidation is now documented in `_semantic_db_stamp`'s docstring and in README's cache bullet.
Round 2 proof: check block `325 passed`, exit 0; `ruff check src tests bench` all passed; `mypy` no issues in 87 files; compileall OK; acceptance `test_tf_freshness.py` 14 passed. Every test file mentioning Gateway/Reindexer/code_query_handler/cached: 1116 passed, 29 skipped, 1 xfailed, 1 failed (`test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read`, the root-only failure commands.md's Test command deselects). Whole Test command: left to the conductor's gate.

**Round 3 (re-handout after ticket 14)**: started at 474ee7fd40a414b331c0d1db096931d63726c087. No code changed: the only failure in round 2 was the gate's Lint `codeintel c4 . --check` (an engines -> transports layer violation), which ticket 14 (474ee7f) fixed. Re-proved on current HEAD:
- Check block: `.venv/bin/pytest tests/test_gateway_freshness.py ... tests/test_cli_commands.py -q -o addopts=` -> `325 passed`, exit 0.
- Done when tests, each PASSED (`-rA`): test_gateway_freshness.py db-touch / db-delete / semantic-db miss; test_reindexer.py unchanged-tree, edited-file, graph-before-semantic, git-failure[timeout|nonzero], `test_never_raises_on_bad_path`; test_cache.py `test_a_different_freshness_string_misses`; test_reindex_lock.py both order tests. `git diff 03957a6 -- tests/test_reindex_lock.py` shows only the two `ran ==` lines. `CODEBASE_MEMORY_CACHE_DIR` set to tmp_path in test_gateway_freshness.py (lines 42-43).
- Lint: `ruff check src tests bench` all passed; `codeintel c4 . --check --out /tmp/codeintel-c4-check` -> `0 gating finding(s) — exit 0`.
- Whole Test command: left to the conductor's gate.

**Round 4 (footprint finding)**: started at a48f90fc11a1e8fdb04e9c5520091bee3d721c03. No code changed.
- Review fixes (round 1, footprint check): the flagged paths (docs/c4.md, src/codeintel/c4/check.py, src/codeintel/c4/model.py, tests/test_c4.py, tests/test_c4_check.py) are ticket 14's commits eb217d8 and 474ee7f, which the round-3 resume folded into this ticket's range; this ticket never edited them. The plan repair a48f90f added them to Touches with a note under "Not in this ticket", so the finding is answered by the plan, not by a code change. I did not change or revert any c4 code. `git diff --name-only 03957a6 HEAD -- src tests README.md docs` now lists only paths on Touches (README.md, docs/c4.md, the two c4 src files, the three gateway src files, test_c4, test_c4_check, test_cache, test_gateway_freshness, test_reindex_lock, test_reindexer).
- Check block re-run on a48f90f: `.venv/bin/pytest tests/test_gateway_freshness.py ... tests/test_cli_commands.py -q -o addopts=` -> `325 passed`, exit 0.
- Lint: `ruff check src tests bench` -> All checks passed; `codeintel c4 . --check --out /tmp/codeintel-c4-check` -> `0 gating finding(s) — exit 0`.
- Whole Test command: left to the conductor's gate.

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

## Review findings (round 2, gate (full))

gate: output in /home/user/codeintel/.feature-flow/state/gate-trust-fixes.log
gate: Build: .venv/bin/python -m compileall -q src/codeintel
gate: Test: .venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
gate: Lint: .venv/bin/ruff check src tests bench && .venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check
gate: Lint failed after 1s. the last lines of its output:
All checks passed!
codeintel c4 --check — layer report
  project: codeintel      depth 3 (auto-fit)      mode: declared
  layers:   cli > transports > tools > gateway > engines > core
  coverage: 10 of 21 elements assigned; 11 unassigned

VIOLATIONS (1 gating, 0 allowlisted)
  layer-order  src.codeintel.engines -> src.codeintel.transports    engines imports transports, which is above it
      src/codeintel/engines/ -> src/codeintel/transports/  (x10)

CYCLES (1)
  import-cycle  3 elements: src.codeintel.engines, src.codeintel.gateway, src.codeintel.transports

UNASSIGNED (11) — never gating
  layer-unassigned  bench.agent_bench matches no declared layer
  layer-unassigned  bench.agent_tools matches no declared layer
  layer-unassigned  bench.graphify_arm matches no declared layer
  layer-unassigned  bench.oracle_py matches no declared layer
  layer-unassigned  bench.oracle_ts matches no declared layer
  layer-unassigned  bench.questions matches no declared layer
  layer-unassigned  bench.run matches no declared layer
  layer-unassigned  bench.score matches no declared layer
  layer-unassigned  plans.trust_fixes.acceptance matches no declared layer
  layer-unassigned  scripts.check_release_consistency matches no declared layer
  layer-unassigned  scripts.release_canary matches no declared layer

STALE ALLOWLIST (1) — never gating
  stale-allow  allowlist entry 0 (src/codeintel/engines/graph/** -> src/codeintel/transports/metrics.py) matches no current violation — the exception can be deleted

ADVISORY (12 — never gating)
  12 CALLS/USAGE-only edge(s) point up the stack. These are NOT evidence of a
  dependency (see docs/layers-design.md §1). Use --json for the list.

2 gating finding(s) — exit 2

## Review findings (round 1, footprint check)

the build changed paths outside the ticket's `Touches:` line (src/codeintel/gateway/gateway.py src/codeintel/gateway/cache.py src/codeintel/gateway/reindexer.py README.md tests/test_cache.py tests/test_reindexer.py tests/test_reindex_lock.py tests/test_gateway.py tests/test_gateway_freshness.py): docs/c4.md, src/codeintel/c4/check.py, src/codeintel/c4/model.py, tests/test_c4.py, tests/test_c4_check.py. tickets run side by side because their Touches do not overlap, so change only what Touches names. if the ticket cannot be done inside it, say so in the Answer and stop: the plan has to change, not the footprint. one file the ticket truly needs (a test that pins the old behaviour, a generator) may be named in the Answer as `Extra file: <path> - <why>`, when it is the only one

## Review findings (round 1, pair review)

Range reviewed: 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f..0bc8714 (END = HEAD). It includes ticket 14's c4 commits (eb217d8, 474ee7f).

QUALITY
1. major src/codeintel/gateway/reindexer.py: a pass that never runs, or that fails, is never retried while the tree stays the same. `maybe_reindex` records `_last_fingerprint[root]` when it submits a pass. If `_do_reindex` then gives up because another process holds the lock, or both engines raise, the recorded fingerprint still matches the tree, so every later query returns False and submits nothing. Reproduced: edit a file, hold the reindex lock, call `maybe_reindex` (True, pass skipped), release the lock, call it three more times: all return False, graph pass ran once in total. `_do_reindex`'s docstring still says "A persistent failure retries every debounce window". Fix: in `_do_reindex`, on the lock-skip path and when a pass raises, clear `_last_fingerprint[root]` under `self._lock`, or record the fingerprint only after a pass completes. Add a test for a lock-skipped pass followed by an unchanged tree.
2. minor src/codeintel/gateway/reindexer.py: in `_tree_fingerprint`, the 5s timeout bounds only the git calls; the `os.stat` loop over every listed file and the unbounded `ls-files -o` output still run on the request thread once per debounce window. Fix: cap the number of listed entries (on overflow, fingerprint None and fall back to the debounce), or move the check off the request thread.
3. minor src/codeintel/gateway/gateway.py: `_SEMANTIC_DB_PATHS`, like `Reindexer._last_fingerprint`, grows by one entry per distinct project_root and never shrinks. Fix: bound it (a small LRU), or key it on the realpath of roots that exist.
4. minor src/codeintel/c4/check.py: when a truncated cycle has no allowed relation inside its listed members, `_allowed_cycle_breaker` falls back to "the first allowed relation", and the `ALLOWED:` reason it prints may belong to an unrelated entry. Fix: when no entry lies inside the cycle, report the cycle as allowed without naming a specific entry's reason, or keep it gating.

codeintel: changed 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f: wrong: it listed unrelated `.get` callers (Searcher._rerank, c4.model functions, bench modules) as `ContentHashCache.get` callers. Its `maybe_reindex` entry was right.

REVIEW: FAIL

Range reviewed: 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f..0bc8714 (END = HEAD), including ticket 14's eb217d8 and 474ee7f.

SPEC
- New tests/test_gateway_freshness.py fails on main and passes here (utime-forward miss; deleted-db miss): MET. 60 passed across freshness/reindexer/cache/lock tests; e2e7d8e failed before the code.
- tests/test_reindexer.py gains the 4 tests (unchanged tree False/no submit/no flag; edit True; graph before semantic; git timeout or nonzero submits once inside the debounce): MET.
- tests/test_cache.py, a different freshness string misses: MET.
- Tests needing a graph db point CODEBASE_MEMORY_CACHE_DIR at tmp_path: MET.
- tests/test_reindex_lock.py passes with only the two order lists changed: MET.
- Check-block pytest command passes: MET. All existing tests still pass: MET (2221 passed, 31 skipped, 1 deselected, 1 xfailed). Only test files on Touches change: MET.
- Ticket 14 Done when, c4 tests: MET (243 passed, 1 xfailed).
- Ticket 14 Done when, `codeintel c4 . --check --out /tmp/codeintel-c4-check`: MET (exit 0, 0 gating findings, 1 allowlisted, no stale allowlist).
- Ticket 14 Done when, .codeintel.toml unchanged: MET.
- scope: OK. Note: map.md ends with a stray unbulleted "03 — ..." line under Open questions.
- test first: OK. e2e7d8e and eb217d8 add the failing tests before the code.

Accepted deviation: the fingerprint hashes (path, mtime_ns, size) of `git ls-files -c -o --exclude-standard` instead of `-m`, as round 1 asked, so git never runs the repo's clean filter.

codeintel: changed 03957a6b7daa3f224aaee3fd3cbfbf41ebe2262f: wrong: it listed many unrelated `.get` callers (Searcher.search, c4.model functions) as ContentHashCache.get callers.

REVIEW: PASS
