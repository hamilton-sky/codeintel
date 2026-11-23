# Make `changed` check index age in every mode, against HEAD's commit time, with no slack

Type: task
Status: open
Blocked by: —
Test first: yes
Level: 3
Builder: backend
Floor: allow config
Touches: src/codeintel/core/paths.py, src/codeintel/engines/graph/changed_range.py, src/codeintel/engines/graph/ops.py, src/codeintel/setup/reset.py, tests/conftest.py, tests/test_changed_range.py, tests/test_null_reason_and_ripple.py, tests/test_graph_real.py, tests/test_graph_failure_population.py, tests/test_index_age.py

Fix T2's `changed` half. It covers five findings, and the Repro and Expected for each are in `.feature-flow/state/redblue-findings.md`:

- `uncommitted-changed-no-staleness`
- `nongit-uncommitted-claims-clean`
- `index-age-misses-out-of-diff-commits`
- `index-age-2s-slack`
- `callers-callees-stale-complete` (only its `changed` side; callers/callees/impact are ticket 02)

1. **One place that finds the graph db.**
   - Move the slug function `_graph_slug` from `src/codeintel/setup/reset.py` into `src/codeintel/core/paths.py` as `graph_slug(canonical_root)`. Keep `setup.reset._graph_slug` importable (`tests/test_reset.py` uses it).
   - Add `graph_db_stamp(project_root) -> int`: the max `st_mtime_ns` of `<graph_cache_dir()>/<graph_slug(realpath root)>.db` and its `-wal`. When neither exists, use the newest `*.db`/`*.db-wal` in that directory, so a repo answered from an ancestor project still gets a stamp. Return 0 when the directory is missing.
   - Add `graph_db_mtime(project) -> float | None`, which reads the db for a resolved project id.
   - `graph_index_age` in `src/codeintel/engines/graph/changed_range.py` reads the db time through that helper rather than building the path itself. Ticket 03's cache key uses the same stamp.
2. **HEAD commit time.** In `graph_index_age`, also read `_git(root, "log", "-1", "--format=%ct")`. A commit newer than the db makes the result `stale` and names "HEAD (a commit)". Keep `_last_tree_move` for checkouts and rewrite its docstring: a commit does not move the tree, but it does prove files were written after an earlier index pass.
3. **No slack.** Replace `if written + 2.0 < newest` with `if written < newest`.
4. **Uncommitted `changed`.** In `GraphOps._op_changed`, when there is no target:
   - First run `_git(root, "rev-parse", "--is-inside-work-tree")`, through one small helper in `changed_range.py` (for example `in_work_tree(root) -> bool`) so a test can replace that one check. If it fails, set `self._pending_null = ("not-a-git-repo", ...)` with the same hint range mode gives, and return `None`.
   - Otherwise, before the `detect_changes` call (read the age BEFORE the backend opens the db, as `changed.py` explains near its `graph_index_age` call), compute `graph_index_age(project, root, changed + untracked paths)`. Get the paths from `git status --porcelain` through `_git`.
   - Then add `stale-index` or `index-age-unknown` with `_add_gap("changed", ...)`, worded like range mode's gaps in `changed.py`.
   - An untracked source file whose path has 0 symbols in the answer is `stale` even when its mtime is older.
   - "working tree clean" is printed only inside a git work tree.

5. **Keep the suite honest about the new check.** Most graph tests use the monkeypatched `_run` fake, with no real graph db file and often no git repo, so the new check would turn their answers into `index-age-unknown` or `not-a-git-repo`.
   - Add an autouse fixture to `tests/conftest.py` that makes `graph_index_age` report `fresh`. `src/codeintel/engines/graph/changed.py` imports the function by name (`from .changed_range import ... graph_index_age`), so the stub patches that binding as well as `changed_range.graph_index_age`. Code added later calls it as `changed_range.graph_index_age(...)`, so this one stub reaches it.
   - Add an opt-in fixture `real_index_age` that restores the real function. `tests/test_index_age.py` (new) and every existing test that pins `stale-index` or `index-age-unknown` request it.
   - The `Floor: allow config` line exists for this `conftest.py` change and nothing else.
   - Uncommitted-mode tests in `tests/test_null_reason_and_ripple.py` (`build_result("changed", "", …, ROOT)` on a path that is not a git repo) monkeypatch the work-tree helper to True and keep their assertions, since they test rendering, not the git check.
   - `tests/test_graph_failure_population.py::test_no_graph_op_reports_a_backend_failure_as_an_absence` calls `build_result("changed", "", [], 30000, "/tmp/x")` with a failing backend and asserts the reason is `backend-error` or `timeout`. `/tmp/x` is not a git work tree, so the new check would answer `not-a-git-repo` before the backend is asked. The test is about the backend-failure population, so it monkeypatches the work-tree helper to True for the loop and keeps every assertion unchanged.
   - `tests/test_graph_real.py`'s uncommitted `changed` tests (`test_changed_renders_files_and_impacted_symbols`, `test_changed_clean_tree_is_informative_not_null`, `test_changed_dedupes_files_and_drops_module_markers`, `test_changed_drops_root_level_file_markers`, `test_changed_keeps_symbol_with_slash_in_qualified_name`, `test_changed_payload_has_no_target_and_raises_timeout_floor`, `test_changed_drops_non_source_files_and_the_tools_own_artifact`, `test_changed_drops_ds_store`, `test_changed_counts_only_source_when_mixed`, and the malformed/backend-failure ones) run on `ROOT = "/Users/x/Documents/project/codeintel"`, which does not exist and is not a git repo. They test rendering, not the git check, so they monkeypatch the work-tree helper to True and keep their current assertions ("working tree clean", "Changes impact (…"). The conftest stub covers only `graph_index_age`, so this is needed on its own.
6. **Why this ticket is level 3.** It moves `_graph_slug`, which `src/codeintel/setup/reset.py` uses to pick the graph db file it deletes, and it adds an autouse fixture that changes what the whole suite sees. Two guards:
   - `setup.reset` keeps deleting exactly the same files: `tests/test_reset.py::test_graph_slug_matches_the_backends_real_file_names` and the test after it pass unchanged.
   - The autouse stub must never hide a staleness test: every existing test that asserts `stale-index` or `index-age-unknown` (today all in `tests/test_changed_range.py`) requests `real_index_age`, and keeps its assertion unchanged.

## Not in this ticket

- `stale-index` on callers/callees/impact answers is ticket 02.
- The gateway cache key is ticket 03.
- `status`/`doctor` showing the graph age is not planned (see map.md Open questions).

## Done when

- New `tests/test_index_age.py` reproduces each finding with a real `git init` repo in `tmp_path`, a fake graph db file whose mtime is set with `os.utime`, and the monkeypatched `_run` fake. Each test fails on main eb0902f:
  - an uncommitted edit with no reindex gives `stale-index` and partial
  - an untracked file gives `stale-index`
  - a non-git dir gives reason `not-a-git-repo`
  - a commit after the index gives `stale-index`
  - an edit 0.1 s after the index gives `stale-index`
- `tests/test_changed_range.py`'s uncommitted-mode tests are updated: `test_an_empty_target_is_still_the_original_uncommitted_op` (which today fails on any subprocess) now allows the `git` calls of the work-tree check and `git status`, and the tests still pin "working tree clean" for a clean git tree. `tests/test_graph_failure_population.py` passes with its assertions unchanged. `.venv/bin/pytest tests/test_index_age.py tests/test_changed_range.py tests/test_reset.py tests/test_graph_real.py tests/test_null_reason_and_ripple.py tests/test_graph_failure_population.py tests/test_dispatch_callers.py tests/test_mcp_answer_size.py tests/test_gateway.py -q -o addopts=` passes.
- `tests/test_graph_real.py` passes, with its uncommitted `changed` assertions unchanged.
- `tests/test_reset.py`'s slug tests pass unchanged, and `grep -rn "stale-index\|index-age-unknown" tests/` finds no assertion in a test that does not request `real_index_age` (the reviewer checks this).
- All existing tests still pass.

```check
# The conductor runs this block before review.
$ .venv/bin/pytest tests/test_index_age.py tests/test_changed_range.py tests/test_reset.py tests/test_graph_real.py tests/test_null_reason_and_ripple.py tests/test_graph_failure_population.py tests/test_dispatch_callers.py tests/test_mcp_answer_size.py tests/test_gateway.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "An agent working right after an edit", and Decisions ("The index age check", "The `changed` (no target) git check")
- redblue-fixplan.md § T2
- src/codeintel/engines/graph/changed.py: the range path's `graph_index_age` call and its gap wording (read-only here)

## Answer

Work started at commit 23e7ab74dde64b646c0a861ff8bb6c0d210a5234. Test-only commit: 7912bed.

**Built**
- `src/codeintel/core/paths.py`: `graph_slug(canonical_root)` (moved from `setup/reset.py`), `graph_db_stamp(project_root) -> int` (max `st_mtime_ns` of `<slug>.db`/`-wal`; else the newest `*.db`/`*.db-wal` in the cache dir; 0 when the dir is missing), `graph_db_mtime(project) -> float | None`.
- `src/codeintel/setup/reset.py`: `_graph_slug` is now `from codeintel.core.paths import graph_slug as _graph_slug`; the unused `import re` went with it.
- `src/codeintel/engines/graph/changed_range.py`: `graph_index_age` reads the db time through `graph_db_mtime`, also dates HEAD with `_git(root, "log", "-1", "--format=%ct")` ("HEAD (a commit)"), compares `written < newest` (no slack). `_last_tree_move` docstring rewritten. New `in_work_tree(root) -> bool` and `uncommitted_paths(root) -> (changed, untracked)` (absolute paths from `git rev-parse --show-toplevel` + `git status --porcelain=v1 -z --untracked-files=all`).
- `src/codeintel/engines/graph/ops.py`: `_op_changed` with no target refuses `not-a-git-repo` outside a work tree; otherwise reads `changed_range.graph_index_age(project, root, changed + untracked)` before `detect_changes`, and `_uncommitted_age_gap` adds `stale-index` / `index-age-unknown` (section `changed`) on an answer that has source files or symbols. An untracked source file with no symbol in the answer is `stale-index` regardless of mtime.
- `tests/conftest.py`: autouse `_index_age_is_fresh` stub (patches `changed_range.graph_index_age` and `changed.graph_index_age`) and opt-in `real_index_age`.
- `tests/test_index_age.py` (new), `tests/test_changed_range.py` (6 staleness tests request `real_index_age`; `test_an_empty_target_is_still_the_original_uncommitted_op` now allows only git `rev-parse --is-inside-work-tree|--show-toplevel` and `status`), `tests/test_graph_real.py` and `tests/test_null_reason_and_ripple.py` (an `in_a_work_tree` fixture on the uncommitted `changed` tests), `tests/test_graph_failure_population.py` (patches `in_work_tree` to True; assertions unchanged).

**Proof**
- New tests fail on main: at the test-only commit 7912bed (src identical to eb0902f, checked with `git diff eb0902f 23e7ab7 -- src tests`, empty), `.venv/bin/pytest tests/test_index_age.py tests/test_changed_range.py -q -o addopts=` gave 11 failed, 99 passed. The five finding tests failed on assertions: uncommitted edit `'stale-index' in []`; untracked file `'stale-index' in []`; non-git `None == 'not-a-git-repo'` (result was "working tree clean"); commit after index `'stale-index' in []`; 0.1 s edit `'stale-index' in []`. The paths-helper tests failed with ImportError (helpers did not exist).
- Check block: `.venv/bin/pytest tests/test_index_age.py tests/test_changed_range.py tests/test_reset.py tests/test_graph_real.py tests/test_null_reason_and_ripple.py tests/test_graph_failure_population.py tests/test_dispatch_callers.py tests/test_mcp_answer_size.py tests/test_gateway.py -q -o addopts=` -> `351 passed, 1 skipped` (the skip is `test_reset.py:706`, root-only, pre-existing).
- `test_graph_real.py` passes with only `@pytest.mark.usefixtures("in_a_work_tree")` added (git diff shows no assertion line changed). "working tree clean" still pinned for a clean git tree in `test_changed_range.py` and `test_index_age.py::test_a_clean_git_tree_still_says_working_tree_clean`.
- `tests/test_reset.py` is unchanged (not in `git status`) and passes, including `test_graph_slug_matches_the_backends_real_file_names` and the test after it. `grep -rn --include=*.py "stale-index\|index-age-unknown" tests/`: every assertion is in `test_index_age.py` (module-level `pytestmark = usefixtures("real_index_age")`) or in the six `test_changed_range.py` tests that now take `real_index_age`; the rest are docstrings/comments.
- All existing tests: the whole Test command is left to the conductor's gate. I also ran `tests/test_cold_process.py test_corpus.py test_graph_provider.py test_mapper.py test_mcp_server.py test_qualifier_scan.py test_cli_commands.py test_reindexer.py test_architecture.py test_summary_integrity.py` -> 342 passed, 28 skipped, 1 xfailed. `ruff check src tests bench` clean, `mypy` clean, `codeintel c4 . --check --out /tmp/codeintel-c4-check` 0 gating findings, compileall OK.
- Acceptance `plans/trust-fixes/acceptance/test_tf_freshness.py`: the five `changed` tests pass; the 9 that fail are callers/callees/impact and the MCP cache (tickets 02, 03).

**Decisions**
- The uncommitted age gap is added only when the answer has source files or symbols. A clean tree (or non-source-only changes) has no graph content to be stale, so it stays "working tree clean" with no gap, matching range mode's "only when a graph lookup answered".
- One `stale-index` gap at most: the untracked-file wording wins when it applies (it names the file), otherwise the age detail.
- `git status` paths are relative to the repo top level, not `root`, so `uncommitted_paths` joins them to `--show-toplevel` and returns absolute paths (`graph_index_age`'s `os.path.join(root, abs)` keeps them absolute).
- The `not-a-git-repo` hint mirrors range mode's wording, with "no uncommitted changes to report" instead of a ref.
- `graph_db_mtime` returns seconds from `st_mtime_ns / 1e9`, so both helpers read the same stat field.

**Shortcuts taken**
- `%ct` has 1 s resolution: a commit landing in the same second after the index is not caught by the commit check (its files' own mtimes still are, if they are in the diff). Worth upgrading only if index-then-commit within a second shows up in practice.
- Deleted files in the uncommitted path set are skipped by `graph_index_age` (it cannot stat them); a deletion alone with an old commit gives `fresh`. Ticket 02's "missing file" rule is the natural place if that matters.
- `in_work_tree("")` (no project root) answers `not-a-git-repo` rather than range mode's `no-project-root`.

**For later tickets**
- Ticket 02: use `changed_range.graph_index_age(...)` through the module (the conftest stub reaches it). Tests asserting `stale-index` must request `real_index_age`. Uncommitted `changed` tests on a fake `ROOT` need `changed_range.in_work_tree` patched to True.
- Ticket 03: `core.paths.graph_db_stamp(root)` is ready for the cache key.
- Docs (likely ticket 13, `docs/graph.md` is outside this ticket's Touches): `docs/graph.md` ~566 says "a plain commit does not count" for `stale-index`; that is now false (HEAD's commit time counts, no 2 s slack), and uncommitted `changed` now has `stale-index`/`index-age-unknown` and `not-a-git-repo`.
- `tests/test_changed_range.py::test_a_plain_commit_after_the_index_does_not_make_it_stale` still passes (its index is dated in the future, after the commit) but its name and docstring describe the old rule; I left them as they were.

## Review findings (round 1, pair review)

I'm failing this ticket on one major finding. When `git status` fails inside a git work tree, uncommitted `changed` reports the index as fresh, which is the false freshness this ticket exists to remove. I reviewed `23e7ab7..db461c1`. `db461c1` is HEAD, so no later commits were left out. I read the ticket at the base commit and the diff with the tasks folder excluded. The `## Answer` never reached me.

I ran the Done when command myself: 351 passed, 1 skipped. Every test that asserts `stale-index` or `index-age-unknown` requests `real_index_age`: the 6 in `tests/test_changed_range.py`, and all of `tests/test_index_age.py` through `pytestmark`. `setup.reset._graph_slug` can still be imported and its regex is unchanged, so reset deletes the same files.

Inputs probed as a hostile caller:
- root (missing, not a directory, not a repo, inside .git, bare repo): _git catches the OSError, in_work_tree returns False, answer is not-a-git-repo. No way through.
- The repo's git config: _GIT already forces core.fsmonitor=false on the new git status.
- project id in graph_db_mtime: only stats a file, same behaviour as old code.
- Graph cache dir and db files (permission errors): see finding 2.
- git status output (rename entries, size, failure): rename and copy entries parsed correctly. A failure leads to finding 1.

QUALITY
1. major /home/user/codeintel/src/codeintel/engines/graph/changed_range.py: `uncommitted_paths` returns `([], [])` whenever `git status` (or `--show-toplevel`) fails. Likeliest cause is the 30s `_GIT_TIMEOUT_S` on a large repo, made more likely by `--untracked-files=all`. `graph_index_age` then compares only HEAD's commit time and the reflog against the db, and returns `fresh` whenever those are older. So the uncommitted answer stays `complete` with no gap, even with edits on disk. The untracked-file check also gets an empty list and stays silent. Reproduced: a repo with an edit newer than the db gives `stale`; with `git status` forced to fail, the same tree gives `IndexAge(state='fresh')`. Breaks the module's own rule that "cannot-tell is unknown, never fresh"; range mode already reports the matching failure (`untracked_error`). Fix: have `uncommitted_paths` report the failure (an error string or flag), have `_op_changed` emit `index-age-unknown` naming the failed git call, and add a test that makes `status` fail and asserts that gap.
2. minor /home/user/codeintel/src/codeintel/core/paths.py: `graph_db_stamp` documents "0 when unknown", but lets PermissionError and other OSErrors escape from `os.scandir(directory)` and from `_db_stamps_ns`, which catches only FileNotFoundError. Ticket 03 will call it from the gateway cache key, which must never raise (CONTRIBUTING never-raise rule). Fix: catch OSError and return 0, or the partial maximum.
3. minor /home/user/codeintel/tests/test_changed_range.py: `test_a_plain_commit_after_the_index_does_not_make_it_stale` keeps its name and docstring ("Counting it flagged every query after every commit"), but the code now counts a commit after the index as stale on purpose. It only passes because its db mtime is 600s in the future. Fix: rename it and update the docstring to say what it now pins (a commit older than the index is not stale), or delete it as superseded by tests/test_index_age.py.
4. minor /home/user/codeintel/src/codeintel/engines/graph/changed_range.py: `uncommitted_paths` lists changes from the whole repository, not just the directory the query ran from. In a subdirectory, an edit outside it still marks its index stale. Safe but noisy. Fix: filter to `root`, or say so in the gap text.

codeintel: changed 23e7ab74dde64b646c0a861ff8bb6c0d210a5234: helped (confirmed every remaining `_graph_slug` reference resolves through the re-export in reset.py)

REVIEW: FAIL

I checked ticket 01 (`changed` index age) against its own last commit, which is HEAD (db461c19). There were no later commits to leave out. The `## Answer` never reached me: I read the ticket at 23e7ab74 and viewed the diff with the ticket folder held back.

SPEC
- New tests/test_index_age.py, each finding test fails on main eb0902f: MET. Unpacked eb0902f's src with git archive into the scratchpad and ran the new test file against it with PYTHONPATH: 11 failed, 2 passed. All five finding tests fail on main: uncommitted edit (stale + partial), untracked file, non-git dir (not-a-git-repo), commit after the index, edit 0.1 s after. The 2 that pass are negative controls (edit older than the index; clean tree prints "working tree clean"). Real git init in tmp_path, db file set with os.utime, fake _run.
- test_changed_range uncommitted tests updated, test_graph_failure_population assertions unchanged, listed pytest command passes: MET (conductor). test_an_empty_target_is_still_the_original_uncommitted_op now allows only rev-parse --is-inside-work-tree / --show-toplevel and status, still pins "working tree clean". test_graph_failure_population only adds the in_work_tree monkeypatch.
- test_graph_real passes with uncommitted changed assertions unchanged: MET (adds only in_a_work_tree fixture and usefixtures decorators).
- test_reset slug tests unchanged, every stale-index/index-age-unknown assertion in a test requesting real_index_age: MET (test_changed_range.py 602, 812, 820, 828, 849, 1347; test_index_age.py via pytestmark).
- All existing tests still pass: MET. pytest tests -q -o addopts=: 2181 passed, 31 skipped, 1 xfailed, 1 failed (test_doctor root-only, fails the same on eb0902f). Acceptance test_tf_freshness.py: all 5 changed tests pass; 9 failing are tickets 02/03.
- scope: OK. Every changed source/test file is in Touches; others are plans/trust-fixes/learnings.md, map.md and the ticket file.
- test first: OK.

codeintel: none run

REVIEW: PASS
