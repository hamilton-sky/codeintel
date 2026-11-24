# Find aliased imports and same-named sibling methods beyond the answer's own files

Type: task
Status: open
Blocked by: 05, 06, 13
Test first: yes
Level: 3
Builder: backend
Touches: src/codeintel/engines/graph/blindspots.py, src/codeintel/engines/graph/dispatch.py, src/codeintel/engines/graph/answer.py, src/codeintel/engines/graph/confidence.py, src/codeintel/gateway/reindexer.py, tests/test_blindspot_gaps.py, tests/test_dispatch_callers.py, tests/test_edge_confidence.py, tests/test_reindexer.py, tests/fixtures/blindspots/

Ticket 13's end-to-end run left 5 acceptance tests failing, all in `plans/trust-fixes/acceptance/test_tf_safe_for_destructive.py`. A `callers` answer can still say `safe_for_destructive: true` in two cases the spec names.

1. **Aliased imports (3 tests):**
   - `test_callers_of_a_python_symbol_imported_as_an_alias_is_partial_with_aliased_import_gap`
   - `test_callers_of_a_ts_symbol_imported_as_an_alias_is_partial_with_aliased_import_gap`
   - `test_callers_of_a_ts_symbol_reexported_under_an_alias_is_partial_with_aliased_import_gap`

   Ticket 05's `aliased-import-unbound` rule only reads the answer's row files and the target's own file. When the file that imports the target under an alias has no caller row (the backend never bound the alias), the rule never sees it. The spec says "imported as `h` somewhere".
2. **Same-named siblings (2 tests):**
   - `test_callers_of_a_method_whose_sibling_has_a_name_matched_call_lists_it_and_has_dispatch_gap`
   - `test_callers_of_a_method_whose_protocol_member_has_a_name_matched_call_has_dispatch_gap`

   Ticket 06 finds siblings through the classes that define the method. The backend can return same-named `*.m` method nodes and a `suffix_match` edge with no class nodes, so no `total_area`/`totalArea` row and no `dispatch-sibling-unbound` gap appear.
3. **Reindexer debounce after boot:** `Reindexer._last_fired` defaults to `0.0` on the monotonic clock. On a host up for less than the debounce window, such as a fresh CI runner, the first reindex of a root is held back. `tests/test_reindexer.py::test_an_unchanged_tree_is_fingerprinted_at_most_once_per_debounce_window` fails on such a host.

## What to do

1. Aliased imports: look for an import of the target's module that binds the target under another name, or re-exports it under one. Search every source file of the target's language that imports the target's module, not only the answer's row files and the target's own file. Keep the search bounded: use the backend's import edges or a bounded text scan of files that name the module, with the existing per-file size caps. When one is found and no row comes from that file, add the `aliased-import-unbound` gap. That makes the answer partial and `safe_for_destructive` false. Keep ticket 05's existing behaviour and tests.
2. Siblings: when the backend returns other method nodes with the same short name (`*.m`), or a `suffix_match` edge, treat them as same-named siblings, with or without class nodes. List their name-matched callers as rows marked as such, and add the `dispatch-sibling-unbound` gap. Keep ticket 06's class-based path.
3. Debounce: make a root that has never fired count as outside the window, for example with `float("-inf")`. That way the first pass is never held back by host uptime.
4. Do not change the acceptance tests.

## Not in this ticket

- A spec change on whether a commit made after the index makes `callers` stale. Ticket 13's builder showed it is not the cause of these failures.
- CHANGELOG wording (I update it at PR time).

## Done when

- New unit tests in `tests/test_blindspot_gaps.py` / `tests/test_dispatch_callers.py` cover each case above with the acceptance fakes' shape: the alias in a file with no row (Python and TS, plus a TS re-export), and siblings with no class nodes (a plain sibling and a protocol member). Each fails before the change.
- A `tests/test_reindexer.py` test with the monotonic clock pinned near 0 fails before the change and passes after.
- All 5 named acceptance tests pass, and the rest of `test_tf_safe_for_destructive.py` still passes.
- All existing tests still pass.

```check
$ .venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_reindexer.py plans/trust-fixes/acceptance/test_tf_safe_for_destructive.py -q -o addopts=
prints passed
```

## Answer

Work started at 9d5d501c8ddad4e8d2c74b77b147a23fb35b9651. Test-only commit: bbbe8ec.

**Built**
- `src/codeintel/engines/graph/blindspots.py`: `files_aliasing(root, name, family)` returns an `AliasSearch(files, shortfall)`. It lists the files of the target's language (python or ts-js, by `render._LANG_FAMILIES`) that write `name as <identifier>`. It uses `git grep -l -I -E -z --untracked --exclude-standard` through `changed_range._git_capped` (30 s timeout, output capped at 200 kB). A root git does not manage (exit 128, or git missing) gets a bounded walk instead (`_walk_aliasing`). The walk skips dot-dirs, `node_modules`, `__pycache__`, `site-packages` and `venv`, reads through `qualifier_scan._read` (containment and `MAX_SOURCE_BYTES`), and stops at 5000 files or 2 s. A cut, a failure or a stopped walk sets `shortfall`. A name that is not an identifier, a family with no alias reader, or a root that is not a directory searches nothing.
- `src/codeintel/engines/graph/answer.py`: in `_known_blind_spots` (callers), after ticket 05's row-file loop, each family of the answered symbols' files is searched with `files_aliasing`. Every hit that was not already read, and whose language matches, is read through the answer's `SourceReader` (same read budget) and checked with `alias_imports`. An alias adds `aliased-import-unbound` (section `imports`) naming the file and alias, with "no caller row here comes from that file". A search shortfall adds the same gap as "could not check every file …". A file the budget refused still goes to the existing `reader.unchecked` gap.
- `src/codeintel/engines/graph/dispatch.py`: when `_dispatch_bases` found no target class (`dispatch.target is None`), `_through_siblings` now calls `_method_namesakes`. That runs one `MATCH (m:Method) WHERE m.name="m" RETURN m.qualified_name, m.file_path LIMIT 200` (via `_lookup`, `surface=False`) and builds `_Definer`s with `_method_definer`: the class is the qualified name minus its last part. The target is the direct answer's `qn`, and every other `*.m` node is a sibling. From there it runs the existing sibling loop, rows, heading, `_mark_sibling` and `dispatch-sibling-unbound`. The interface check for TS also still runs. On this class-less path a row qualifies through `_guessed_sibling_call`: `suffix_match`/`unique_name`, with call text that is the bare method name or empty, or a receiver call that `_guessed_receiver_call` accepts (so `self.`/`this.`/`super` are still refused). A failed lookup, or one that hit its row limit, adds a "could not check" `dispatch-sibling-unbound`. The class-based path from ticket 06 is unchanged.
- `src/codeintel/gateway/reindexer.py`: both `_last_fired.get(project_root, …)` defaults are now `float("-inf")`.
- `tests/test_blindspot_gaps.py`: 13 new tests (alias in a no-row file: Python on git and on a non-git root, TS import, TS re-export, walk limit gives "could not check", `files_aliasing` edges, no alias stays complete, alias in another language ignored; class-less sibling, class-less protocol member, all-bound sibling adds nothing, `self.area` not listed). `_ask_dispatch` gained a `methods=` answer for the `(m:Method)` query. One existing test changed, see Decisions.
- `tests/test_reindexer.py`: `test_the_first_pass_of_a_root_is_not_held_back_by_host_uptime` pins `time.monotonic` to 5.0.

**Proof**
- Fails before: at bbbe8ec (tests only), `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_reindexer.py -q -o addopts=` gave `8 failed, 68 passed, 1 skipped`. The failures were the 6 new positive cases (Python alias in a no-row file ×2 [git, not-a-repository], TS alias import, TS re-export, class-less sibling (StopIteration, no `total_area` row), class-less protocol member (no `totalArea` row)) plus the new uptime test (`maybe_reindex` returned False). The existing `test_an_unchanged_tree_is_fingerprinted_at_most_once_per_debounce_window` also failed: this host had been up 335 s (`/proc/uptime`), less than its 1000 s window.
- After: the check block `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_reindexer.py plans/trust-fixes/acceptance/test_tf_safe_for_destructive.py -q -o addopts=` gave `268 passed, 1 skipped`. The skip is 05's live test, which skips without the backend on PATH. With `.venv/bin` on PATH, `tests/test_blindspot_gaps.py` gave `41 passed` (the live test included). `tests/test_reindexer.py`: 38 passed.
- Acceptance: `test_tf_safe_for_destructive.py -v` gave 9/9 PASSED, including the 5 named tests: `…python_symbol_imported_as_an_alias…`, `…ts_symbol_imported_as_an_alias…`, `…ts_symbol_reexported_under_an_alias…`, `…sibling_has_a_name_matched_call…`, `…protocol_member_has_a_name_matched_call…`.
- Existing tests: I ran all 50 test files that touch the graph engine or the reindexer, with `.venv/bin` on PATH and the root-only doctor test deselected: `1 failed, 1586 passed, 29 skipped, 1 xfailed`. The one failure is `tests/test_graph_real.py::test_live_callers_of_safe_null_result`. It also fails with my source changes stashed (at 9d5d501's src), so it is environmental. It runs only when the backend is on PATH (without it, it skips, as in the gate's PATH). It is a live run against this host's stale codeintel index. Run in-process with the same call, it answers, so I left it alone. `ruff check src tests bench`: all passed. `mypy`: no issues in 88 files. compileall: ok. `codeintel c4 . --check` (backend on PATH): 0 gating. The whole Test command is left to the conductor's gate.

**Decisions**
- The tree search finds candidates by text, using git grep for `name as <ident>`, and does not use backend import edges. The acceptance fake (and 0.10.x) gives no IMPORTS rows for the aliasing file. The `as` pattern is narrow enough that the read budget is not spent on files that only mention the name. `alias_imports` still decides from each hit's text.
- The tree search does not check that the alias import is from the target's own module. That matches 05's rule, and it errs conservative: a re-export through a package (`from pkg import helper as h`, where `pkg/__init__` re-exports) names a module other than the target's file. It is restricted to the target's language family.
- Existing test changed: `test_a_file_that_does_not_exist_adds_nothing` asked `callers helper` on the Python fixture, and that fixture aliases `helper` in three files (`alias_direct.py`, `main.py`, `pkg/reexport.py`). The new search rightly finds them, so its "safe" assertion pinned the old row-files-only scope. It now asks `callers entry`, which no file aliases, with the same missing-file row. Its point (a row file that cannot be read adds nothing) and its assertions are unchanged.
- The class-less sibling path runs whenever `dispatch.target` is `None`, whether there were no class nodes or the definer lookup fell short (it keeps 06's "could not check" detail in that case). It needs one direct method symbol (`direct.count == 1`, `is_method`), as 06 does.
- On the class-less path a guessed row with no receiver in its call text counts as a sibling call. With no class to place the sibling, the backend picking a namesake by name is the evidence. 06's class-based path still requires a receiver.
- Wording avoids `{_LIMIT} rows/files` tallies ("its row limit", "its file limit"), per learnings (06).

**Shortcuts taken**
- The non-git walk has fixed limits (5000 files, 2 s) and its own skip list. A large non-git tree gets a "could not check" alias gap on every `callers` answer. Worth replacing with `rg` or the backend's file list if non-git users report noise.
- `files_aliasing` spawns one `git grep` per `callers` answer with a real root. It is not cached per root or generation. Worth caching against the gateway's cache key if latency shows up on big repos.
- A `_method_definer` class is a guess from the qualified name (the part before the method). Rendering only uses it for `Class.m` wording, and the TS interface check sees no `bases`, so every interface is "may satisfy it by structure".

**For later tickets**
- None of the open tickets change. The CHANGELOG T1 bullet (ticket 13) says the alias rule covers "an alias in a file the answer reads". It now covers any file of the language in the tree, and siblings without class nodes. Reword it at PR time.
- `tests/test_graph_real.py::test_live_callers_of_safe_null_result` fails here whenever the backend is on PATH, with or without this ticket. It is not in the gate's run (it skips without PATH).

## Review findings (round 1, pair review)

QUALITY
1. major src/codeintel/engines/graph/blindspots.py: `_walk_aliasing` (non-git root) calls `os.walk` and `qualifier_scan._read` directly, bypassing SourceReader, `qualifier_scan._within`, the shared Budget and the `_MAX_STALLED` stop. Its 2.0 s deadline is only checked between files, so one stat/open/read/listdir on a stalled NFS/FUSE mount blocks the `callers` query with no limit (the git path has `_git_capped`, 30 s kill). Fix: run each read through `qualifier_scan._within` against a Budget (or reuse the answer's SourceReader), check `_stalled_reads() >= _MAX_STALLED` before each read, and return a "could not check" shortfall when the walk is abandoned.
2. minor src/codeintel/engines/graph/blindspots.py: `files_aliasing`/`alias_imports` flag any `name as X` whatever module it comes from (ticket asked for imports of the target's module). `from other_mod import helper as h` makes `callers core.helper` partial. Fix: require the import to name the target's module (as `_imports_module` does), or say in the gap text the match is by name only.
3. minor src/codeintel/engines/graph/answer.py: one `aliased-import-unbound` gap + body note per file found, up to ~200. Fix: after the first few, collapse into one "and N other files" gap.
4. minor src/codeintel/engines/graph/blindspots.py: "search failed" shortfall puts git's raw stderr (up to 2000 bytes, may hold absolute paths) into the gap detail. Fix: cap it short or pass it through the stderr-cleaning helper.
5. minor src/codeintel/engines/graph/dispatch.py: with no class node `_method_namesakes` treats every other `*.name` method as a sibling, including the target's base classes; such rows are labelled "sibling" wrongly. Fix: say in the row/gap text that the class relation is unknown.

Checked: identifier/cypher/git inputs safe; root containment, symlink/FIFO refusal OK; git env scrubbed, output capped; reindexer -inf correct. Done-when check 268 passed, 1 skipped.

REVIEW: FAIL

SPEC
- New unit tests cover each case (Python alias, TS alias, TS re-export, classless plain sibling, classless protocol member), each failing before the change: MET. HEAD tests vs base src 9d5d501: 9 failed, 69 passed, 1 skipped; failures are the named new tests plus walk limit, files_aliasing and reindexer.
- Reindexer test with monotonic clock pinned near 0 fails before, passes after: MET (test_the_first_pass_of_a_root_is_not_held_back_by_host_uptime).
- All 5 named acceptance tests pass and the rest of the file still passes: MET (conductor; acceptance 75 passed).
- All existing tests still pass: MET. Full suite: 2448 passed, 35 skipped, 1 deselected, 1 xfailed, coverage 91.04%.
- scope: OK. All changed files in Touches plus the flow's own records.
- test first: OK. bbbe8ec adds only tests, confirmed failing without the production change.
Note: test_a_file_that_does_not_exist_adds_nothing now asks about `entry` instead of `helper` because the tree-wide search correctly finds an alias of helper elsewhere in the fixture; it still checks the same property.

REVIEW: PASS
