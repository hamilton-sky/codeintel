# User Stories — GraphProvider (F2)

---

## Story 1 — Graph query on an indexed repo

**As** a coding agent,
**I want** to call `code.query` with `op=impact` and a symbol name,
**so that** I get real caller/callee data from the code graph without writing Cypher or managing a subprocess.

### Acceptance criteria
- Given: repo is indexed with `codebase-memory-mcp`; `GraphProvider` is registered.
- `op=impact` returns `{"ok": true, "result": "<caller/callee block>", "engine": "graph", ...}`.
- `op=callers`, `op=callees`, `op=chain`, `op=pattern`, `op=overview` each return non-null results.
- `result` is a human-readable string (one section per op).
- `engine` field is `"graph"` in the envelope.

---

## Story 2 — Backend not installed → safe-null

**As** a coding agent on a machine without `codebase-memory-mcp` installed,
**I want** `code.query` to return a well-formed safe-null,
**so that** my workflow degrades gracefully to grep rather than crashing.

### Acceptance criteria
- `GraphProvider` auto-detects absence of `codebase-memory-mcp` on PATH.
- Returns `{"ok": true, "result": null, "reason": "engine-unavailable", ...}`.
- Never raises an exception.
- `code.status` reports `engines: ["none"]` (graph absent).

---

## Story 3 — Repo not indexed → safe-null with reason

**As** a coding agent on a machine that has the backend but hasn't indexed this repo,
**I want** `code.query` to return a safe-null with an informative reason,
**so that** I know to run `index` first rather than think the feature is broken.

### Acceptance criteria
- When `list_projects` returns no entry matching `project_root`, the result is `{"ok": true, "result": null, "reason": "project-not-indexed"}`.
- Never raises.

---

## Story 4 — Deadline-bounded queries

**As** a coding agent,
**I want** every graph query to respect a deadline (the `budget` field in ms),
**so that** a wedged or slow backend subprocess never blocks a response.

### Acceptance criteria
- If the subprocess does not finish within `budget` ms (default 5000 ms when budget=0), it is terminated.
- Returns `{"ok": true, "result": null, "reason": "timeout"}`.
- Never hangs indefinitely.
- Default timeout: 5000 ms.

---

## Story 5 — `op=overview` returns architecture snapshot

**As** a coding agent about to refactor,
**I want** `op=overview` to return a whole-repo structural summary,
**so that** I get a quick orientation without reading every file.

### Acceptance criteria
- `op=overview` calls `get_architecture` on the backend.
- Returns a non-null string describing project structure when backend is installed and repo is indexed.
- Safe-null when backend unavailable or not indexed.

---

## Story 6 — Never-raise invariant

**As** the codeintel system,
**I need** `GraphProvider.build_result()` to never raise under any inputs,
**so that** the Gateway's safety contract holds regardless of backend behavior.

### Acceptance criteria
- `build_result(None, None, None, None, None)` returns `{"ok": true, ...}`.
- If the backend crashes mid-response (e.g., subprocess exits non-zero), returns safe-null.
- If JSON parse fails, returns safe-null.
- Fault-injection test proves the invariant.
