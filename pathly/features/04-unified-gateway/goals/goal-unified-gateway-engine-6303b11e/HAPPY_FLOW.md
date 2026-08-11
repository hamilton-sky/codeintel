# Happy Flow — F4: Unified Gateway (Engine Selector)

## Phase 1 — SemanticProvider placeholder

1. Builder creates `src/codeintel/providers/semantic.py`.
2. `SemanticProvider` instantiates without error.
3. Any `build_result()` call returns `{ok:True, result:None, engine:"semantic", reason:"engine-unavailable", cached:False}`.
4. Import from `codeintel.providers.semantic` resolves cleanly.

## Phase 2 — Engine-aware gateway router

1. Builder rewrites `Gateway.__init__` to accept `graph`, `lsp`, `semantic` provider slots.
2. `gateway.query(op="impact", target="foo", engine="graph")` → resolves to `GraphProvider.build_result(...)` → returns graph result.
3. `gateway.query(op="symbol", target="Bar", engine="lsp")` → routes to `LspProvider`.
4. `gateway.query(op="search", target="auth code", engine="semantic")` → routes to `SemanticProvider` → safe-null (unavailable).
5. `engine="auto"` with `op="impact"` → consults `_AUTO_ENGINE` table → routes to graph.
6. `engine="auto"` with `op="symbol"` → routes to lsp.
7. Unavailable engine → `reason="engine-unavailable"`, not an exception.

## Phase 3 — Wire all providers into server

1. `_build_gateway()` instantiates all three providers inside try/except.
2. Returns `Gateway(graph=gp, lsp=lp, semantic=sp)`.
3. `code.query` tool call with `engine="graph"` → `gw.query(..., engine="graph")` → graph result.
4. `code.status` reports `{"engines": ["graph", "lsp"], "semantic": false, ...}` matching installed state.

## Phase 4 — Fan-out merge in gateway

1. `gateway.query(op="impact", target="foo", engine="both")`.
2. Gateway spawns two threads: one calls `graph.build_result(...)`, one calls `lsp.build_result(...)`.
3. Graph returns `## Callers of foo\n- bar (a.py)`.
4. LSP returns null (unsupported op).
5. Merge: only graph is non-null → result = `## [graph]\n## Callers of foo\n- bar (a.py)`.
6. Response: `{ok:True, result:"...", engine:"both", cached:False}`.

## Phase 5 — Content-hash cache

1. `ContentHashCache.get("impact", "foo", "graph", "/repo")` on empty cache → `None`.
2. `ContentHashCache.put(...)` stores the result keyed by `(op, target, engine, project_root, content_hash)`.
3. Second `get(...)` with same content hash → returns cached `Result`.
4. File edited (new content hash) → `get(...)` returns `None` (cache miss).
5. Null results are not stored — `put` silently skips them.

## Phase 6 — Wire cache into gateway

1. Agent calls `gateway.query(op="impact", target="foo", engine="graph")` — cache miss.
2. GraphProvider called; result returned with `cached=False`; result stored in cache.
3. Agent calls the same query again (file unchanged) — cache hit.
4. Result returned with `cached=True`; GraphProvider NOT called.
5. Agent edits the target file; same query again — cache miss; GraphProvider called again.

## Phase 7 — Role/op tiering policy

1. `TieringPolicy(enabled=False)` created — `is_allowed(any_role, any_op)` → `True`.
2. `TieringPolicy(enabled=True, rules={"builder":["impact","callers"]})` created.
3. `is_allowed("builder","impact")` → `True`.
4. `is_allowed("builder","symbol")` → `False`.
5. `is_allowed("other_role","symbol")` → `True` (role not in rules → permissive).

## Phase 8 — Wire tiering into gateway + server

1. Agent calls `code.query` with `role="builder"` and tiering off → op proceeds normally.
2. Harness enables tiering by constructing gateway with `TieringPolicy(enabled=True, rules={...})`.
3. Agent call with `role="restricted"` and a disallowed op → `{ok:True, result:null, reason:"op-not-allowed-for-role"}`.
4. Provider is never invoked for disallowed ops.
5. `role=""` or `role=None` with tiering on → permissive (no rule for empty role).

## Phase 9 — Gateway tests

1. Test runner discovers `tests/test_gateway.py`.
2. All 13 test cases pass.
3. No regressions in `test_never_raise.py`, `test_graph_provider.py`, `test_lsp_provider.py`.
4. Test output shows 0 errors, 0 failures.
