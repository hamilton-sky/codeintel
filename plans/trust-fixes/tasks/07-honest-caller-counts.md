# Count module-scope rows as imports, filter shadowing USAGE rows, and fix the ambiguity banner

Type: task
Status: resolved
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

Work started at 279446f7d6595b39a8e63d9c156b8ca126cec65a. Test-only commit: d070cce.

**Built**
- `src/codeintel/engines/graph/edges.py`: `_IMPORT_KIND = "IMPORT"`, plus its `_EDGE_KINDS` description.
- `src/codeintel/engines/graph/answer.py`:
  - `_row_kind`, used by `_kind_counts` and the row sort. A module-scope CALLS/USAGE row now counts as `IMPORT`, not `_DIRECT_KIND`. `_display` is unchanged, so the row still reads `- module scope of <file>`.
  - Heading: when there are import rows it reads `(N direct call(s), [K other reference(s), ]M file(s) import it — call sites inside not resolved)`. Without import rows the old shapes stay.
  - `_kind_note` raises `import-only-callers` (constant section `callers`) and adds one body sentence. IMPORT rows are kept out of `non-call-relationships`.
  - `_narrow_usage_rows` runs first in `_render_edge_answer`, for `callers` under a narrowed target. It reads each non-resolved, non-module-scope USAGE row's file through `blindspots.SourceReader` (the answer's `_scan_budget`). It drops the row when the file neither contains the qualifier token (substring, as in `qualifier_scan`) nor has an import statement naming the target file's module stem (whole word). An unreadable file, or the defining file, gives no verdict: the row stays, demoted to `name-matched` with `_low_confidence`. The note counts dropped and unchecked rows and names no dropped row. If every row is dropped, the answer is `(0)` with "no caller survived the check …".
  - The ambiguity banner is now `**N symbols named `X` have <unit> edges; M more named `X` exist with none**`. M = count − N, from `_namesakes_total`: one `MATCH (n) WHERE n.name="<_cypher_literal(X)>" RETURN count(n) AS symbols`, sent through `DispatchCallers._lookup(..., surface=False)`. When there is no reply, the reply is not exactly one row with one column, or the value is not an integer, the banner says "others may exist with none (…)". The `target-ambiguous` gap detail uses the same wording.
  - `_settle_name_matches` returns `""` for an unqualified target. `_discriminator` uses only `wanted.qualified`, no longer `groups[0].qn_raw`.
- `src/codeintel/engines/graph/provider.py` (extra file, see below): adds `_answered_lookup = (project, timeout_ms)` per thread, set beside `_answered_root` in `build_result`.
- `docs/trust.md` and `docs/graph.md`: the import heading shape, `import-only-callers`, the USAGE narrowing, the new banner, and "no Settle it for a bare target".
- Tests:
  - New `tests/test_caller_counts.py` (14 tests).
  - `tests/test_graph_failure_population.py`:
    - The two module-scope tests now expect `partial` with `["import-only-callers"]` and the new heading. Their `module scope of …`, no-`__file__` and row-count assertions stay.
    - The "distinct symbols" assertions now check the new banner.
    - The impact test's iterator stub answers the count query apart from the edge queries.
  - `tests/test_edge_confidence.py::test_a_bare_target_is_never_scanned_because_a_re_export_defeats_the_stem`: the bare target now prints no `Settle it`.
  - `tests/test_summary_integrity.py`:
    - `_HEAD_SPLIT` and `_sections` parse and sum the new heading.
    - New registry entry `("tally", "AnswerRendering._render_edge_answer")` for the banner.
    - `_DISCLOSURES` gains "separate answers, not one"; "distinct symbols" is kept.
  - `tests/test_docs_trust_claims.py`: the gap-kind regex also checks `import-only-callers`.

Extra file: src/codeintel/engines/graph/provider.py - the banner's M needs one `_query_rows` count, which needs the project and a timeout. The renderer has neither, and `ops.py` (which has both) is not on Touches either. Two lines record them per thread beside `_answered_root`.

**Proof**
- Fails at the test commit, passes here. At d070cce (tests only), `.venv/bin/pytest tests/test_caller_counts.py tests/test_graph_failure_population.py tests/test_edge_confidence.py tests/test_summary_integrity.py -q -o addopts=` gave 18 failed, 200 passed, 1 xfailed. Every new caller-count test failed except the two controls, along with the updated existing tests and the registry's stale-entry check. Each failure was the old behaviour: `(3)`/`complete` for module-scope rows, `param_shadow` listed, "distinct symbols in this index", `Settle it … 'mod0'` for bare `run`. After the code change all of them pass.
  - Module-scope only: `## Callers of safe_null_result (0 direct calls, 2 files import it — call sites inside not resolved)`, the `import-only-callers` gap, `partial`, safe false (`test_a_caller_only_at_module_scope_is_an_import_and_not_a_direct_call`).
  - Param shadow under `impl.process`: rows are exactly `["main.run", "uses.via_module"]`. `hier.param_shadow` appears nowhere in the body. "1 `USAGE` row(s) dropped". An unreadable file's row is `name-matched`, not verified, and outside the direct count (`(1 direct, 2 other reference(s))`).
  - 5 `run` groups with the count query returning 18: "5 symbols named `run` have caller edges; 13 more named `run` exist with none", sent as exactly one count query with `n.name="run"`. With a failed count (`[]`, a non-integer, two rows) it says "others may exist", with no M and no `Incomplete:`.
  - Unqualified `run` with 6 name-matched rows: no "Settle it", no "is the name it is qualified by".
- Check block: `.venv/bin/pytest tests/test_caller_counts.py tests/test_dispatch_callers.py tests/test_null_reason_and_ripple.py tests/test_summary_integrity.py tests/test_edge_confidence.py tests/test_graph_real.py tests/test_graph_failure_population.py tests/test_docs_trust_claims.py tests/test_bench_qualified.py tests/test_qualifier_scan.py -q -o addopts=` gave `412 passed, 1 skipped, 1 xfailed`, exit 0. The skip is pre-existing (a live test with no backend on the bare PATH). `test_graph_real.py::test_callers_parses_columns_rows` is unchanged: its captured rows are function USAGE rows, not module scope, so it still reads `(0 direct, 3 other reference(s))`. `test_bench_qualified.py` and `bench/score.py` are unchanged and pass.
- Existing tests: all 56 test files that use the graph engine or `build_result`, with the known root-only doctor test deselected, gave `1563 passed, 30 skipped, 1 deselected, 1 xfailed`. The whole Test command is left to the conductor's gate.
- Acceptance `plans/trust-fixes/acceptance/test_tf_caller_counts.py`: 3 passed.
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 files. compileall: ok. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: "0 gating finding(s) — exit 0".
- Live, on this repo:
  - The count query answers on both backends: 0.10.8 `rows: 1 (cols: symbols) "20"`; 0.9.0 `{"columns":["symbols"],"rows":[["20"]]}`.
  - `codeintel query --op callers --target run` gave "8 symbols named `run` have caller edges; 12 more named `run` exist with none".
  - `callers safe_null_result` gave `(16 direct calls, 1 file imports it — call sites inside not resolved)`.

**Decisions**
- Which USAGE rows are narrowed: only the non-`resolved` ones that are not module scope and not `via`. A resolved row followed a real binding. A module-scope row is now an import, counted as such.
- How the file is matched: substring for the qualifier, as `qualifier_scan` does (lenient, so it keeps doubtful rows); whole word for the module stem inside import statements (`__init__`/`index` use the directory name).
- The drop note counts and does not name the dropped rows. The acceptance test requires that dropped names appear nowhere in the body.
- No gap for dropped rows. The rows were namesakes, so dropping them makes the answer more correct, not less complete. The body states the count.
- The count query is optional, so it goes through `_lookup(surface=False)`. A timeout there never becomes the answer's backend failure.
- Unqualified means `not wanted.qualified`, so `run@file` prints no Settle it either.
- Import rows now sort after direct calls (`_row_kind` in the sort key), so the direct calls a heading counts come first.

**Shortcuts taken**
- `ops.py` builds `_confidence_note` and the cap notes before the render, so their "N of M row(s)" counts (and the `low-confidence-edges` gap) still include USAGE rows the narrowing then drops. The drop note says so ("Any row count in a note above this one was taken before they were dropped"). The real fix is to call `_narrow_usage_rows` in `ops._op_callers` before `_confidence_note`, which is outside Touches.
- `changed`'s pool workers do not inherit `_answered_lookup`, so an ambiguous `callers` inside `changed` says "others may exist" instead of a number. Propagating it is one line in `changed.py`, beside `_answered_root`.
- The module-stem check reads import lines by regex. A module imported through a re-export under another name does not count as an import, so a real USAGE row in such a file is dropped when its file also never writes the qualifier.

**For later tickets**
- 11 (hints): the ambiguity banner and the `target-ambiguous` gap detail now say "N symbols named `X` have <unit> edges". The "Ask again as …" hint text is unchanged.
- 13 (CHANGELOG): new gap `import-only-callers` (section `callers`), the new heading shape, USAGE narrowing, the banner with M (one extra `query_graph` count per ambiguous answer), and no Settle it for bare targets. On eb0902f, `tests/test_caller_counts.py` fails on assertions (no ImportError).
