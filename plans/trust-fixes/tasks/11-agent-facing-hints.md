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
