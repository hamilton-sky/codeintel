# Show sibling and interface dispatch candidates on method callers

Type: task
Status: resolved
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

Work started at 80df3c1b4fd500b3687884dd3a690d27e4dbbb38. Test-only commit: 4f97ee5.

**Built**
- `src/codeintel/engines/graph/dispatch.py`: `_callers_through_bases` now runs `_dispatch_bases` itself and passes the result to `_through_bases` (its `target` string parameter, unused, is gone) and to the new `_through_siblings`, all in the same lookup scope and `try`. `_Dispatch` keeps the `definers` its one `_classes_defining` query returned, so the sibling list costs no extra query. `_through_siblings` fetches each sibling's callers with `_fetch_edges` through `_guarded` (`surface=False`). It keeps the rows that `_guessed_receiver_call` accepts (`suffix_match`/`unique_name`, call text `recv.m` whose receiver is not `self`/`cls`/`this`/`super`) and whose caller is not already a caller row of this answer. It lists them under "## Callers bound by name to a sibling of `C.m`" (no count in the heading), records them as name-matched rows, and raises `dispatch-sibling-unbound` (section `callers`) for each sibling with such rows. For TS/JS targets, `_interfaces_declaring` lists `Interface` nodes, reads each one's file through `SourceReader` under the answer's `_scan_budget` (implemented interfaces first), and raises the gap for each interface that declares `m`. The "could not check" wording covers: a sibling fetch that failed or ran out of the lookup budget (it names that sibling and every later one), siblings past `_MAX_BASES`, a sibling reply cut at its row limit, a failed or full interface listing, interface files the read budget refused, and a target whose definer lookup fell short.
- `src/codeintel/engines/graph/blindspots.py`: `interface_declares(text, interface, member)` is a text check over the brace-matched body of `interface <Name>`.
- `tests/test_blindspot_gaps.py`: adds 9 tests and the `_ask_dispatch` router, which answers the edge query with every row of the bare name, the definer query, and the interface listing, as the backend does.

**Proof**
- Fails on main and passes here: at 4f97ee5 (tests only), `.venv/bin/pytest tests/test_blindspot_gaps.py -q -o addopts=` gave `5 failed, 24 passed`. The failures were: Python `callers Square.area` (StopIteration: no `total_area` row), the sibling "could not check" test, TS `callers Square.area` with `Shape` (no gap), TS budget "could not check", and `interface_declares` (ImportError). After the code change: `29 passed`. The `callers Circle.area` test (rows exactly `["total_area"]`, partial, no sibling gap) and the three negative tests already passed on main. That is what they are for: proving nothing changes there.
  - Python `callers Square.area`: `total_area` is `name-matched`, `verified` false, and its `why` contains "`Circle.area`". The `dispatch-sibling-unbound` gap names `Circle.area` and `total_area`, with section `callers`. The answer is `partial` with `safe_for_destructive` false. `only_square` is still verified.
  - TS `callers Square.area`: the gap names `` `Shape` `` and `area`, the answer is partial and not safe.
- Live (codebase-memory-mcp 0.10.8, the fixtures copied to scratch and indexed with `HF_HUB_OFFLINE=1`): Python `callers Square.area` gives `dispatch-sibling-unbound`, partial, safe false, with `total_area` as name-matched. Python `callers Circle.area` is unchanged: only `total_area`, gaps `[low-confidence-edges]`. TS `callers Square.area` and `Circle.area` both get the gap through `Shape`.
- Check block `.venv/bin/pytest tests/test_blindspot_gaps.py tests/test_dispatch_callers.py tests/test_edge_confidence.py tests/test_docs_trust_claims.py tests/test_summary_integrity.py tests/test_null_reason_and_ripple.py tests/test_graph_failure_population.py -q -o addopts=` gave `352 passed, 1 skipped, 1 xfailed`. The skip is 05's live test, which skips when the backend is not on PATH (05's `Floor: allow skip`). With `.venv/bin` on PATH the live test passes too.
- `tests/test_dispatch_callers.py` and `tests/test_edge_confidence.py` are unchanged (`git diff --stat 80df3c1` on them is empty). Their fake routers already answer the new queries correctly: a sibling fetch reuses the edge Cypher, and `asked.matches` filters the rows by qualified name. The Protocol "via" tests pass unchanged.
- Other tests: all 43 test files that use the graph engine, with the known root-only doctor test deselected, gave `1361 passed, 29 skipped, 1 deselected, 1 xfailed`. `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 files. compileall: ok. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: "0 gating finding(s) — exit 0". The whole Test command is left to the conductor's gate.

**Decisions**
- The work is in `dispatch.py`, not in `answer.py::_known_blind_spots` (05's hint), because `answer.py` is not on Touches and the ticket puts it next to `_callers_through_bases`.
- The row's `why` names the sibling through the existing `_hierarchy_note` key, which `confidence._why` appends to a name-matched row's sentence ("matched by name (suffix_match); the backend bound the call `s.area` to `Circle.area` …"). `confidence.py` is not on Touches. The key's name is about `self.m()`, but `_why` does nothing else with it.
- Siblings exclude the target's own bases (their callers are already listed as "via" rows) and Python Protocols (`_protocol_bases` handles those).
- Siblings run only when the direct answer has exactly one method symbol with rows (`direct.count == 1`). With no direct rows, `_callers_only_through_bases` would print sibling rows under its "written against a base type" heading, which would be false.
- A receiver call needs call text that names a receiver (positive evidence only). A row with no recorded `callee` is not used.
- The TS interface check lists every `Interface` node (up to the lookup row limit), not only the implemented ones. The ticket counts an interface the class satisfies by structure as well, and the budget bounds the reads.

**Shortcuts taken**
- `interface_declares` is a regex plus brace matching. Braces in strings or comments can throw it off, `type X = { m(): … }` aliases are not read, and a parameter named `m` inside a member's signature counts as a declaration (that error is conservative). Worth tree-sitter if false gaps show up.
- On large TS projects, listing every interface can spend the read budget, and the answer then carries a "could not check" gap. That is conservative but noisy. If it shows up, narrow the list to implemented interfaces plus the class's own file.
- A rows-only answer (no direct callers) gets no sibling check (see Decisions). It is already not marked safe, because it has no rows.

**For later tickets**
- `plans/dogfood-fixes` 04-06 can build their "Callers through `<Iface>.<m>`" section on `_interfaces_declaring` (it already finds the interface and its file).
- 13: on eb0902f the new `interface_declares` test fails with ImportError, and the provider tests fail on assertions.

