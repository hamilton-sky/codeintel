# Edge Cases — GraphProvider (F2)

---

## Phase 1 — Backend detection + subprocess runner

### EC-1.1 Backend binary not on PATH
- `shutil.which("codebase-memory-mcp")` returns None.
- `self.available = False`.
- Every call to `build_result()` returns `{"ok": true, "result": null, "reason": "engine-unavailable"}`.
- No subprocess spawned. No exception raised.

### EC-1.2 Backend binary exists but is not executable
- `shutil.which` returns a path, but the binary fails with `PermissionError`.
- `_run()` catches the exception, returns None.
- `build_result()` sees None project list → safe-null with `reason="engine-unavailable"`.

### EC-1.3 `list_projects` returns empty list (repo not indexed)
- `_resolve_project()` finds no matching entry → returns None.
- `build_result()` returns safe-null with `reason="project-not-indexed"`.

### EC-1.4 `list_projects` subprocess times out
- `subprocess.TimeoutExpired` raised inside `_run()`.
- `_run()` catches it, returns None.
- `_resolve_project()` returns None → safe-null with `reason="project-not-indexed"`.

### EC-1.5 Backend returns malformed JSON
- `json.loads(proc.stdout)` raises `json.JSONDecodeError`.
- `_run()` catches, returns None.
- Caller treats None as failure → safe-null.

### EC-1.6 `budget=0` passed in
- Default timeout 5000 ms is used. Never passes 0 as a subprocess timeout.

---

## Phase 2 — Op dispatch: impact, callers, callees, chain

### EC-2.1 Symbol not found in graph
- Cypher query returns empty list `[]`.
- Result formatted as `"## Callers of <target>\n(none found)"` — non-null result, ok=True.

### EC-2.2 `chain` op with no `->` separator in target
- `target.split("->")` yields a single element.
- Fallback: treat as `impact` on the single symbol. Document this in the result string.

### EC-2.3 `chain` op where `trace_path` returns None
- Backend doesn't support `trace_path` or returns null.
- `_op_chain` returns None → `build_result` returns safe-null.

### EC-2.4 Cypher query returns unexpected structure (not a list)
- `_run("query_graph", ...)` returns a dict instead of list.
- The formatting helper checks type; if unexpected, returns a "raw" string or empty section.
- Never raises.

---

## Phase 3 — Op dispatch: pattern, overview

### EC-3.1 `pattern` search returns no matches
- `search_code` returns `[]`.
- Result: `"## Pattern matches for \"<target>\"\n(no matches)"`. Not a safe-null — this is a
  valid empty response.

### EC-3.2 `overview` returns a deeply nested dict
- `get_architecture` returns a complex dict.
- `_op_overview` formats it via `json.dumps(..., indent=2)` wrapped in a code block as fallback.
- Never crashes on unexpected shapes.

### EC-3.3 Unknown op passed
- `build_result("nonexistent", ...)` hits the `else` branch.
- Returns `safe_null_result(..., reason="unsupported-op")`.

---

## Phase 4 — Gateway project_root passthrough

### EC-4.1 `project_root=None` passed to gateway
- Gateway passes `project_root or ""` to provider — never None.
- `_resolve_project("")` returns None → safe-null with `reason="project-not-indexed"`.

---

## Phase 5 — Server auto-include + code.status

### EC-5.1 GraphProvider init raises (edge case in detection logic)
- `_build_providers()` wraps `GraphProvider()` in try/except → falls back to `[NoneProvider()]`.
- Server starts regardless.

### EC-5.2 Two simultaneous calls while `_resolve_project` is running
- `_resolve_project` result is cached per `project_root` in `self._project_cache`.
- Concurrent calls read from cache after the first resolves. No lock needed for simple dict reads
  in CPython (GIL-safe for dict get/set).

---

## Phase 6 — Tests

### EC-6.1 Tests must not require `codebase-memory-mcp` to be installed
- All tests that exercise GraphProvider behavior mock `shutil.which` or `_run`.
- Tests can run in a clean CI environment.

### EC-6.2 Monkeypatching `_run` on an instance
- Patch `codeintel.providers.graph.subprocess.run` (or `GraphProvider._run`) — not the global
  subprocess module — to avoid side effects on other tests.
