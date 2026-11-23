# Make `chain A->B` resolve B and return the path, no-path-found, or B not found

Type: task
Status: open
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
