# Add known-blind-spot gaps: aliased imports, nested shadowing, and `lsp_ts_local`

Type: task
Status: open
Blocked by: —
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/engines/graph/blindspots.py, src/codeintel/engines/graph/answer.py, src/codeintel/engines/graph/confidence.py, tests/fixtures/blindspots/, tests/test_blindspot_gaps.py, tests/test_edge_confidence.py

This ticket covers rules 1, 3 and 4 of T1. It fixes `complete-callees-miss-aliased-import` and `nested-fn-false-verified-caller`, and the alias and nested halves of `ts-safe-for-destructive-wrong-half`. Today `_settle_evidence` sets `safe_for_destructive` only from row buckets and gaps, and knows nothing of what the backend cannot see.

1. **New module `src/codeintel/engines/graph/blindspots.py`.** It holds pure functions over rows plus file text. File text is read through `qualifier_scan`'s budgeted reader, never with a bare `open`, so containment and the read budget hold.
   - **`alias_imports(text, name)`** finds `import … name as Y`, `from … import name as Y`, `{ name as Y }` and `export { name as Y }`, and returns the aliases.
     - For `callers X`: if any USAGE or import row's file shows an alias of X, add an `aliased-import-unbound` gap naming the file and the alias.
     - For `callees F`: scan F's body for `alias(` calls that have no matching callee row, and add the same gap.
   - **`nested_shadow(text, caller_span, name)`** returns True when the caller's own body defines `name` before the call. Use `ast` for `.py`, falling back to the regex when the file does not parse. Use the regex `function\s+name\b` for TS/JS.
     - On a hit, demote that row to name-matched and add `target-ambiguous` with a detail naming the nested definition.
   - **Positive evidence only.** A rule fires on text it read. A file that does not exist or cannot be opened adds nothing (most existing graph tests use row files under a made-up `ROOT` that does not exist, and their answers must not change). Only a file skipped because the scan budget ran out adds the gap, with "could not check" wording (spec.md, Edge cases).
2. **Wire it into `AnswerRendering._render_edge_answer`.** Run the rules after the rows are grouped and before `_record_rows`, so the demoted buckets are the ones recorded. Every new gap goes through `self._add_gap`, and an existing gap already forces `partial`/`safe_for_destructive: false`. Do not add a second trust field.
   - **Keep the summary census unchanged.** `tests/test_summary_integrity.py` fails on a new string in `src/` that is a heading with `→` or `(N …)`, a tally phrase (a count followed by a unit noun such as `file`, `row`, `caller`, `symbol`), or a dict literal with a claim key (`confidence`, `runnable`, …) in a function not in its `_VERIFIED_BY`. Word gap details with names (the file and the alias), not counts, and keep `blindspots.py` free of claim-key dicts. That file is not on Touches; the check block runs it.
3. **`_name_resolution_note`.** Never print "the only symbol named `X`" when the nested-shadow rule fired.
4. **`confidence._evidence_class`.** `lsp_ts_local` is no longer resolved on its own. Classify it as name-matched, with a `why` that says so. `lsp_direct` stays resolved unless rule 1's nested check demoted the row.
5. **Fixtures.** `tests/fixtures/blindspots/py/` and `tests/fixtures/blindspots/ts/` reproduce the repros in the findings file:
   - `helper as h` through a re-export
   - `alias_direct.py`
   - the shapes file with `outer`/nested `inner` plus a module `inner`
   - `Circle`/`Square.area` with `total_area`, and the TS `Shape` interface, for ticket 06
   
   The Python fixtures are linted by the gate's `ruff check src tests bench`, and the `tests/*` per-file-ignores in `pyproject.toml` cover only security and style rules, not pyflakes: an unused alias import (`F401`) fails the gate (checked with the venv's ruff 0.17.6 on a scratch copy of `pyproject.toml`). So every alias a fixture imports is used (`h()` is called), and a re-export lists the alias in `__all__`. Do not add an ignore or a `noqa`; change the fixture instead. The `.ts` fixtures are not linted.

## Not in this ticket

- Sibling-method and interface dispatch (`dispatch-sibling-unbound`) is ticket 06.
- Module-scope counting and USAGE narrowing are ticket 07.
- `test_bench_oracle_ts.py` cases are not added: that oracle scores accuracy, and its expected sets are a separate decision.

## Done when

- New `tests/test_blindspot_gaps.py` uses the monkeypatched `_run` fake with rows shaped like the 0.9.0 rows in the findings, and reads the fixture files as the scanned source. These fail on main and pass here:
  - Python `callers helper` → `aliased-import-unbound`, `safe_for_destructive` false
  - Python `callees entry` → `aliased-import-unbound`, not complete
  - Python and TS `callers inner` → `outer` is not resolved, `target-ambiguous` present, no "only symbol named"
  - a row with strategy `lsp_ts_local` → `possible`, not `verified`
- A live test in the same file runs `codeintel index` and `query` on `tests/fixtures/blindspots/py` (copied to `tmp_path`), and skips when `codebase-memory-mcp` is not on PATH, the same way `tests/test_graph_real.py` skips. It asserts `safe_for_destructive` is false for `helper` and `inner`.
- `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_edge_confidence.py tests/test_dispatch_callers.py tests/test_qualifier_scan.py tests/test_changed_range.py tests/test_graph_failure_population.py tests/test_summary_integrity.py tests/test_docs_trust_claims.py -q -o addopts=` passes, and all existing tests still pass. Only `tests/test_edge_confidence.py` among the existing files may change, and only where a test pins `lsp_ts_local` as resolved (none does today); the others pass unchanged, which shows the rules fire only on evidence.

```check
$ .venv/bin/pytest tests/test_blindspot_gaps.py tests/test_edge_confidence.py tests/test_dispatch_callers.py tests/test_qualifier_scan.py tests/test_changed_range.py tests/test_graph_failure_population.py tests/test_summary_integrity.py tests/test_docs_trust_claims.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "An agent deciding whether a change is safe", and Decisions ("Where the blind-spot rules live", "How the nested-shadow check reads source")
- redblue-fixplan.md § T1
- src/codeintel/engines/graph/qualifier_scan.py: the budgeted reader to reuse, read-only

## Answer
