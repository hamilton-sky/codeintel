# Implementation Plan — GraphProvider (F2)

Rigor: standard | Conversations: 3 | Phases: 6

Do NOT touch `src/codeintel/providers/none.py`, `src/codeintel/provider.py`, or
`tests/test_never_raise.py` during this feature — they belong to F1 and must stay green.

Recovery instruction: If verification fails and the fix requires out-of-scope changes, stop and
report. If fundamentally broken, rollback with `git checkout` on affected files and retry.

---

## Conversation 1 — GraphProvider core

Build the `GraphProvider` class: backend detection, project-name resolution, deadline-bounded
subprocess runner, and all six op implementations. The server is NOT wired yet — this conv ends
with a manually importable, testable provider.

Do NOT touch `server.py` or `gateway.py` in this conversation.

### Phase 1 — Backend detection + subprocess runner

File: `src/codeintel/providers/graph.py`

Done when: `python3 -c "from codeintel.providers.graph import GraphProvider; p = GraphProvider(); print(p.available)"` prints `True` or `False` without raising, and `GraphProvider().build_result('impact', 'fn', [], 0, '/tmp')` returns `{'ok': True, ...}`.

Purpose: Foundation for all graph ops. Establishes the detection probe and the safe subprocess
wrapper that every op will use.

Depends on: `src/codeintel/provider.py` (existing `CodeProvider` protocol + `safe_null_result`).

Enables: All other phases in Conv 1.

Verify:
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "
from codeintel.providers.graph import GraphProvider
p = GraphProvider()
print('available:', p.available)
r = p.build_result('impact', 'gateway', [], 0, '.')
assert r['ok'] is True
print('ok:', r)
"
```

Implementation notes:
- `GraphProvider.__init__`: call `_detect_backend()` → sets `self.available: bool` and
  `self._cmd: str | None` (the path to `codebase-memory-mcp`).
- `_detect_backend()`: use `shutil.which("codebase-memory-mcp")` — returns the path or None.
- `_run(method: str, payload: dict, timeout_ms: int) -> dict | None`: runs
  `subprocess.run([self._cmd, "cli", method, json.dumps(payload)], capture_output=True, timeout=timeout_ms/1000)`.
  Returns parsed JSON or None. Swallows ALL exceptions → caller decides the safe-null.
- `_resolve_project(project_root: str) -> str | None`: calls `_run("list_projects", {}, 3000)`,
  finds the entry whose `root_path` == project_root (or is a parent path match), returns its
  `name`. Returns None if not found or backend unavailable. Cache per `project_root` for the
  lifetime of this provider instance (simple `dict`).
- Default timeout when `budget == 0`: 5000 ms.

### Phase 2 — Op dispatch: impact, callers, callees, chain

File: `src/codeintel/providers/graph.py`

Done when: `GraphProvider().build_result('callers', 'gateway', [], 0, '.')` returns ok=True and,
when the backend IS installed and repo indexed, `result` is a non-empty string containing
caller information.

Purpose: Implement the four structural-traversal ops using the backend's `query_graph` method.

Depends on: Phase 1 (`_run`, `_resolve_project`).

Enables: Story 1 acceptance criteria for impact/callers/callees/chain.

Verify:
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "
from codeintel.providers.graph import GraphProvider
p = GraphProvider()
for op in ['impact', 'callers', 'callees', 'chain']:
    r = p.build_result(op, 'gateway', [], 0, '.')
    assert r['ok'] is True, f'{op} failed: {r}'
print('all ops ok')
"
```

Implementation notes:
- `build_result` dispatches to `_op_*` helpers based on `op`.
- `_op_callers(target, project, timeout_ms)`:
  Cypher: `MATCH (caller)-[:CALLS]->(fn) WHERE fn.name = "<target>" RETURN caller.name, caller.file_path LIMIT 20`
  via `query_graph`. Format result as `## Callers of <target>\n- <name> (<file>)\n...`.
- `_op_callees(target, project, timeout_ms)`:
  Cypher: `MATCH (fn)-[:CALLS]->(callee) WHERE fn.name = "<target>" RETURN callee.name, callee.file_path LIMIT 20`.
  Format: `## Callees of <target>\n`.
- `_op_impact(target, project, timeout_ms)`:
  Call both `_op_callers` and `_op_callees`; merge into one result block
  `## Impact of <target>\n### Callers\n...\n### Callees\n...`.
- `_op_chain(target, project, timeout_ms)`:
  target format: `"source->dest"` (split on `->`). Call `trace_path` with
  `{"project": project, "function_name": source, "mode": "calls"}`. Format output.
  If target has no `->`, treat as impact.

### Phase 3 — Op dispatch: pattern, overview + build_result wiring

File: `src/codeintel/providers/graph.py`

Done when: All six ops return ok=True for any input; `build_result` with an unknown op returns
safe-null with `reason="unsupported-op"`.

Purpose: Complete the op surface and wire `build_result` to the full dispatch table.

Depends on: Phase 2.

Enables: Story 5 (overview); makes the provider fully conformant.

Verify:
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "
from codeintel.providers.graph import GraphProvider
p = GraphProvider()
for op in ['impact', 'callers', 'callees', 'chain', 'pattern', 'overview', 'bogus']:
    r = p.build_result(op, 'x', [], 0, '.')
    assert r['ok'] is True, f'{op}: {r}'
print('all ops + unknown op safe-null: ok')
"
```

Implementation notes:
- `_op_pattern(target, project, timeout_ms)`:
  Call `search_code` with `{"project": project, "pattern": target}`.
  Format: `## Pattern matches for "<target>"\n<results or "No matches.">`.
- `_op_overview(target, project, timeout_ms)`:
  Call `get_architecture` with `{"project": project}`.
  Format: pass through the returned architecture text or convert dict to readable sections.
- `build_result` full dispatch table:
  ```
  "impact"   → _op_impact
  "callers"  → _op_callers
  "callees"  → _op_callees
  "chain"    → _op_chain
  "pattern"  → _op_pattern
  "overview" → _op_overview
  ```
  Unknown op → `safe_null_result(..., reason="unsupported-op")`.
- All `_op_*` are wrapped in try/except → return None on failure; `build_result` maps None → safe_null.
- `engine` field in all returned Results: `"graph"`.

---

## Conversation 2 — Server wiring

Wire `GraphProvider` into the running MCP server. Fix `server.py` to forward `project_root` and
auto-include the graph engine. Update `code.status` to report graph engine availability.

Do NOT rewrite `gateway.py`'s provider-iteration logic — only add what's needed. Do NOT change
`provider.py` or `none.py`.

### Phase 4 — Gateway project_root passthrough

File: `src/codeintel/gateway.py`

Done when: `Gateway().query("impact", "gateway", project_root="/tmp")` passes `project_root` to
the provider's `build_result()`.

Purpose: The existing Gateway swallowed `project_root` — GraphProvider needs it to resolve the
project name. This phase ensures the passthrough without breaking NoneProvider.

Depends on: Conv 1 (GraphProvider exists and accepts project_root).

Enables: Phase 5 (server uses project_root from MCP args).

Verify:
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "
from codeintel.gateway import Gateway
from codeintel.providers.none import NoneProvider
gw = Gateway([NoneProvider()])
r = gw.query('impact', 'x', project_root='/tmp')
assert r['ok'] is True
print('project_root passthrough ok:', r)
"
```

Implementation notes:
- Current `gateway.py` already has `project_root=None` in `query()` signature and passes it to
  `build_result()`. Verify this is wired through; if not, add the passthrough.
- Also ensure `engine` and `budget` are forwarded correctly to `build_result()`.
- No other changes to Gateway logic.

### Phase 5 — Server auto-include + code.status update

File: `src/codeintel/server.py`

Done when: Starting the server and calling `code.status` returns `{"ok": true, "engines": ["graph", "none"], ...}` when the backend is installed; `["none"]` when it is not. `code.query` with `project_root` forwarded reaches the provider correctly.

Purpose: Expose GraphProvider to the MCP surface. This is the integration point that makes F2
visible to agents.

Depends on: Phase 4 (Gateway wiring), Conv 1 (GraphProvider).

Enables: Story 1 (real graph data via MCP), Story 2 (status reporting).

Verify:
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "
from codeintel.server import code_query_handler, code_status_handler
r = code_status_handler({})
assert r['ok'] is True
print('status:', r)
r2 = code_query_handler({'op': 'impact', 'target': 'gateway', 'project_root': '.'})
assert r2['ok'] is True
print('query:', r2)
"
```

Implementation notes:
- Add a module-level `_build_providers()` function that:
  1. Creates `GraphProvider()`.
  2. If `gp.available`: providers = [gp, NoneProvider()]
  3. Else: providers = [NoneProvider()]
- Replace `Gateway()` with `Gateway(_build_providers())` in `code_query_handler`.
- Update `_code_query` async handler to accept and forward `project_root: str = ""`.
- Update `code_status_handler` to return the list of available engine names:
  - Check `GraphProvider().available`; if True, include `"graph"` in engines list.
- Keep `code_query_handler` and `code_status_handler` as pure Python (non-async) for testability.

---

## Conversation 3 — Tests

Add `tests/test_graph_provider.py` covering the never-raise invariant, backend-absent scenario,
timeout behavior, and op dispatch correctness.

Do NOT touch `tests/test_never_raise.py`. Run both test files to ensure nothing regressed.

### Phase 6 — GraphProvider test suite

File: `tests/test_graph_provider.py`

Done when: `pytest tests/test_graph_provider.py tests/test_never_raise.py -v` passes green.

Purpose: Prove the never-raise invariant and key behavioral guarantees for GraphProvider without
requiring the actual backend binary.

Depends on: Conv 1 + Conv 2 (GraphProvider + server wiring complete).

Enables: Story 6 (never-raise invariant tested); meets F2 AC "deadline-bounded" and
"safe-null with reason".

Verify:
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
pip install -e ".[dev]" -q 2>/dev/null || pip install -e . -q
pytest tests/test_graph_provider.py tests/test_never_raise.py -v
```

Tests to include:

**Group 1 — Never-raise: None args**
- `test_graph_provider_none_args`: `build_result(None, None, None, None, None)` → `ok=True`.

**Group 2 — Never-raise: wrong types**
- `test_graph_provider_wrong_types`: `build_result(123, [], {}, "bad", object())` → `ok=True`.

**Group 3 — Backend unavailable: safe-null with reason**
- `test_graph_provider_unavailable`: monkeypatch `shutil.which` to return None →
  `available=False`, `build_result(...)` returns `{"ok": True, "result": None, "reason": "engine-unavailable"}`.

**Group 4 — Project not indexed: safe-null with reason**
- `test_graph_provider_project_not_indexed`: monkeypatch `_run` to return `[]` (empty project list) →
  `build_result("impact", "fn", [], 0, "/my/repo")` returns `{"ok": True, "result": None, "reason": "project-not-indexed"}`.

**Group 5 — Subprocess raises/times out: safe-null**
- `test_graph_provider_subprocess_raises`: monkeypatch `_run` to raise `subprocess.TimeoutExpired` →
  `build_result(...)` returns `{"ok": True, "result": None, "reason": "timeout"}`.
- `test_graph_provider_subprocess_crash`: monkeypatch `_run` to return None (simulates crash/bad JSON) →
  returns `{"ok": True, "result": None}`.

**Group 6 — Op dispatch: unknown op returns safe-null**
- `test_graph_provider_unknown_op`: `build_result("nonexistent-op", "x", [], 0, "")` →
  `{"ok": True, "result": None, "reason": "unsupported-op"}`.

**Group 7 — engine field**
- `test_graph_provider_engine_field_when_available`: monkeypatch to simulate available backend
  and indexed project; mock `_run` to return a minimal valid response →
  `build_result("callers", "x", [], 0, "/repo")["engine"] == "graph"`.

**Group 8 — Server status reflects graph availability**
- `test_code_status_with_graph`: monkeypatch `GraphProvider.available` → True;
  `code_status_handler({})["engines"]` includes `"graph"`.
- `test_code_status_without_graph`: monkeypatch `GraphProvider.available` → False;
  `code_status_handler({})["engines"]` does not include `"graph"`.
