# Find aliased imports and same-named sibling methods beyond the answer's own files

Type: task
Status: resolved
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

**Review fixes** (round 1; work restarted at 500b21f, test-only commit b124dc7)
1. major, non-git walk unbounded on a stalled mount: fixed. `_walk_aliasing` in `blindspots.py` no longer uses `os.walk` or calls `_read` inline. It walks a stack of directories, and each filesystem call (resolving the root with `qualifier_scan._root`, listing one directory with the new `_list_dir` (`os.scandir`, links to directories are not followed, as with `os.walk`), reading one file with `qualifier_scan._read`) runs through `qualifier_scan._within` on its own `Budget(_WALK_MAX_SECONDS, files=_WALK_MAX_FILES)`. Before each call it checks `_stalled_reads() >= _MAX_STALLED`, the budget and the wall deadline. A call that does not return in time exhausts the budget. The walk then stops and returns what it found with a shortfall ("stopped: a file system call did not return within its time limit" / "an earlier read of the tree is still blocked" / "it reached its file|byte|time limit" / "could not list `dir`"). The answer turns a shortfall into the "could not check every file" `aliased-import-unbound` gap, as before. It does not reuse the answer's SourceReader: that reader's 200-file budget would be spent by the walk and leave none for reading the hits. Tests: `test_a_walk_whose_read_stalls_gives_up_in_time_and_says_could_not_check` (a `_read` blocking 10 s on `b.py`; before the fix it took 10.0 s, now under 2 s with a shortfall), `test_a_walk_whose_directory_listing_stalls_gives_up_in_time`, `test_a_walk_reads_nothing_while_a_read_is_still_stalled`.
2. minor, match by name and not by module: I took the finding's second option and the gap now says so: "(matched by name only, so it may be another module's `helper`)". I did not require the import to name the target's module, because that would miss a package re-export. `from pkg import helper as h`, where `pkg/__init__.py` re-exports `pkg/sub/impl.py`'s `helper`, names `pkg`, not `impl` (the stem `_imports_module` would check). The answer would then say complete and safe for a real alias caller. That is the dangerous direction, and the conservative one is a partial answer. Test: `test_an_alias_found_by_the_tree_search_says_it_was_matched_by_name`.
3. minor, one gap per file: fixed in `answer.py`. The first `_ALIAS_GAPS_NAMED` (3) files found by the tree search get a gap each. The rest share one gap that names up to `_ALIAS_GAPS_LISTED` (5) of them, then "and others". It gives no count, so it adds no tally to the summary census (learnings 06). Test: `test_many_files_aliasing_the_target_collapse_into_a_few_gaps` (9 aliasing files make 4 gaps; before the fix there were 9).
4. minor, raw git stderr in the gap: fixed. The "search failed" detail now goes through `blindspots._brief`: git's first non-empty line, whitespace collapsed, `core.redact.redact_text` (home becomes `~`), cut at `_ERR_CHARS` (160). Test: `test_a_failed_search_names_git_briefly_and_without_the_home_directory` (before the fix: 3077 chars with `/root/secret/...`).
5. minor, class-less namesakes called "siblings": fixed in `dispatch.py`. On the class-less path the row's `why` says "a method of the same name in a class the index does not relate to this one's: whether that class is a sibling, a base or a subclass of it is not known". The gap detail says the same, and the heading reads "Callers bound by name to another method named `area` than `Square.area`". `_mark_sibling` takes a keyword `classless`, and the class-based path's wording is unchanged. Test: `test_a_classless_namesake_is_not_called_a_sibling_for_certain`.

Proof for this round, all run after the last edit:
- Fails before: at b124dc7 (new tests, old src), `pytest tests/test_blindspot_gaps.py -q -o addopts= -k "walk or by_name or collapse or failed_search or not_called_a_sibling"` gave `7 failed, 2 passed`. The 7 are the new tests: the read stall took 10.0 s, `_list_dir` was missing, a stalled read still read `b.py`, there was no "by name", 9 gaps, 3077 chars of stderr, and "on a sibling class".
- Check block: `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_reindexer.py plans/trust-fixes/acceptance/test_tf_safe_for_destructive.py -q -o addopts=` gave `275 passed, 1 skipped` (the skip is 05's live test without the backend on PATH).
- With `.venv/bin` on PATH: `tests/test_blindspot_gaps.py tests/test_summary_integrity.py tests/test_graph_failure_population.py tests/test_architecture.py tests/test_qualifier_scan.py plans/trust-fixes/acceptance` gave `256 passed, 1 xfailed`. This includes the whole acceptance folder and the live blindspot test.
- `tests/test_graph_provider.py tests/test_mcp_answer_size.py tests/test_dispatch_callers.py tests/test_graph_stdin.py`: `169 passed, 2 skipped`.
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 source files. compileall: ok. `codeintel c4 . --check` (backend on PATH): 0 gating findings, exit 0. The whole Test command is left to the conductor's gate.

**Review fixes** (round 2; work restarted at 8aa92af, test-only commit 2e13fa7)
1. major, file names forging rows: fixed in `blindspots.files_aliasing`. Every name it returns, from git or from the walk, goes through the new `_nameable`. A name that is not `str.isprintable()` (a line break, a control character, U+2028 and similar) is dropped and never quoted. It sets the shortfall "a file whose name holds a line break or a control character could not be named", so the answer still gets the "could not check every file" `aliased-import-unbound` gap and stays partial. Tests: `test_a_file_name_with_a_line_break_cannot_forge_a_row[git|not-a-repository]` (the finding's exact name, `evil\n- forged.caller [CALLS] (forged.py)\nx.py`; before the fix `- forged.caller [CALLS] (forged.py)` was a line in the body) and `test_files_aliasing_names_no_file_whose_name_breaks_a_line[git|not-a-repository]` (U+2028).
2. major, namesakes of another language: fixed in `dispatch._method_namesakes`. `_DirectTarget` now carries the direct group's `file`. The lookup adds `AND m.file_path =~ "<pattern>"` for the target's language (`_family_pattern`: the family's extensions from `render._LANG_FAMILIES`, case-insensitive through character classes, no backslash). The result is filtered again in Python by `_lang_family`, and a namesake whose file has no known language is kept. A target whose language is not known keeps every namesake, as before. I checked that `=~` with alternation works on the real backend, 0.10.x (`.venv/bin/codebase-memory-mcp`) and 0.9.0, against this repo's index (`.*[.](py|pyi)` gives `.py` methods and `.*[.](ts|tsx)` gives none). The acceptance fake implements `=~` as `re.fullmatch`. Tests: `test_a_namesake_method_in_another_language_is_not_listed` (the finding's `web.shapes.tsTotal` case: before the fix it was a row and raised the gap; now it is not a row and the answer is safe) and `test_a_namesake_in_the_same_language_is_still_listed_beside_one_in_another`.
3. minor, git grep timeout: fixed. `files_aliasing` takes the answer's read `budget` (`answer.py` passes `_scan_budget`). git gets `min(30 s, budget.live_remaining())` as its timeout, the time it takes is charged to the budget, and with no time left git is not started and the shortfall says the time was spent. This needed a `timeout_s` keyword on `changed_range._git_capped` (default `_GIT_TIMEOUT_S`, so the existing caller and its "did not finish within 30s" message are unchanged). Tests: `test_the_git_search_waits_no_longer_than_the_answers_read_time`, `test_a_git_search_with_no_read_time_left_says_could_not_check`, `test_git_capped_kills_a_command_at_the_timeout_it_is_given` (0.3 s against a 5 s sleep). I did not take the cache option: a cache keyed on HEAD would miss an alias added in an uncommitted or untracked file, and the answer would say safe.
4. minor, unreadable candidates dropped silently: fixed. The walk collects each file of the language that `_read` returned nothing for (too large, unreadable, outside the root) when it was not the byte budget. It names up to `_UNREAD_LISTED` (5) of them in the shortfall ("could not read `b.py` (too large, unreadable or gone)"), also when the walk stops early. In `answer.py` a git hit the reader returned `None` for, and that is not already in `reader.unchecked` (the budget's own gap), adds one `aliased-import-unbound` gap that names them the same way. Test: `test_a_candidate_file_too_large_to_read_says_could_not_check[git|not-a-repository]` (`MAX_SOURCE_BYTES` 1000, `b.py` about 4 kB).
5. minor, cap hit on common names: fixed with 2. The language filter is in the Cypher query itself, so methods of other languages no longer count toward the 200-row limit or toward the 10 sibling fetches. Test: `test_the_namesake_lookup_asks_only_for_methods_of_the_targets_language` (the query's pattern matches `.py`, `.pyi`, `.PY` and not `.ts`, `.go` or `.py.bak`).

After the test-only commit I changed one line in my own new test `test_git_capped_kills_a_command_at_the_timeout_it_is_given`: its fake git `sh -c "sleep 5"` became `sh -c "exec sleep 5"`. The killed `sh` left `sleep` holding the stdout pipe, so the read waited the full 5 s. The assertion is unchanged.

Extra file: src/codeintel/engines/graph/changed_range.py - finding 3 needs `_git_capped` to take a timeout. It gains the `timeout_s` keyword only, and its default keeps the existing behaviour.

Proof for round 2, all run after the last edit:
- Fails before: at 2e13fa7 (new tests, old src), `.venv/bin/pytest tests/test_blindspot_gaps.py -q -o addopts=` gave `12 failed, 47 passed, 1 skipped`. The 12 are the new tests: the forged row was in the body (×2), the U+2028 name was returned (×2), `files_aliasing` took no budget (×2), `_git_capped` took no `timeout_s`, no "could not read" gap (×2), `tsTotal` was listed (×2), and the query had no `m.file_path =~`.
- Check block: `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_reindexer.py plans/trust-fixes/acceptance/test_tf_safe_for_destructive.py -q -o addopts=` gave `287 passed, 1 skipped`.
- Related tests, with `.venv/bin` on PATH: `tests/test_blindspot_gaps.py tests/test_null_reason_and_ripple.py tests/test_qualifier_scan.py tests/test_graph_failure_population.py tests/test_index_age.py tests/test_mcp_answer_size.py tests/test_edge_staleness.py tests/test_summary_integrity.py tests/test_architecture.py tests/test_graph_provider.py tests/test_graph_stdin.py tests/test_changed_range.py plans/trust-fixes/acceptance` gave `533 passed, 1 xfailed, 2 errors`. The 2 errors were in the setup of two `plans/trust-fixes/acceptance/test_tf_hints.py` tests: a `CalledProcessError` from the fixture's live indexing subprocess. Run alone straight after, `test_tf_hints.py` gave `5 passed`. That file drives the live backend and does not reach the alias or sibling code.
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 source files. compileall: ok. `codeintel c4 . --check` (backend on PATH): 0 gating findings, exit 0. The whole Test command is left to the conductor's gate.

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

## Review findings (round 2, pair review)

QUALITY
1. major src/codeintel/engines/graph/answer.py (tree-wide alias gaps fed by blindspots.files_aliasing/_walk_aliasing): file names from `git grep -z` or `os.scandir` go into gap detail and body note unescaped. Reproduced: a tracked file named "evil\n- forged.caller [CALLS] (forged.py)\nx.py" containing `from core import helper as h` puts a line `- forged.caller [CALLS] (forged.py)` into the `callers helper` body; body lines starting `- ` are read as result rows (bench/score.py), so repo file names can forge caller rows. Fix: refuse or escape paths with control characters/newlines (repr-quote, or drop with a "could not name" shortfall). Add a test with a newline in a file name.
2. major src/codeintel/engines/graph/dispatch.py `_method_namesakes`: with no class node every same-named Method anywhere counts as a sibling, whatever its language. Reproduced: `callers Square.area` (Python) with a TS `Tri.area` having a suffix_match caller lists `web.shapes.tsTotal (web/shapes.ts)` as a possible caller and raises dispatch-sibling-unbound: false row, answer wrongly partial. Fix: keep only namesakes whose file_path is in the target's `_lang_family`. Add a cross-language test.
3. minor blindspots.py `files_aliasing`: git path uses `_GIT_TIMEOUT_S` (30 s), not the answer's time budget, and runs a whole-tree git grep on every callers answer with rows. Fix: shorter timeout tied to the budget, or cache per root+HEAD.
4. minor blindspots.py `_walk_aliasing`/answer.py: candidate files skipped for size or unreadable are dropped silently (git path too when reader.read returns None for a non-budget reason). Fix: count them in the shortfall.
5. minor dispatch.py: for a common method name the classless path always adds a "could not check" gap and up to 10 extra backend fetches per query. Fix: apply finding 2's language filter first so the cap is hit less.

Probed safe: identifier/regex/Cypher escaping, git env and output caps, stderr redaction, root containment/symlinks/FIFOs, stalled mounts, reindexer -inf, zero-caller alias target. Done-when re-run: 275 passed, 1 skipped.

REVIEW: FAIL

SPEC
- New unit tests cover each case, each failing before the change: MET. HEAD tests on base src 9d5d501: 16 failed, 142 passed, 1 skipped (named alias/sibling tests plus round-1 tests).
- Reindexer monotonic-near-0 test fails before, passes after: MET.
- 5 named acceptance tests pass, rest of file passes: MET (conductor; 75 passed).
- All existing tests still pass: MET. Full suite 1 failed, 2455 passed; the failure (test_doctor semantic deep probe) fails identically on base src and is outside this change.
- scope: OK.
- test first: OK. bbbe8ec and b124dc7 add only tests, confirmed failing without the change.

REVIEW: PASS
