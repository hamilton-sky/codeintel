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
