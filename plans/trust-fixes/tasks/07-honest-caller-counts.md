# Count module-scope rows as imports, filter shadowing USAGE rows, and fix the ambiguity banner

Type: task
Status: open
Blocked by: 04, 05, 06
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/engines/graph/answer.py, src/codeintel/engines/graph/edges.py, tests/test_dispatch_callers.py, tests/test_null_reason_and_ripple.py, tests/test_summary_integrity.py, tests/test_edge_confidence.py, tests/test_caller_counts.py, tests/test_graph_real.py, tests/test_graph_failure_population.py, tests/test_docs_trust_claims.py, docs/trust.md, docs/graph.md

It is ordered after 06 because both change `tests/test_dispatch_callers.py` and `tests/test_edge_confidence.py`, and after 04 because both change `tests/test_summary_integrity.py` and `docs/graph.md`.

This ticket fixes T5: findings `callers-collapse-to-module-scope`, `usage-rows-from-shadowing-names` and `ambiguous-count-wrong`. All three changes are in `src/codeintel/engines/graph/answer.py`.

1. **`_kind_counts`.**
   - Stop folding module-scope CALLS/USAGE rows into `_DIRECT_KIND`. Count them under a new `IMPORT` kind, and add its description to `edges._EDGE_KINDS`.
   - `_render_edge_answer`'s heading reads `## Callers of X (4 direct calls, 10 files import it — call sites inside not resolved)` when there are import rows.
   - Add an `import-only-callers` gap when any import row exists.
   - `_display` of a module-scope row is unchanged, so `bench/score.py` still reads `- ` lines as rows.
2. **USAGE rows under a narrowed target.**
   - When `wanted.narrowed`, extend `_text_can_judge`/`_scan_for_qualifier` to USAGE rows: drop a USAGE row whose file neither mentions the qualifier nor imports the target's module.
   - Where the scan cannot judge, badge the row name-matched and leave it out of the direct count.
   - The `_name_resolution_note` "Narrowed by …" sentence then stays true.
3. **The ambiguity banner.**
   - Replace "**N distinct symbols in this index are named `X`**" with "N symbols named `X` have caller edges; M more named `X` exist with none". M comes from one `_query_rows` count, `MATCH (n) WHERE n.name = <X>`, using the existing `_cypher_literal` quoting. If that count fails, omit the M clause and say "others may exist".
   - Never print the "Settle it" / "`<token>` is the name it is qualified by" text for an unqualified target (`_settle_name_matches`, `_discriminator`).
4. Update `docs/trust.md` and `docs/graph.md` wherever they quote the old heading shape or banner.

## Not in this ticket

- Recovering the enclosing function of a module-scope call from text ("recovered from text" rows) is the optional half of the fix plan. It is dropped here: the gap and the honest count meet the expected behaviour.
- Alias and nested rules are ticket 05; sibling dispatch rows are ticket 06.

## Done when

- New `tests/test_caller_counts.py` fails on main and passes here:
  - a module-scope-only caller gives the `import-only-callers` gap and `0 direct calls`
  - a param-shadow USAGE row (`def param_shadow(process)`) is excluded under the narrowed target `impl.process`
  - with 5 `run` groups answered and a count query returning 18, the banner says "5 symbols named `run` have caller edges; 13 more named `run` exist with none"
  - an unqualified `run` prints no "Settle it"
- Existing tests that pin the old shape are updated on purpose, and only where the rows really are module scope:
  - `tests/test_graph_real.py::test_callers_parses_columns_rows` pins "Callers of safe_null_result (0 direct, 3 other reference(s))"; it changes only if those captured rows are module scope.
  - `tests/test_graph_failure_population.py::test_callers_render_module_scope_nodes_as_locations_not_file_dunders` and `test_a_symbol_referenced_only_from_module_scope_is_not_reported_as_uncalled` pin `confidence: complete` and no gaps for module-scope callers. They now expect `partial` and the `import-only-callers` gap; their rendering assertions (`module scope of …`, no `__file__`, the row counts) stay.
  - `tests/test_summary_integrity.py`: the heading stays in `AnswerRendering._render_edge_answer` (registered `count` key) and the banner in `_settle_name_matches` (registered `tally` key). If removing the "Settle it" text removes a census site, delete its `_VERIFIED_BY` entry and verifier (`test_the_registry_has_no_entries_for_summaries_that_no_longer_exist` fails on a stale one); if the new banner lands in a new function, register it with a verifier. The aggregate verifier's `_HEAD_SPLIT` pattern (with `_sections` and `_headline_disagreements`) parses `(N direct, M other reference(s))`; extend it to the new heading shape so the headline-versus-rows check still runs on it, and keep the heading's numbers summing to the `- ` rows beneath it (one module-scope row per importing file).
  - `bench/score.py` and `tests/test_bench_qualified.py` read `- module scope of <file>` rows and the `target-ambiguous` gap kind, not the heading or the banner text, so they pass unchanged.
  - `tests/test_docs_trust_claims.py` checks that every gap kind `docs/trust.md` names is raised somewhere; name `import-only-callers` there only once it is raised.
- `.venv/bin/pytest tests/test_caller_counts.py tests/test_dispatch_callers.py tests/test_null_reason_and_ripple.py tests/test_summary_integrity.py tests/test_edge_confidence.py tests/test_graph_real.py tests/test_graph_failure_population.py tests/test_docs_trust_claims.py tests/test_bench_qualified.py tests/test_qualifier_scan.py -q -o addopts=` passes, and all existing tests still pass.

```check
$ .venv/bin/pytest tests/test_caller_counts.py tests/test_dispatch_callers.py tests/test_null_reason_and_ripple.py tests/test_summary_integrity.py tests/test_edge_confidence.py tests/test_graph_real.py tests/test_graph_failure_population.py tests/test_docs_trust_claims.py tests/test_bench_qualified.py tests/test_qualifier_scan.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "An agent reading caller counts"
- redblue-fixplan.md § T5
- bench/score.py `graph_answer` reads `- ` lines as rows (read-only)

## Answer
