# Make graph hints name only tools the agent has, suggest close names, and stop leaking project ids

Type: task
Status: open
Blocked by: 02, 04, 07
Test first: yes
Level: 3
Builder: backend
Touches: src/codeintel/engines/graph/provider.py, src/codeintel/engines/graph/backend.py, tests/test_missing_target.py, tests/test_graph_provider.py, tests/test_null_reason_and_ripple.py, tests/test_backend_failure_detail.py, tests/test_agent_hints.py

This ticket fixes T9: `hints-name-tools-agent-lacks`, `no-edges-hint-garbled-and-wrong-op`, and the hint half of `symbol-dead-without-lsp`. It also fixes the engine half of T7, `backend-error-hint-leaks-other-projects`. It is level 3 because a scoped HTTP caller can read other tenants' project ids today.

1. **Not-in-graph hint** (`GraphProvider.build_result`):
   - Remove the `index_repository` advice and its comment block. Say "refresh with: `codeintel index <root>` (or ask the user to run it)".
   - Add did-you-mean: one backend name search (`_search_symbols` with a name pattern) for names within edit distance 2 or sharing a prefix, using `difflib.get_close_matches`. List up to 5.
   - The did-you-mean lookup is best effort: when the backend fails or times out on it, the hint simply has no suggestions. It never changes the envelope's `reason`, and it never puts "is not in the graph index" into a backend-failure answer (`tests/test_graph_failure_population.py::test_no_graph_op_reports_a_backend_failure_as_an_absence` and `tests/test_cold_process.py`, both unchanged).
   - Rewrite `tests/test_missing_target.py`'s two `index_repository` tests (`test_the_reindex_hint_names_the_mcp_tool_as_well_as_the_cli` and `test_the_mcp_remediation_does_not_hand_back_a_path_no_shell_will_expand`) to pin the new wording: the hint names `codeintel index <root>` and no `index_repository`, and the MCP-facing part still carries no `~` path.
2. **Backend text passthrough** (in `src/codeintel/engines/graph/backend.py`, where "the graph backend refused to run" is built, and in any hint that echoes backend text):
   - Rewrite `search_graph(...)` / `trace_path` mentions as `code.query op=pattern target=<name>`.
   - Strip any JSON list of project ids (the `[...],"count":N` shape) and any flattened absolute path.
   - Map backend "project not found" text to reason `project-not-indexed`.
3. **`_dependency_kinds`.** Accept only kinds in `_EDGE_KINDS`, excluding `DEFINES` and `CONTAINS`. When nothing remains, drop the "Other relationships DO point at it" sentence.
4. **The no-edges hint is op-specific.**
   - `callers` keeps the dead-code caveat.
   - `callees`, `impact` and `context` say "no outgoing calls resolved; aliased imports and dynamic calls are invisible to the graph" and do not mention dead code.
5. **Keep the summary census unchanged.** `tests/test_summary_integrity.py` fails on new hint text that is a tally phrase (a count followed by a unit noun such as `symbol`, `file`, `project`) or a heading with `→` or `(N …)`. List the close names without counting them ("did you mean: `a`, `b`"). That file is not on Touches; the check block runs it.
6. **`_suggest_op`.**
   - Exclude the requested op, withdrawn ops and alias ops (`changes`).
   - For an op the graph does not serve (`symbol`, `search`), the hint says "`symbol` is answered by engine=lsp; with LSP down use op=context or op=pattern". For `search` it names engine=semantic.
   - Remove `changes` from the "ops:" list the hint prints.

## Not in this ticket

- The `chain` destination logic is ticket 04. This ticket only changes hint text that `chain` may surface.
- The HTTP-only request-id message is dropped. The engine strip covers every transport.

## Done when

- New `tests/test_agent_hints.py` fails on main and passes here:
  - not-in-graph `compact_for_mcpp` lists `compact_for_mcp` and never names `index_repository`, `search_graph` or `trace_path`
  - a backend-error whose stderr holds `["a-b-c","d-e-f"],"count":17` has neither id in the hint
  - backend "project not found" gives `project-not-indexed`
  - `--engine graph --op symbol` gives a hint without "did you mean symbol" and without `changes`
  - a no-edges `callees` hint has no "dead code" and no `1 1`, while `callers` keeps the caveat
- `tests/test_graph_provider.py`'s `_suggest_op` tests still pass, and `.venv/bin/pytest tests/test_agent_hints.py tests/test_missing_target.py tests/test_graph_provider.py tests/test_null_reason_and_ripple.py tests/test_backend_failure_detail.py tests/test_graph_failure_population.py tests/test_summary_integrity.py tests/test_docs_deadcode_withdrawal.py tests/test_doctor.py tests/test_dispatch_callers.py -q -o addopts= --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read` passes. The last five files are not on Touches and pass unchanged (`tests/test_doctor.py` builds a refusing-backend detail with "refused to run"; `tests/test_docs_deadcode_withdrawal.py` checks how withdrawn ops are named). All existing tests still pass.
- `grep -rn "index_repository" src/codeintel/engines/graph/provider.py` finds nothing (the reviewer checks this). The backend method name in `src/codeintel/gateway/reindexer.py` and `c4` is a real backend call and stays.

```check
$ .venv/bin/pytest tests/test_agent_hints.py tests/test_missing_target.py tests/test_graph_provider.py tests/test_null_reason_and_ripple.py tests/test_backend_failure_detail.py tests/test_graph_failure_population.py tests/test_summary_integrity.py tests/test_docs_deadcode_withdrawal.py tests/test_doctor.py tests/test_dispatch_callers.py -q -o addopts= --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
prints passed
```

## Reference

- spec.md, the stories "An agent following hints" and "An operator running the HTTP server with RBAC"
- redblue-fixplan.md § T9 and § T7, first bullet

## Answer

Work started at commit dcbb275f0061ce993d1db4c54acaf636a58b07be. Test-only commit: f0edf31.

**Built**
- `src/codeintel/engines/graph/backend.py`: `_agent_facing(text)` is applied in `_stderr_tail` before the 400-character cap, so every `BackendRefused` message and detail ("the graph backend refused to run …") is cleaned. That covers the doctor row and every hint that quotes it. It does three things:
  - removes the `"available_projects":[...],"count":N` list. It also catches the cut-off form `…x","y"],"count":N` that has no opening `[`.
  - replaces any quoted flattened path (`"a-b-c"`, two or more hyphens) with `"<project>"`.
  - rewrites `search_graph(name_pattern="<.*>name<.*>")` to `code.query op=pattern target=name`. Any other `search_graph` becomes `code.query op=pattern`, `trace_path` becomes `code.query op=chain`, and `index_repository` becomes `codeintel index`.
  New public helper `is_project_not_found(message)`.
- `src/codeintel/engines/graph/provider.py`:
  - not-in-graph hint: "`X` is not in the graph index for this project. Did you mean: `a`, `b`? If you just added or renamed it, refresh with: `codeintel index <root>` (or ask the user to run it)." The `index_repository` advice and its comment block are gone. The new `_did_you_mean` makes one `_search_symbols` call with `name_pattern` = the two halves of the name OR-ed together (escaped) and `limit` 200. `_close_names` (`difflib.get_close_matches`, cutoff 0.75, then names where one is a prefix of the other) picks up to 5. When the search fails or comes back empty, the hint has no names, and the reason never changes.
  - A backend failure whose message says "project not found" gives `project-not-indexed` with `run: codeintel index <root>  (or: codeintel doctor)`.
  - `_dependency_kinds` keeps only kinds in `_EDGE_KINDS` that are not structural (`CONTAINS` was added to `_STRUCTURAL_EDGES`). When nothing is left, the "Other relationships" sentence is not printed (that path already existed).
  - no-edges hint: `callers` keeps the dead-code caveat. Every other op (callees, impact, context, chain) says "no outgoing calls resolved; aliased imports and dynamic calls are invisible to the graph, so this is not proof that it calls nothing."
  - New `_ALIAS_OPS = {"changes"}`, `_known_ops()` and `_OTHER_ENGINE_OPS`. `_suggest_op` leaves out the op that was asked, withdrawn ops and aliases. The "ops:" list leaves out `changes`. For `symbol`: "`symbol` is answered by engine=lsp, not the graph; with LSP down use op=context or op=pattern." For `search`: engine=semantic, "use op=pattern for a name search".
- Tests:
  - new `tests/test_agent_hints.py` (15 tests; the stderr tests drive a real `#!/bin/sh` fake backend).
  - `tests/test_missing_target.py`: the two `index_repository` tests are rewritten. They now check `codeintel index <root>` plus "ask the user to run it", no `index_repository`, and no `~` after the MCP-facing remedy.
  - `tests/test_null_reason_and_ripple.py::test_no_edges_names_the_relationships_that_do_point_at_the_symbol` now uses `callees` and expects `4 CALLS`, with no DECORATES/TESTS/DEFINES/`1 1`.

**Proof**
- Fails on main, passes here. At f0edf31 (tests only), `.venv/bin/pytest tests/test_agent_hints.py tests/test_missing_target.py tests/test_null_reason_and_ripple.py -q -o addopts=` gave 17 failed, 52 passed. Every failure was the old behaviour:
  - the hint named `index_repository` and offered no `compact_for_mcp`.
  - `a-b-c`/`d-e-f` appeared in the hint.
  - `backend-error` where `project-not-indexed` was expected.
  - `search_graph(` was passed through.
  - "did you mean symbol? ops: … changes …".
  - "dead code" and "1 1"/"2 CONTAINS" appeared in callees/impact/context hints.
  After the change: 69 passed. Done-when sub-bullets, each a test in `tests/test_agent_hints.py`:
  - `compact_for_mcpp` lists `compact_for_mcp` with no `index_repository`/`search_graph`/`trace_path` (`test_a_typo_lists_the_close_name_and_names_no_backend_tool`).
  - stderr `["a-b-c","d-e-f"],"count":17`, whole and cut off: neither id appears (`test_a_backend_error_never_carries_other_projects_ids`, 2 cases).
  - "project not found" gives `project-not-indexed` (`test_a_backend_project_not_found_is_project_not_indexed`).
  - `symbol`: no "did you mean symbol", no `changes`, names engine=lsp/op=context/op=pattern (`test_graph_symbol_points_at_lsp_not_at_itself`).
  - no-edges `callees`/`impact`/`context`: no "dead code" and no `1 1`; `callers` keeps the caveat (`test_a_no_edges_hint_off_callers_…`, `test_a_no_edges_callers_hint_keeps_the_dead_code_caveat`).
- Check block (the exact `$` command): `405 passed, 1 deselected, 1 xfailed`, exit 0. `test_graph_provider.py`'s `_suggest_op` tests pass, and the five files off Touches are unchanged.
- `grep -rn "index_repository" src/codeintel/engines/graph/provider.py`: no output, exit 1.
- Existing tests: the whole Test command is left to the conductor's gate. I also ran 22 neighbouring files (test_changed_range, test_cli_commands, test_cold_process, test_corpus, test_edge_confidence, test_gateway, test_graph_real, test_incompleteness, test_lsp_provider, test_mcp_server, test_search_fallback, test_semantic_provider, test_chain_destination, test_edge_staleness, test_caller_counts, test_never_raise, test_architecture, test_docs_trust_claims, test_wire_text, test_redaction_boundary, test_rbac, test_graph_stdin): 745 passed, 28 skipped, exit 0.
- `ruff check src tests bench`: all checks passed. `mypy`: no issues in 88 files. compileall: ok. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: 0 gating findings.
- Live on this repo (codebase-memory-mcp 0.10.8):
  - `codeintel query --op callers --target compact_for_mcpp --engine graph` gave "… Did you mean: `compact_for_mcp`? If you just added or renamed it, refresh with: `codeintel index /home/user/codeintel` (or ask the user to run it)."
  - `--op symbol --engine graph` gave the engine=lsp hint, and its ops list has no `changes`.
  - `BackendClient._run('query_graph', {project: 'nope-zz'})` detail: `{"error":"project not found or not indexed",…,<project list withheld>}`.
  - `trace_path` of `other to helper2`: "Use code.query op=pattern target=other to helper2 to find the exact name, then pass it to code.query op=chain."
- Acceptance `test_tf_hints.py` and the `test_tf_http.py` project-deleted test cannot run here. Their fixtures index into a private HOME, and the running backend daemon refuses that ("the active account daemon uses a different cache directory", learnings 05). The fakegraph case `test_graph_engine_symbol_hint_points_at_lsp_and_context_or_pattern` failed before this change on "did you mean symbol"; this change targets exactly that.

**Decisions**
- The strip runs in `_stderr_tail`, before the cap, not on the capped message in `_refused`. On a machine with many projects, cutting first kept only the tail of the list and lost the "project not found" words at the front. That is why the redblue repro read `backend-error`, not `project-not-indexed`. The cut-off-list regex stays as a second line of defence.
- "Flattened absolute path" means a quoted token with two or more hyphens. Unquoted absolute paths are not stripped: `test_backend_failure_detail.py::test_a_home_directory_in_the_backends_stderr_is_redacted` requires `~/.cache/codebase-memory-mcp/graph.db` to stay in the doctor detail.
- `_dependency_kinds` follows the ticket literally: only `_EDGE_KINDS` (CALLS, USAGE, CALL_REFERENCE, IMPORT). DECORATES/TESTS/INHERITS are no longer named. The existing test that pinned "3 DECORATES" was changed to match.
- `chain` and `context` get the outgoing-calls wording, because `chain` traces outbound. Only `callers` keeps the dead-code caveat.
- Did-you-mean search pattern: the two halves of the name OR-ed, so one typo in either half still finds the name. Names of 3 characters or fewer are searched whole.

**Shortcuts taken**
- Flattened-path detection is a heuristic (quoted, at least two hyphens). It misses a one-hyphen slug such as `"home-x"` and would also hide a quoted hyphenated non-path. It is worth replacing with an exact check against the known project list if a leak shows up in another shape.
- A name with typos in both halves (edit distance 2 split across the halves) is not found by the one search. The ticket allows one search, and this is best effort.
- The did-you-mean search uses the answer's full `timeout_ms`, so a not-in-graph answer on a slow backend can take one extra round trip.
- "project not found" maps to `project-not-indexed` only on the null-answer path. If one half of `impact` hits it while the other answers, it stays a `backend` gap. Its text is already stripped.

**For later tickets**
- `src/codeintel/engines/graph/ops.py:518` (ticket 04's `chain` `target-not-found` hint) still names `index_repository`. ops.py is not on this ticket's Touches. Ticket 15 finding 3 already covers it, and the fix is "`codeintel index <root>` (or ask the user to run it)". After that, the spec line "No hint names `index_repository`" holds everywhere.
- Ticket 13 (CHANGELOG): did-you-mean on not-in-graph; backend stderr stripped of project lists and flattened paths, and its tool names rewritten; "project not found" now gives `project-not-indexed`; op-specific no-edges hint; `_dependency_kinds` limited to `_EDGE_KINDS`; `symbol`/`search` on the graph engine name their own engine; `changes` dropped from the ops list.

## Review findings (round 1, pair review)

QUALITY
1. major tests/test_agent_hints.py: `test_a_failed_name_search_leaves_the_hint_without_names_and_the_reason_alone` checks `"did you mean" not in env["hint"]`, but the code writes " Did you mean: " (capital D, provider.py `_did_you_mean`), so the assertion passes even when suggestions are printed and nothing pins "a failed lookup gives no suggestions". Fix: compare case-insensitively (`not in hint.lower()`) or assert no backticked candidate appears.
2. minor src/codeintel/engines/graph/provider.py: `_did_you_mean` calls `_search_symbols` directly with the full `timeout_ms`, not via `_guarded(..., surface=False)`; a failed suggestion search leaves `_last_failure` set after a not-in-graph answer and costs one extra backend launch of up to a full budget per unknown name. Fix: route through `_guarded(surface=False)` or the remaining scope budget, and restore the earlier failure state.
3. minor src/codeintel/engines/graph/backend.py: `_FLATTENED_PATH` only withholds double-quoted ids; `project home-alice-work-repo not found` and `'home-alice-work-repo'` pass unchanged. No live leak on 0.10.8, but the ticket says "any flattened absolute path". Fix: also match single-quoted and bare hyphen-slug tokens next to "project".
4. minor src/codeintel/engines/graph/backend.py: `_TOOL_NAMES` does not rewrite `list_projects`. Fix: map it to `code.status` or drop it.
5. minor src/codeintel/engines/graph/provider.py: with `resolution.is_ancestor`, the not-in-graph return comes before the "Scope:" note, so did-you-mean names can come from outside the caller's root undisclosed, and a role without `pattern` can list names via typos. Fix: skip suggestions when `resolution.is_ancestor`, or add the scope note.

Probes: target metacharacters/length, op strings, backend stderr shapes (incl. live 0.10.8 reply), ancestor scope: no cross-tenant id read. Done when: 405 passed, 1 deselected, 1 xfailed; no `index_repository` in provider.py; full suite 2379 passed (only the deselected root-only test fails). test_tf_hints.py cannot run here (temp HOME with warm daemon). END 86c2633 = HEAD.

codeintel: changed dcbb275f0061ce993d1db4c54acaf636a58b07be: helped

REVIEW: FAIL

SPEC
- New tests/test_agent_hints.py fails on main and passes here (close name listed without backend tool names; ids withheld; "project not found" -> project-not-indexed; `--op symbol` hint without self-suggestion or `changes`; callees no-edges hint without "dead code" or `1 1`, callers keeps the caveat): MET.
- `_suggest_op` tests pass; ten-file pytest command passes; all existing tests pass: MET (2379 passed, 31 skipped, 1 deselected, 1 xfailed).
- `grep -rn "index_repository" src/codeintel/engines/graph/provider.py` finds nothing: MET.
- scope: OK.
- test first: OK.

Notes: acceptance test_tf_hints.py and one test_tf_http.py test fail because indexing into a temp HOME is refused while the warm daemon runs (environment). Quality note: "Did you mean:" is capitalised, so `"did you mean" not in env["hint"]` in test_a_failed_name_search_leaves_the_hint_without_names_and_the_reason_alone can never fail. END 86c2633 = HEAD. Answer not read.

REVIEW: PASS
