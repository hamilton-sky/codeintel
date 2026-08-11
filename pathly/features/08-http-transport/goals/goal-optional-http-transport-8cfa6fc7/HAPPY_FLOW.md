# 08-http-transport — Happy Flow

## Overview

A Pathly harness sends `POST /code/query` with `{"op": "symbol", "target": "Gateway"}` to a
running `codeintel serve-http` server. The server delegates to the existing gateway, which finds
the LSP provider available, and returns the symbol definition. The HTTP response is identical in
shape to the MCP tool result. The harness never needs to spawn or pipe to an MCP process.

---

## Phase 1 — HTTP server module

### Step-by-Step

#### Step 1: Module import
- **User does**: nothing (module is loaded by Phase 2 CLI or test fixtures)
- **System does**: imports `code_query_handler` and `code_status_handler` from `codeintel.server`; defines `_Handler` and `CodeIntelHTTPServer`; exports `run(host, port)`
- **State after**: `from codeintel.http_server import run` succeeds without error

#### Step 2: POST /code/query — valid request
- **User does**: sends `POST /code/query` with `Content-Type: application/json` and body `{"op": "symbol", "target": "Gateway", "project_root": "/path/to/repo"}`
- **System does**: `_Handler.do_POST` reads body, calls `json.loads`, passes dict to `code_query_handler()`, which routes through Gateway → LSP provider → returns `Result`
- **State after**: HTTP 200 response, JSON body `{"ok": true, "op": "symbol", "target": "Gateway", "result": "...", "engine": "lsp", "cached": false}`

#### Step 3: GET /code/status
- **User does**: sends `GET /code/status`
- **System does**: `_Handler.do_GET` calls `code_status_handler({})`, serializes result
- **State after**: HTTP 200 response, JSON body `{"ok": true, "engines": ["lsp"], "graph": false, "lsp": true, "semantic": false, "indexed": false, "model": null}`

---

## Phase 2 — CLI serve-http subcommand

### Step-by-Step

#### Step 1: User starts the server
- **User does**: runs `codeintel serve-http --port 8766`
- **System does**: `__main__.main()` dispatches to `from codeintel.http_server import run; run(host="127.0.0.1", port=8766)`
- **State after**: terminal prints `Listening on http://127.0.0.1:8766`, server is accepting connections

#### Step 2: Verify help text
- **User does**: runs `codeintel serve-http --help`
- **System does**: argparse prints usage with `--port` and `--host` options
- **State after**: exits 0, help text is visible

---

## Phase 3 — HTTP server tests

### Step-by-Step

#### Step 1: Test fixture starts ephemeral server
- **User does**: runs `pytest tests/test_http_server.py`
- **System does**: fixture launches `CodeIntelHTTPServer` on port 0 in a background thread, captures assigned port
- **State after**: server is accepting connections on a random available port

#### Step 2: Tests execute
- **User does**: pytest runs each test function
- **System does**: HTTP client sends requests to the test server, asserts on status codes and response shapes
- **State after**: all 6 tests pass; fixture tears down the server thread

---

## End State

`codeintel serve-http` runs as an optional HTTP sidecar. Any harness that can speak HTTP can
call `POST /code/query` and get the same safe-null Result the MCP tool would return. The
existing `codeintel serve` (MCP stdio) is completely unaffected.

## Success Indicators

- [ ] `pytest tests/test_http_server.py -q` passes with 6 tests, no failures
- [ ] `codeintel serve-http --help` exits 0 and shows `--port`/`--host`
- [ ] POST /code/query response shape matches `Result` TypedDict from `provider.py`
- [ ] Engine miss returns 200, not 500
- [ ] `codeintel serve` (MCP) still works after adding serve-http
