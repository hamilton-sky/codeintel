# Show sibling and interface dispatch candidates on method callers

Type: task
Status: open
Blocked by: 05
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/engines/graph/dispatch.py, src/codeintel/engines/graph/blindspots.py, tests/test_blindspot_gaps.py, tests/test_dispatch_callers.py, tests/test_edge_confidence.py

This ticket covers rule 2 of T1. It fixes `dynamic-dispatch-asymmetric` and the interface half of `ts-safe-for-destructive-wrong-half`. The backend binds a duck-typed `s.area()` to `Circle.area` by suffix match, so `callers Square.area` answers complete and safe without it.

For `callers C.m`, in `DispatchCallers`, next to `_callers_through_bases`:

1. Ask the backend for the other `*.m` methods (sibling classes) and for interface or Protocol members named `m`. Reuse `_classes_defining`, `_method_owners` and the existing `_lookup` scope and budget.
2. For each sibling whose callers include a name-matched receiver-call row (`suffix_match`, `unique_name`) that is not already in this answer, add that row here as name-matched. Its `why` names the sibling it was bound to.
3. Add a `dispatch-sibling-unbound` gap naming the sibling or interface, only on evidence: a sibling with a different qualified name than the target that has such a row (step 2), or an interface that declares `m` (step 4). A sibling lookup that returns only the target itself, or nothing, adds nothing.
4. For TS, also add the gap when the class `implements` an interface that declares `m`, or when a fixture interface declares `m` and the class defines it. Detect the declaration with a text check in `blindspots.py` over the interface's file.

**Existing method-callers tests.** `tests/test_dispatch_callers.py` and `tests/test_edge_confidence.py` ask `callers` of method targets (`GraphProvider.build_result`, `CodeProvider.build_result`, `StrategyChain.resolve`, `Child.run`) through fake `_run`/`_query_rows` routers, and many assert `confidence: complete` and `safe_for_destructive: true`. The new sibling query must not turn those answers partial unless the fixture really has a sibling with a name-matched receiver row. Where a fake router answers the new query wrongly (for example returns `None` for an unknown Cypher, or the edge rows for any query with `-[`), teach the fake to answer it the way the real backend would. Do not change any `confidence`, `safe_for_destructive` or gap assertion in those files.

**Keep the summary census unchanged.** `tests/test_summary_integrity.py` fails on a new heading with `→` or `(N …)`, a new tally phrase (a count followed by a unit noun), or a new claim-key dict outside a function in its `_VERIFIED_BY`. `DispatchCallers._members_of` and `_grow_hierarchy` already hold registered tallies; new text in a new method must not count. Word the gap with names. That file is not on Touches; the check block runs it.

Stop at the lookup budget. When the budget is spent before siblings were checked, add the gap with "could not check" wording rather than staying silent.

## Not in this ticket

- A "Callers through `<Iface>.<m>`" section for TS structural interfaces is `plans/dogfood-fixes` tickets 04-06. This ticket adds only the gap and the name-matched rows.
- Alias and nested rules are ticket 05.

## Done when

- `tests/test_blindspot_gaps.py` (new in ticket 05) gains these tests, which fail on main and pass here:
  - Python `callers Square.area` lists `total_area` as name-matched, has `dispatch-sibling-unbound`, and is `safe_for_destructive` false
  - `callers Circle.area` is unchanged (still partial, `total_area` still listed once)
  - TS `callers Square.area` with `Shape` declaring `area` has the gap
- The existing Protocol "via" tests in `tests/test_dispatch_callers.py` still pass unchanged. `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_docs_trust_claims.py tests/test_summary_integrity.py tests/test_null_reason_and_ripple.py tests/test_graph_failure_population.py -q -o addopts=` passes, and all existing tests still pass. In `tests/test_dispatch_callers.py` and `tests/test_edge_confidence.py` only fake routers change; no assertion does (the reviewer checks the diff).

```check
$ .venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_docs_trust_claims.py tests/test_summary_integrity.py tests/test_null_reason_and_ripple.py tests/test_graph_failure_population.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "An agent deciding whether a change is safe"
- redblue-fixplan.md § T1, rule 2
- tests/fixtures/blindspots/ (from 05)

## Answer
