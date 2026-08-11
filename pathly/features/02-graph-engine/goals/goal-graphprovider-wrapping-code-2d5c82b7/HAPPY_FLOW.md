# Happy Flow — GraphProvider (F2)

---

## Phase 1 — Backend detection + subprocess runner

1. `GraphProvider()` initializes.
2. `_detect_backend()` calls `shutil.which("codebase-memory-mcp")` → returns `/usr/local/bin/codebase-memory-mcp`.
3. `self.available = True`, `self._cmd = "/usr/local/bin/codebase-memory-mcp"`.

---

## Phase 2 — Impact query (callers + callees)

1. Agent calls `code.query` with `op=impact, target="parse_result", project_root="/myrepo"`.
2. `server.py` `_code_query` handler receives args; calls `code_query_handler(...)`.
3. `Gateway.query()` iterates providers; reaches `GraphProvider.build_result("impact", "parse_result", [], 5000, "/myrepo")`.
4. `_resolve_project("/myrepo")`:
   - Calls `_run("list_projects", {}, 3000)` → `[{"name": "myrepo", "root_path": "/myrepo"}]`.
   - Returns `"myrepo"`.
5. `_op_impact("parse_result", "myrepo", 5000)`:
   - Calls `_op_callers(...)` → Cypher query via `query_graph` → `[{"caller.name": "route_handler", "caller.file_path": "server.py"}]`.
   - Calls `_op_callees(...)` → `[{"callee.name": "validate_schema", "callee.file_path": "schema.py"}]`.
   - Merges: `"## Impact of parse_result\n### Callers\n- route_handler (server.py)\n### Callees\n- validate_schema (schema.py)"`.
6. `build_result` returns `{"ok": true, "op": "impact", "target": "parse_result", "result": "## Impact...", "engine": "graph", "cached": false}`.
7. Agent receives the structured impact block.

---

## Phase 3 — Pattern search

1. Agent calls `op=pattern, target="TODO"`.
2. `_op_pattern("TODO", "myrepo", 5000)` → `search_code({"project": "myrepo", "pattern": "TODO"})`.
3. Backend returns a list of matches with file + line.
4. Result: `"## Pattern matches for \"TODO\"\n- src/server.py:42\n- src/gateway.py:17"`.

---

## Phase 4 — Overview

1. Agent calls `op=overview, target=""`.
2. `_op_overview("", "myrepo", 5000)` → `get_architecture({"project": "myrepo"})`.
3. Backend returns dict with layers/modules/entry-points.
4. Result: formatted architecture section string.

---

## Phase 5 — Server + status

1. `code.status` call → `code_status_handler({})` → checks `GraphProvider().available` → True.
2. Returns `{"ok": true, "engines": ["graph", "none"], "indexed": False, "model": null}`.
3. Agent knows graph engine is live and can proceed with graph queries.
