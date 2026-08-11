# 08-http-transport — Plan Architecture

## Context

The existing MCP server (`server.py`) already exposes two transport-agnostic handler functions:
`code_query_handler(args: dict) -> dict` and `code_status_handler(args: dict) -> dict`. These
functions hold all business logic (gateway routing, safe-null contract, engine dispatch). This
feature adds a thin HTTP adapter that calls them directly — no logic is duplicated.

## Design Principles

- **No new package dependencies.** Use stdlib `http.server` and `json` only.
- **Reuse, don't duplicate.** `http_server.py` imports from `server.py`; it is a transport adapter, not a copy of the gateway logic.
- **Never-500 contract inherited.** `code_query_handler` already wraps everything in `except Exception → safe_null_result`. The HTTP layer only needs to map caller errors (bad JSON) to 4xx; all engine/internal errors remain 2xx.
- **Single-responsibility.** `http_server.py` does HTTP parsing and JSON serialization only. It contains no intelligence.
- **File size budget.** `http_server.py` ≤ 120 lines; `__main__.py` diff ≤ 15 lines.

## Phase Mapping

### Phase 1 — HTTP server module

New file `src/codeintel/http_server.py`:

```
BaseHTTPRequestHandler
├── do_POST  (/code/query)
│     ├── read Content-Length bytes from rfile
│     ├── json.loads → dict or 400
│     └── code_query_handler(body) → _send_json(200, result)
├── do_GET  (/code/status)
│     └── code_status_handler({}) → _send_json(200, result)
└── _send_json(status, data)
      ├── send_response(status)
      ├── send_header("Content-Type", "application/json")
      └── wfile.write(json.dumps(data).encode())
```

Key invariants:
- `_Handler.do_POST` catches `json.JSONDecodeError` AND `not isinstance(parsed, dict)` → 400
- All other exceptions in `do_POST`/`do_GET` are NOT caught at the handler level; they bubble to
  `BaseHTTPRequestHandler.handle_error()` which logs to stderr but does not crash the server. This
  is acceptable because `code_query_handler` and `code_status_handler` already catch everything.

### Phase 2 — CLI integration

`__main__.py` change is additive only: a new `elif args.command == "serve-http":` branch and
two new `add_argument` calls on a new `http_parser`. No existing branches are modified.

`KeyboardInterrupt` from `serve_forever()` is caught in the CLI dispatch branch (not in
`http_server.run()`) so that `run()` remains testable without try/except.

### Phase 3 — Tests

Test fixture pattern:

```python
import http.server, threading, http.client, json

@pytest.fixture
def server():
    srv = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, port
    srv.shutdown()
```

Tests use `http.client.HTTPConnection` (stdlib) — no requests library needed.

## Dependency Graph

```
http_server.py
    └── imports: codeintel.server (code_query_handler, code_status_handler)
                 codeintel.server imports gateway.py → provider.py (unchanged)

__main__.py
    └── lazy import: codeintel.http_server.run

tests/test_http_server.py
    └── imports: codeintel.http_server (CodeIntelHTTPServer, _Handler)
```

No circular imports. `http_server.py` is a leaf module — nothing else imports it at startup.
