# USER_STORIES — LspProvider (F3 LSP Engine Adapter)

---

## Story 1 — Symbol lookup after warm-up

**As** a coding agent calling `code.query`,
**I want** `op=symbol, engine=lsp` to return an always-fresh definition and reference list for a named symbol,
**so that** I get precise, post-edit accuracy that the graph index cannot guarantee.

### Acceptance criteria

- AC1.1: After the LSP session is READY, `build_result("symbol", "<name>", ...)` returns `ok=True` with a non-null `result` containing definition and/or references.
- AC1.2: `result["engine"]` is `"lsp"`.
- AC1.3: If the symbol is not found, returns safe-null with `ok=True` and `result=None` (not an error).

---

## Story 2 — Async warm-up: first call is safe-null

**As** a coding agent,
**I want** the first call to an unwarmed `LspProvider` to return a safe-null immediately (not block),
**so that** my prompt is never stalled by a slow language-server startup.

### Acceptance criteria

- AC2.1: While the session is in WARMING state, `build_result(...)` returns `ok=True`, `result=None`, `reason="warming"`.
- AC2.2: The warm-up runs in a daemon background thread and does not block the caller.
- AC2.3: After warm-up completes, subsequent calls return real data (AC1.1).

---

## Story 3 — Boot failure → cooldown, no per-request respawn

**As** a coding agent,
**I want** a failed LSP boot to enter a cooldown rather than retrying on every request,
**so that** a broken backend never floods the system with subprocess spawns.

### Acceptance criteria

- AC3.1: When the LSP bridge fails to start, the session transitions to FAILED state.
- AC3.2: While in FAILED state (during cooldown), `build_result(...)` returns `ok=True`, `result=None`, `reason="boot-failed"`.
- AC3.3: A new warm-up attempt is NOT started on every request during cooldown.
- AC3.4: After the cooldown window elapses, the next `build_result` call starts a fresh warm-up attempt (back to WARMING).

---

## Story 4 — Project-root switching tears down old session

**As** a developer querying multiple repos in sequence,
**I want** switching `project_root` to tear down the old session and start fresh for the new root,
**so that** symbols from repo A never bleed into results for repo B.

### Acceptance criteria

- AC4.1: Calling `build_result` with a different `project_root` than the last call creates a new `_LspSession` for the new root.
- AC4.2: The old session's background thread is not leaked (daemon thread, GC-collectible).

---

## Story 5 — Always-safe: never raises, engine-unavailable graceful

**As** a coding agent,
**I want** `LspProvider` to honor the never-raise / safe-null contract under any failure mode,
**so that** a broken LSP backend never crashes my prompt.

### Acceptance criteria

- AC5.1: `build_result` with `None` or wrong-typed arguments returns `ok=True`.
- AC5.2: When the LSP bridge binary is not detected, returns `ok=True`, `result=None`, `reason="engine-unavailable"`.
- AC5.3: An unsupported op returns `ok=True`, `result=None`, `reason="unsupported-op"`.
- AC5.4: Any uncaught internal exception is caught at the outermost level and returns `ok=True`.

---

## Story 6 — Overview op

**As** a coding agent,
**I want** `op=overview` on the LSP engine to return a symbols overview for the project or a file,
**so that** I can orient in the codebase with always-fresh data.

### Acceptance criteria

- AC6.1: When READY, `build_result("overview", "<path-or-empty>", ...)` returns a non-null result with symbol overview text.
- AC6.2: If overview data is unavailable, returns safe-null (not an error).

---

## Story 7 — Server status reports LSP availability

**As** a coding agent running `code.status`,
**I want** the status response to include `"lsp"` when the LSP bridge is detected,
**so that** I know whether to use `engine=lsp` before making a query.

### Acceptance criteria

- AC7.1: `code_status_handler({})` returns `"lsp"` in `engines` when the LSP bridge binary is on PATH.
- AC7.2: `code_status_handler({})` does NOT include `"lsp"` when the bridge is absent.
