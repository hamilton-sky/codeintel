# 08-http-transport — User Stories

## Context

The codeintel MCP server exposes `code.query` and `code.status` tools over stdio. Harnesses like
Pathly that prefer HTTP (rather than spawning and piping to an MCP process) have no way to reach
the same intelligence. This feature adds an optional HTTP port that mirrors the MCP contract
exactly — same response envelope, same safe-null behavior on engine miss — so any harness can
call `POST /code/query` over HTTP without adopting the MCP transport.

## Stories

### Story 1.1: HTTP code query endpoint
**As a** harness developer, **I want** a `POST /code/query` HTTP endpoint that accepts the same
fields as the MCP `code.query` tool, **so that** I can query code intelligence from an HTTP client
without running an MCP subprocess.

**Acceptance Criteria:**
- [ ] `POST /code/query` accepts JSON body with fields: `op`, `target`, `project_root`, `engine`, `role`
- [ ] Response JSON shape is byte-identical to the MCP `Result` type: `{ok, op, target, result, engine, cached, [reason]}`
- [ ] An engine miss returns HTTP 200 with `ok: true, result: null` (never 500)
- [ ] A malformed or non-JSON body returns HTTP 400 with a clear error message
- [ ] All fields are optional in the request body (missing fields default to empty string / "auto")

**Edge Cases:**
- Empty body → 400
- Body is valid JSON but not an object → 400
- `engine` value not in known set → 200 + `ok: true, result: null, reason: "unknown-engine"`
- All engines unavailable → 200 + `ok: true, result: null, reason: "engine-unavailable"`
- Internal exception in gateway → 200 + `ok: true, result: null, reason: "gateway-error"`

**Delivered by:** Phase 1 → Conversation 1

---

### Story 1.2: HTTP status endpoint
**As a** harness developer, **I want** a `GET /code/status` endpoint, **so that** I can check
which engines are available before sending queries.

**Acceptance Criteria:**
- [ ] `GET /code/status` returns HTTP 200 with `{ok, engines, graph, lsp, semantic, indexed, model}`
- [ ] Response is byte-identical to `code_status_handler()` output from `server.py`
- [ ] Never returns a 500 — any internal failure falls back to the all-false status envelope

**Edge Cases:**
- Engine detection fails → returns safe status with all engines false
- Server starts with no engines available → response still ok:true

**Delivered by:** Phase 1 → Conversation 1

---

### Story 1.3: CLI serve-http subcommand
**As a** developer, **I want** `codeintel serve-http --port PORT` to start the HTTP server,
**so that** I can expose the HTTP transport without modifying existing MCP serve behavior.

**Acceptance Criteria:**
- [ ] `codeintel serve-http` starts the HTTP server on default port 8766
- [ ] `codeintel serve-http --port PORT` binds to the specified port
- [ ] `codeintel serve-http --host HOST` binds to the specified host (default: 127.0.0.1)
- [ ] Server startup prints `Listening on http://<host>:<port>` to stdout
- [ ] Existing `codeintel serve` (MCP stdio) is unaffected

**Edge Cases:**
- Port already in use → OS error propagates cleanly (no silent hang)
- SIGINT / Ctrl-C → server shuts down cleanly

**Delivered by:** Phase 2 → Conversation 1

---

### Story 1.4: HTTP transport tests
**As a** developer, **I want** automated tests for the HTTP endpoints, **so that** the safe-null
contract and response shape are verified in CI.

**Acceptance Criteria:**
- [ ] Test: valid POST /code/query returns 200 with correct Result shape
- [ ] Test: bad JSON body returns 400
- [ ] Test: engine miss returns 200 with ok:true, result:null
- [ ] Test: GET /code/status returns 200 with expected keys
- [ ] Tests run without starting a real server (use `http.server` test client or mock transport)

**Delivered by:** Phase 3 → Conversation 1
