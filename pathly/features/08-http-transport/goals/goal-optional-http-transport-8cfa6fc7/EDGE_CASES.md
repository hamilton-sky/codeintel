# 08-http-transport — Edge Cases

## Phase 1 — HTTP server module

### EC-1.1: Malformed JSON in POST body
- **Trigger**: client sends `POST /code/query` with body `"not valid json"` or `{'single': 'quotes'}`
- **Current behavior**: `json.loads` raises `json.JSONDecodeError`
- **Expected behavior**: `do_POST` catches the exception, returns HTTP 400 with `{"error": "bad-request"}`
- **Handled in**: Phase 1 — `_Handler.do_POST` wraps `json.loads` in try/except

### EC-1.2: Empty or missing body in POST
- **Trigger**: client sends `POST /code/query` with `Content-Length: 0` or no body
- **Current behavior**: `json.loads("")` raises `json.JSONDecodeError`
- **Expected behavior**: HTTP 400 — same path as EC-1.1
- **Handled in**: Phase 1 — same `json.JSONDecodeError` catch

### EC-1.3: Valid JSON but not a dict
- **Trigger**: client sends `POST /code/query` with body `[1, 2, 3]` or `"string"`
- **Current behavior**: `code_query_handler` receives a non-dict; `args.get("op", "")` would raise
- **Expected behavior**: `do_POST` checks `isinstance(body, dict)`; if not → HTTP 400
- **Handled in**: Phase 1 — add `isinstance` guard after `json.loads`

### EC-1.4: Unknown engine value
- **Trigger**: client sends `{"op": "symbol", "target": "foo", "engine": "turbo"}`
- **Current behavior**: `Gateway.query()` returns `safe_null_result(..., reason="unknown-engine")`
- **Expected behavior**: HTTP 200 with `{"ok": true, "result": null, "reason": "unknown-engine"}` — never 500
- **Handled in**: Phase 1 — `code_query_handler` already catches this via `Gateway.query()`

### EC-1.5: All engines unavailable
- **Trigger**: server starts with graph=None, lsp=None, semantic not indexed
- **Current behavior**: `code_query_handler` falls through to `safe_null_result`
- **Expected behavior**: HTTP 200 with `ok: true, result: null, reason: "engine-unavailable"` — never 500
- **Handled in**: Phase 1 — no special handling needed; inherited from existing gateway contract

### EC-1.6: Internal exception in gateway
- **Trigger**: unexpected exception escapes all gateway try/except blocks
- **Current behavior**: `code_query_handler` has a top-level `except Exception` that returns `safe_null_result(..., reason="handler-error")`
- **Expected behavior**: HTTP 200 with `ok: true, result: null` — never 500
- **Handled in**: Phase 1 — inherited; no additional handling needed in `do_POST`

### EC-1.7: Unknown HTTP route
- **Trigger**: client sends `GET /unknown` or `DELETE /code/query`
- **Current behavior**: no route defined
- **Expected behavior**: HTTP 404 with `{"error": "not-found"}`
- **Handled in**: Phase 1 — `do_GET`/`do_POST` check path; fall through to 404

### EC-1.8: Missing Content-Length header on POST
- **Trigger**: client omits `Content-Length` header
- **Current behavior**: `rfile.read(0)` would read nothing
- **Expected behavior**: treat as empty body → HTTP 400 (EC-1.2 path)
- **Handled in**: Phase 1 — default `content_length = int(self.headers.get("Content-Length", 0))`

---

## Phase 2 — CLI serve-http subcommand

### EC-2.1: Port already in use
- **Trigger**: user runs `codeintel serve-http --port 8766` when port 8766 is already bound
- **Current behavior**: `HTTPServer.__init__` raises `OSError: [Errno 48] Address already in use`
- **Expected behavior**: exception propagates to the terminal with the OS error message; user sees a clear error
- **Handled in**: Phase 2 — no special handling (let it bubble; no silent hang)

### EC-2.2: SIGINT / Ctrl-C shutdown
- **Trigger**: user presses Ctrl-C while `serve_forever()` is running
- **Current behavior**: `KeyboardInterrupt` propagates out of `serve_forever()`
- **Expected behavior**: server shuts down cleanly; no traceback shown
- **Handled in**: Phase 2 — wrap `run()` call in `except KeyboardInterrupt: pass` inside `__main__` dispatch

---

## Phase 3 — HTTP server tests

### EC-3.1: Test port collision
- **Trigger**: two test runs in parallel grab the same random port before binding
- **Current behavior**: second bind fails
- **Expected behavior**: using `port=0` (OS assigns) makes collision impossible
- **Handled in**: Phase 3 — fixture uses `port=0` and reads `server.server_address[1]`

### EC-3.2: Server thread not cleaned up
- **Trigger**: test raises before fixture teardown
- **Current behavior**: daemon thread lingers
- **Expected behavior**: fixture uses `finally` block to call `server.shutdown()`; thread is daemon so process still exits
- **Handled in**: Phase 3 — fixture structure with `try/finally`

---

## Known Limitations

- HTTP server is synchronous (one request at a time per thread). Concurrent query load is not a
  concern for the "optional harness helper" use case; this is intentional and in-scope.
- No TLS / authentication. The server binds to localhost by default; exposing to external
  interfaces is the caller's responsibility.
- `indexed` and `model` fields in `/code/status` are always `false`/`null` (same as existing
  `code_status_handler`); a richer status endpoint is a future concern.
