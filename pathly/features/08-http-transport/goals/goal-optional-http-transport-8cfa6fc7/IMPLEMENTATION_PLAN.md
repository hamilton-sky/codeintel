# 08-http-transport — Implementation Plan

## Overview

Add an optional HTTP transport layer that exposes `POST /code/query` and `GET /code/status`
endpoints. The MCP handler functions in `server.py` are already transport-agnostic; this
feature wraps them in a stdlib `http.server.BaseHTTPRequestHandler` with no new package
dependencies. A new `serve-http` CLI subcommand starts the server. The entire change is 3 phases
in 1 conversation.

## Layer Architecture

```
CLI (__main__.py serve-http)
        │
        ▼
http_server.py  (new)
   HTTPRequestHandler
        │  POST /code/query   │  GET /code/status
        ▼                     ▼
code_query_handler()    code_status_handler()   ← server.py (unchanged)
        │
        ▼
Gateway.query()  →  safe_null_result()          ← gateway.py / provider.py
```

## Prerequisites

- Feature `04-unified-gateway` is complete: `src/codeintel/gateway.py` and
  `src/codeintel/server.py` exist with `code_query_handler()` and `code_status_handler()`.
- Verify: `python -c "from codeintel.server import code_query_handler; print('ok')"` exits 0.

## Phase 1 — HTTP server module

**File:** `src/codeintel/http_server.py` — CREATE
**Done when:** `python -c "from codeintel.http_server import CodeIntelHTTPServer; print('ok')"` exits 0 and the module-level docstring describes both endpoints.
**Delivers stories:** S1.1, S1.2
**Depends on:** `src/codeintel/server.py` (read-only, imports `code_query_handler`, `code_status_handler`)
**Enables:** Phase 2 (CLI) and Phase 3 (tests)
**Details:**
- Import `code_query_handler` and `code_status_handler` from `codeintel.server`.
- Class `CodeIntelHTTPServer(http.server.HTTPServer)` — no init override needed; just a named class for clarity.
- Class `_Handler(http.server.BaseHTTPRequestHandler)`:
  - `do_POST`: only route is `/code/query`. Parse `Content-Type` (expect application/json).
    Read `Content-Length` bytes. Try `json.loads`. On `json.JSONDecodeError` or non-dict body →
    send 400 with `{"error": "bad-request"}`. On success → call `code_query_handler(body)` →
    send 200 with JSON result.
  - `do_GET`: only route is `/code/status`. Call `code_status_handler({})` → send 200 with JSON.
  - All other method/path combos → 404 with `{"error": "not-found"}`.
  - `log_message`: suppress default stderr chatter (override to no-op or route to Python logging).
  - `_send_json(status, data)`: helper — sets `Content-Type: application/json`, writes JSON body.
- Module-level `run(host="127.0.0.1", port=8766)` function:
  - Creates `CodeIntelHTTPServer((host, port), _Handler)`, prints `Listening on http://{host}:{port}`, calls `serve_forever()`.
- No new package dependencies — use only `http.server`, `json`, `io` from stdlib.
- File must stay ≤ 120 lines.

**Verify:** `python -c "from codeintel.http_server import run, CodeIntelHTTPServer; print('ok')"`

---

## Phase 2 — CLI serve-http subcommand

**File:** `src/codeintel/__main__.py` — MODIFY
**Done when:** `codeintel serve-http --help` prints usage without errors and shows `--port` and `--host` options.
**Delivers stories:** S1.3
**Depends on:** Phase 1 (`http_server.run` must be importable)
**Enables:** Manual and integration testing of the live server
**Details:**
- In `main()`, after the existing `subparsers.add_parser("install", ...)` block, add:
  ```python
  http_parser = subparsers.add_parser("serve-http", help="Start the HTTP transport server")
  http_parser.add_argument("--port", type=int, default=8766, help="Port to listen on (default: 8766)")
  http_parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
  ```
- In the `if args.command == ...` dispatch chain, add:
  ```python
  elif args.command == "serve-http":
      from codeintel.http_server import run
      run(host=args.host, port=args.port)
  ```
- Do NOT touch any existing subcommand branches (`serve`, `index`, `query`, `status`, `install`).
- Do NOT add any global imports — keep the lazy import pattern already used in this file.

**Verify:** `codeintel serve-http --help`

---

## Phase 3 — HTTP server tests

**File:** `tests/test_http_server.py` — CREATE
**Done when:** `pytest tests/test_http_server.py -q` passes with no failures.
**Delivers stories:** S1.4
**Depends on:** Phase 1 (imports `_Handler`, `CodeIntelHTTPServer` or uses `http.client`)
**Enables:** CI coverage of the safe-null contract over HTTP
**Details:**
- Use `threading.Thread` to start a test server on a random port (`port=0`, then read
  `server.server_address[1]`), tear it down in a fixture's `finally` block.
- `_post(path, body)` helper: sends HTTP POST via `http.client.HTTPConnection`, returns `(status, dict)`.
- `_get(path)` helper: sends HTTP GET via `http.client.HTTPConnection`, returns `(status, dict)`.
- Tests:
  1. `test_query_valid_body` — POST `/code/query` with `{"op": "symbol", "target": "foo"}` →
     status 200, response has keys `ok`, `op`, `target`, `result`, `engine`, `cached`.
  2. `test_query_bad_json` — POST `/code/query` with body `"not-json"` → status 400.
  3. `test_query_empty_body` — POST `/code/query` with empty body → status 400.
  4. `test_query_engine_miss` — POST `/code/query` with `{"op": "symbol", "target": "x"}` when
     no real engine is available → status 200, `ok` is True, `result` is None.
  5. `test_status` — GET `/code/status` → status 200, response has key `ok`, value True.
  6. `test_unknown_route` — GET `/unknown` → status 404.
- Never import private gateway internals directly; only import from `codeintel.http_server`.
- If `codeintel.http_server` is not importable, the test module should fail at collection time
  with a clear ImportError (no try/except around the top-level import).

**Verify:** `pytest tests/test_http_server.py -q`

---

## Key Decisions

- **Stdlib only** (`http.server` + `json`): adds zero new package dependencies, fits the
  "optional" character of this feature, and keeps the module under 120 lines.
- **Synchronous server in its own thread**: `code_query_handler` is sync; wrapping in
  `HTTPServer.serve_forever()` is the minimal path. Async upgrade (aiohttp/starlette) is a
  future concern.
- **Reuse existing handlers** (`code_query_handler`, `code_status_handler`): the MCP and HTTP
  transports share identical business logic — no duplication, consistent safe-null behavior.
- **Never 500**: all exceptions in `do_POST`/`do_GET` are caught and mapped to either 400
  (caller error) or 200 + safe-null (engine/internal error). The `code_query_handler` already
  returns `ok:true, result:null` on any internal failure.
- **Port 8766 default**: avoids collision with the Pathly comms server (8765) and common dev
  ports; configurable via `--port`.

## Recovery instruction

If verification fails and the fix requires out-of-scope changes, stop and report. If fundamentally
broken, rollback with `git checkout -- src/codeintel/http_server.py src/codeintel/__main__.py tests/test_http_server.py` and retry.
