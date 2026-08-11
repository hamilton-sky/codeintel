# User Stories — F4: Unified Gateway (Engine Selector)

## Story 1: Engine Selection

**As a** coding agent calling `code.query`,
**I want to** specify `engine=graph`, `engine=lsp`, `engine=semantic`, `engine=both`, `engine=all`, or `engine=auto`,
**So that** I can direct queries to the most appropriate backend without managing multiple tool interfaces.

### Acceptance Criteria
- `engine=graph` routes to GraphProvider only; returns `engine-unavailable` if not installed.
- `engine=lsp` routes to LspProvider only; returns `engine-unavailable` if not installed.
- `engine=semantic` routes to SemanticProvider; returns `engine-unavailable` (placeholder until F5).
- `engine=both` fans out to graph+lsp and merges non-null results.
- `engine=all` fans out to graph+lsp+semantic and merges non-null results.
- `engine=auto` picks the best-available engine for the op (see auto-routing table).
- Omitting `engine` defaults to `auto` behaviour.
- Every path returns the safe-null envelope `{ok, op, target, result, engine, cached, reason?}`; never raises.

## Story 2: Auto-Routing

**As a** coding agent,
**I want** the gateway to automatically select the best engine for my op when I don't specify one,
**So that** I don't need to know which engine handles which op.

### Acceptance Criteria
- `op=impact|callers|callees|chain|pattern` → auto-routes to graph.
- `op=symbol` → auto-routes to lsp.
- `op=search` → auto-routes to semantic.
- `op=overview` → auto-routes to graph, falls back to lsp if graph unavailable.
- `op=context` → auto-routes to both (graph+lsp merged).
- If the auto-selected engine is unavailable, the result is safe-null with `reason=engine-unavailable`.

## Story 3: Fan-Out Merge

**As a** coding agent,
**I want** `engine=both` and `engine=all` to merge responses from multiple engines,
**So that** I get combined structural and semantic intelligence in one response.

### Acceptance Criteria
- `engine=both` collects graph result + lsp result and merges non-null outputs.
- `engine=all` additionally includes semantic result.
- Merge format: results concatenated with engine-labelled headers (e.g. `## [graph]`).
- If only one engine returns data, that data is returned without the other's empty section.
- If all engines return null, the merged result is safe-null.
- The `engine` field in the response reflects the requested engine value (`both` or `all`).

## Story 4: Content-Hash Cache

**As a** coding agent,
**I want** repeated queries on unchanged files served from cache,
**So that** I avoid redundant backend calls when the code hasn't changed.

### Acceptance Criteria
- A repeated identical query returns `cached: true` in the result envelope.
- Editing the target file (changing its content hash) busts the cache for that query.
- Only non-null results are cached.
- The cache is in-process and per-gateway-instance (no persistence across restarts required for v1).

## Story 5: Auto-Backend Detection

**As a** server operator,
**I want** the gateway to detect which engines are available at startup,
**So that** the server works correctly even if some backends are not installed.

### Acceptance Criteria
- Gateway queries which of graph/lsp/semantic are installed during initialisation.
- An unavailable engine returns `reason=engine-unavailable`, never an exception.
- `code.status` reports which engines are available.

## Story 6: Optional Role/Op Tiering

**As a** harness (e.g. Pathly),
**I want** to optionally restrict which ops callers in a given role can execute,
**So that** I can enforce a policy layer while the standalone default remains permissive.

### Acceptance Criteria
- Tiering is **off by default**: all ops are allowed for all callers.
- When enabled, a role→allowed_ops map is consulted before dispatching.
- A disallowed op returns safe-null with `reason=op-not-allowed-for-role`.
- `code.query` accepts an optional `role` parameter; it is ignored when tiering is off.
- Enabling tiering requires explicit config — it cannot happen by accident.
