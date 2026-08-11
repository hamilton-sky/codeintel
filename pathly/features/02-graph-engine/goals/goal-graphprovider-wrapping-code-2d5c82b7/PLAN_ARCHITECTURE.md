# Plan Architecture — GraphProvider (F2)

This file maps design decisions to implementation phases. The authoritative project design is in
`pathly/project/SPEC.md` — this file captures F2-scoped decisions only.

---

## Design Decisions

### D1 — Subprocess CLI bridge (not MCP client)

`GraphProvider` shells out to `codebase-memory-mcp cli <method> '<json>'` via `subprocess.run()`
rather than launching a full MCP session.

**Rationale:** MCP session management (protocol handshake, persistent connection) is F3's job (LspProvider pattern). A CLI bridge is simpler, stateless, and straightforward to deadline-bound. The `codebase-memory-mcp` CLI is already documented and tested in the skill protocol.

**Trade-off:** Each call spawns a process. For F2's use pattern (one-shot queries) this is acceptable. If query frequency becomes a bottleneck, a persistent session can be added in F4 without changing the provider protocol.

### D2 — Project-name resolution via `list_projects`

Rather than requiring the caller to pass a project name, `GraphProvider` resolves it from `project_root` by calling `list_projects` and matching on `root_path`.

**Rationale:** The `CodeProvider` protocol only carries `project_root` — no project-name field. Auto-resolution keeps the public contract clean and consistent with how LSP and semantic providers will be configured.

**Trade-off:** One extra subprocess call per unique `project_root`. Mitigated by `self._project_cache` (in-process, lifetime of the provider instance).

### D3 — Graph ops use Cypher via `query_graph`

`callers`, `callees`, and `impact` use `query_graph` with explicit Cypher rather than a higher-level `search_graph` call.

**Rationale:** `query_graph` gives precise control over traversal depth and result shape. `search_graph` is name-pattern search; it doesn't return caller/callee edges. Cypher queries are explicit about what graph relationship (`CALLS`) is traversed.

**Trade-off:** Couples `GraphProvider` to the `query_graph`/Cypher API. If the backend changes its query interface, only `GraphProvider` needs updating (not the gateway or server).

### D4 — Safe-null hierarchy

Three distinct `reason` values for safe-nulls:
- `"engine-unavailable"` — binary not on PATH.
- `"project-not-indexed"` — binary present but repo not in the project list.
- `"timeout"` — subprocess exceeded budget.
- `"unsupported-op"` — op string not in dispatch table.

**Rationale:** The agent needs to distinguish "install the backend" from "run `index` first" from "query timed out" — different recovery actions. Generic `"no-result"` obscures this.

### D5 — Default timeout: 5000 ms when budget=0

**Rationale:** Zero budget from the protocol means "caller didn't specify". Treating it as 0 ms would kill every subprocess instantly. 5000 ms is a safe default (graph queries on medium repos are typically <1 s, 5 s is headroom for cold-start subprocess overhead).

---

## Phase Mapping

### Phase 1 — Backend detection + subprocess runner
- Implements D1 (subprocess bridge) and D2 (project-name resolution).
- Key types: `GraphProvider` class, `_run()` helper, `_resolve_project()` cache.

### Phase 2 — Op dispatch: impact, callers, callees, chain
- Implements D3 (Cypher via `query_graph`) and D4 (reason hierarchy for op results).
- Key types: `_op_callers`, `_op_callees`, `_op_impact`, `_op_chain` helpers.

### Phase 3 — Op dispatch: pattern, overview + build_result wiring
- Completes D4 (unsupported-op safe-null) and D5 (default timeout).
- Full `build_result` dispatch table. Pattern uses `search_code`; overview uses `get_architecture`.

### Phase 4 — Gateway project_root passthrough
- Ensures `project_root` from the MCP call reaches `GraphProvider._resolve_project()`.
- No new architectural decisions — verifies existing wiring in `gateway.py`.

### Phase 5 — Server auto-include + code.status update
- `_build_providers()` encapsulates the detection-and-registration logic in `server.py`.
- `code.status` reflects live `GraphProvider.available` check.

### Phase 6 — Tests
- Fault-injection coverage for D1–D5: every safe-null path exercised without real backend.
