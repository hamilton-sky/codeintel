# Make `chain A->B` resolve B and return the path, no-path-found, or B not found

Type: task
Status: resolved
Blocked by: 01
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/engines/graph/ops.py, src/codeintel/core/provider.py, docs/graph.md, README.md, tests/test_chain_destination.py, tests/test_graph_provider.py, tests/test_graph_real.py, tests/test_summary_integrity.py

This ticket fixes T3: findings `chain-ignores-destination` (both surfaces) and `chain-no-resolution-labels`. `GraphOps._op_chain` does `target.split("->")[0]` and drops B.

1. When the target contains `->`, split it into A and B, and strip a `name@file` hint from each, as today.
2. Resolve B with the same `_SymbolTarget` path `callers` uses, by asking the backend's `search_graph` for the name.
   - If B is not found, set `self._pending_null = ("target-not-found", "<B> is not in the graph index …")` and return `None`.
   - Add `target-not-found` to the `not_found_reasons` set in `core/provider.safe_null_result`.
3. Call `trace_path` from A with `direction: "outbound"` and depth ≤ 5. Then run a BFS over the returned hops to find B's qualified name. If the 0.9.x payload has no parent links, rebuild the path from `hop` numbers and the caller/callee pairs; if no path can be rebuilt, treat it as no path.
   - **Path found.** Render `## Path A → B` with the ordered hops.
   - **No path.** Render "No call path from A to B within N hops (this is not proof none exists)" and add the `no-path-found` gap.
   - **B ambiguous.** List the candidates and claim no path.
4. In `_fmt`, a hop with an empty `strategy` prints `[unstated]` and adds one `hop-resolution-unstated` gap per answer, which caps the answer at partial. This applies to bare `chain A` answers too.
5. **Register the new summary in the census.** `tests/test_summary_integrity.py::_summary_sites` treats a markdown heading containing `→` as count-bearing (`_HEADING_COUNT`), and "within N hops" is a tally phrase (`_TALLY`, unit `hop`). So `## Path A → B` and the no-path line are new census sites keyed by their enclosing qualname (for example `("count", "GraphOps._op_chain")` and `("tally", "GraphOps._op_chain")`, or the helper they are built in). Without entries `test_every_summary_site_has_a_verifier` fails.
   - Add one `_VERIFIED_BY` entry per new key. Its value names the referent and the verifier test: the hops listed under `## Path A → B` are a connected chain from A's qualified name to B's (each hop's callee is the next hop's caller), and N in "within N hops" is the depth that was actually sent to `trace_path`.
   - Write that verifier test (in the new `tests/test_chain_destination.py`; the census finds test names in sibling `test_*.py` files) so it asserts against the stub's payload and the request it captured, not against the rendered text alone.
   - Run `tests/test_summary_integrity.py` and register exactly the keys it reports, no more: `test_the_registry_has_no_entries_for_summaries_that_no_longer_exist` fails on an entry the census does not find.
6. Fix the docs:
   - `docs/graph.md` § `chain` detail: remove the false "falls back to `impact`" sentence and describe A->B.
   - The `chain` row in its op table.
   - README's `chain` row ("ordered hops, each labelled with how it was resolved"), which must say unlabelled hops show `[unstated]`.

## Not in this ticket

- The "Did you mean" for a misspelled A or B is ticket 11 (hints).
- `chain`'s MCP tool description already says "call path between two symbols", which becomes true. No change is needed in `src/codeintel/transports/mcp.py`.

## Done when

- New `tests/test_chain_destination.py` uses a stub `_run` that returns 0.9.0-shaped `trace_path` payloads (no `strategy`). These fail on main and pass here:
  - reachable B gives `## Path` with the ordered hops
  - unreachable B gives the `no-path-found` gap and `confidence: partial`
  - missing B gives reason `target-not-found` and outcome `not_found`
  - hops without `strategy` print `[unstated]` and add the `hop-resolution-unstated` gap
- Existing chain tests are updated only where they pinned the dropped-B behaviour or an unlabelled hop: in `tests/test_graph_provider.py`, and in `tests/test_graph_real.py` `test_chain_parses_trace_path` and `test_chain_carries_risk_labels` (their 0.9.0 hops carry no `strategy`, so they now also expect `[unstated]` and the `hop-resolution-unstated` gap; their existing `[hop 1]` and `[risk: …]` assertions stay). `.venv/bin/pytest tests/test_chain_destination.py tests/test_graph_provider.py tests/test_graph_real.py tests/test_summary_integrity.py tests/test_wire_text.py tests/test_docs_trust_claims.py tests/test_docs_deadcode_withdrawal.py -q -o addopts=` passes (the last three, which read `wire_text`'s `trace_path` parse and the docs this ticket edits, are not on Touches and pass unchanged).
- `tests/test_summary_integrity.py` passes with the new `_VERIFIED_BY` entries and their verifier.
- `grep -n "falls back to" docs/graph.md` no longer finds a `chain` sentence (the reviewer checks this). All existing tests still pass.

```check
$ .venv/bin/pytest tests/test_chain_destination.py tests/test_graph_provider.py tests/test_graph_real.py tests/test_summary_integrity.py tests/test_wire_text.py tests/test_docs_trust_claims.py tests/test_docs_deadcode_withdrawal.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "An agent tracing a path"
- redblue-fixplan.md § T3
- src/codeintel/engines/graph/targets.py (`_SymbolTarget`)

## Answer

Work started at commit bf3c5b865e2337f0b170e4961d96b11acd89afb3. Test-only commit: 08780a3.

**Built**
- `src/codeintel/engines/graph/ops.py`: `_op_chain` sends a target containing `->` to the new `GraphOps._chain_between`; the bare form is unchanged apart from its hop badges. New helpers: `_chain_badges` (the `[hop N] [how]` badges, `[unstated]` when `strategy` is empty, `[risk: …]` still printed when there is no class), `_unstated_hops_note` (one `hop-resolution-unstated` gap per answer, section `chain`, plus a `> Resolution unstated: …` line in the body), `_chain_endpoint` (resolves one end: `search_graph` with `name_pattern: ^<re.escape(name)>$`, narrowed by `_parse_symbol_target(...).matches`), `_rebuild_chain` (walks back from B over `query_graph` caller/callee edges among the traced nodes), and the `_CHAIN_DEPTH = 5` constant.
- `_chain_between`: an empty side of `->` gives `no-target`. A or B not found gives `target-not-found` naming that end. An end with several matches lists the candidates (with their files) and says "No path is claimed". Otherwise it sends `trace_path` from A's qualified name with `direction: outbound, depth: 5` and renders `## Path A → B` with one `- caller → callee [hop N] [how]` line per hop, or "No call path from A to B within 5 hops (this is not proof none exists)." plus a `no-path-found` gap.
- `src/codeintel/core/provider.py`: added `target-not-found` to `not_found_reasons`.
- `docs/graph.md`: the op-table `chain` row and the `### chain detail` section now describe A->B (resolution, outbound depth 5, rebuild from edges, the three outcomes, `[unstated]`). The "falls back to `impact`" sentence is gone.
- `README.md`: the `chain` row names `"A->B"` and says unlabelled hops show `[unstated]`.
- `tests/test_chain_destination.py` (new, 15 tests), `tests/test_graph_real.py` (`test_chain_parses_trace_path` and `test_chain_carries_risk_labels` now also expect `[unstated]` and the gap; their `[hop 1]` and `[risk: …]` assertions are unchanged), `tests/test_summary_integrity.py` (two `_VERIFIED_BY` entries).

**Proof**
- New tests fail on main and pass here. At the test-only commit 08780a3, `.venv/bin/pytest tests/test_chain_destination.py -q -o addopts=` gave 12 failed, 1 passed. Each failure was the bug itself: the dropped-B neighbourhood `## Call chain for main …` printed where `## Path` or "No call path" was expected; `result` was not None for a missing B (no `target-not-found`); hops had no `[unstated]`. The one that passed is the 0.10.x control (`test_a_hop_with_a_strategy_is_not_unstated`). `test_graph_real.py`'s two chain tests failed there on the new `[unstated]` assertions. Now: all 15 pass.
  - reachable B: `## Path main → validate`, hops `main→load→parse→validate` in order (`test_a_reachable_destination_gives_the_path_with_its_hops_in_order`).
  - unreachable B: the no-path line, `no-path-found`, `confidence: partial` (`test_an_unreachable_destination_is_no_path_found_and_partial`).
  - missing B: `reason: target-not-found`, `outcome: not_found`, and no `trace_path` call (`test_a_missing_destination_is_target_not_found`).
  - unstated hops: every hop line has `[unstated]` and there is exactly one `hop-resolution-unstated` gap, for both `A->B` and a bare `chain A` (`test_hops_without_a_strategy_print_unstated_and_add_one_gap`, `test_a_bare_chain_also_labels_unstated_hops`).
- Check block: `.venv/bin/pytest tests/test_chain_destination.py tests/test_graph_provider.py tests/test_graph_real.py tests/test_summary_integrity.py tests/test_wire_text.py tests/test_docs_trust_claims.py tests/test_docs_deadcode_withdrawal.py -q -o addopts=` gave `242 passed, 1 skipped, 1 xfailed`, exit 0. `test_wire_text.py`, `test_docs_trust_claims.py` and `test_docs_deadcode_withdrawal.py` are unchanged. `test_graph_provider.py` needed no change: its chain case stubs `_dispatch`.
- Census: `tests/test_summary_integrity.py` passes. Without the entries it reported exactly `count:GraphOps._chain_between` (`'## Path {} → {}'`) and `tally:GraphOps._chain_between` (the no-path line and the gap detail), and those are the two keys registered. Their verifier is `test_the_path_hops_form_a_connected_chain_and_n_is_the_depth_sent`. It checks the rendered hops against the stub's `EDGES` (each hop is an edge, consecutive, starting at A's qualified name and ending at B's) and N against the `depth` in the captured `trace_path` request.
- `grep -n "falls back to" docs/graph.md` finds only line 376 (a `_SymbolTarget` file-stem sentence), no `chain` sentence.
- Existing tests: the whole Test command is left to the conductor's gate. I also ran `test_architecture test_backend_failure_detail test_graph_failure_population test_null_reason_and_ripple test_mcp_server test_corpus test_dispatch_callers test_edge_staleness test_gateway test_cli_commands` (309 passed, 26 skipped) and `test_cache test_incompleteness test_integration test_missing_target test_never_raise test_cold_process` (105 passed, 2 skipped). Ruff, mypy (87 files), compileall and the c4 check (0 gating findings) are all clean.
- Acceptance `plans/trust-fixes/acceptance/test_tf_chain.py`: 7 passed.
- Live, codebase-memory-mcp 0.10.8 on this repo:
  - `codeintel query --op chain --target "GraphOps._op_chain->BackendClient._run"` gave `## Path …` with 2 hops (`[?name-guess]`, then `[lsp]`), confidence complete.
  - `->safe_null_result` gave the no-path line, `no-path-found`, partial.
  - `->nope_zz` gave `target-not-found` / `not_found`.
  - `->_run` listed 7 candidates and claimed no path.

**Decisions**
- How the path is rebuilt. I checked 0.9.0 and 0.10.8 directly: both `trace_path` payloads carry a `hop` per node and no parent link. So `_rebuild_chain` walks back from B with one `query_graph` per step. Callees must be in the frontier; callers must be traced nodes with a hop below B's, or A itself. The first time it reaches A, it has a shortest path among the traced nodes. If the edges do not connect A to B, the answer is "no path" (the ticket's rule).
- How hops are labelled. A path hop is labelled by its own edge's `c.strategy` (0.9.0 edges carry one: `suffix_match`, `lsp_callable_alias`, …). The traced node's `strategy` is the fallback. So on a live 0.9.x index, path hops are usually labelled, not `[unstated]`. The bare `chain A` form has no edges to read, so on 0.9.x every hop there is `[unstated]`.
- A is resolved too, by the same lookup as B. `trace_path` then gets A's full qualified name (checked to work on 0.9.0), and a namesake cannot be traced. A missing A also gives `target-not-found`; an ambiguous A lists its candidates.
- The heading and the no-path line name A and B as the caller wrote them (`## Path entry → Cache.save`), which is what the acceptance tests ask for. Hop lines give full qualified names.
- An empty side (`A->` or `->B`) gives `no-target` with a hint naming the missing side.
- Gap sections are the constant `chain`, per learnings (02).

**Shortcuts taken**
- The rebuild sends every traced node with a hop below B's in an `IN [...]` list. For a hub A at depth 5 that list could hold thousands of names, so the query would be large and slow. Worth upgrading (for example, filtering to the BFS layer `hop == step`) if `chain A->B` proves slow on big repos.
- The ends are looked up with `search_graph` `limit: 50`. A name with more than 50 namesakes (after narrowing) would list only the first 50 as candidates.
- A recursive `chain A->A` answers no path: the trace does not list A as its own callee.

**For later tickets**
- Ticket 11 (hints): `target-not-found` comes from `GraphOps._chain_between`. Its hint names the end that is missing, and that is where a "Did you mean" would go (the `_chain_endpoint` result is empty there).
- Ticket 13 (CHANGELOG): `chain A->B` now honours B, has the new reason `target-not-found` and the gaps `no-path-found` / `hop-resolution-unstated` (section `chain`), and now calls `search_graph` and `query_graph` besides `trace_path`.
