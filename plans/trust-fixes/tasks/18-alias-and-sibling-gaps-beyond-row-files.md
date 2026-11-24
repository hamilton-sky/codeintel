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
