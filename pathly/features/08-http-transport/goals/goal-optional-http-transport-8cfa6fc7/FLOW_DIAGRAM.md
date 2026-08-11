# 08-http-transport — Flow Diagram

## Happy Path: POST /code/query

```
HTTP client
    │  POST /code/query
    │  {"op": "symbol", "target": "Gateway", ...}
    ▼
_Handler.do_POST
    │  read body via Content-Length
    │  json.loads(body) → dict
    ▼
code_query_handler(args)      ← server.py (unchanged)
    │  Gateway.query(op, target, engine, role, ...)
    ▼
Gateway._dispatch_single / _fan_out
    │  LSP / graph / semantic provider
    ▼
Result {ok:true, op, target, result, engine, cached}
    │
    ▼
_send_json(200, result)
    │
    ▼
HTTP client receives 200 + JSON body
```

## Fallback: Engine Miss

```
code_query_handler(args)
    │  provider is None / unavailable
    ▼
safe_null_result(op, target, reason="engine-unavailable")
    │  {ok:true, result:null}
    ▼
_send_json(200, safe_null)
    │
    ▼
HTTP client receives 200 (never 500)
```

## Error Flow: Bad Request Body

```
HTTP client
    │  POST /code/query
    │  body: "not valid json"
    ▼
_Handler.do_POST
    │  json.loads raises JSONDecodeError
    │  OR parsed is not dict
    ▼
_send_json(400, {"error": "bad-request"})
    │
    ▼
HTTP client receives 400
```

## GET /code/status

```
HTTP client
    │  GET /code/status
    ▼
_Handler.do_GET
    │  route == "/code/status"
    ▼
code_status_handler({})       ← server.py (unchanged)
    │  detects graph/lsp/semantic availability
    ▼
{ok:true, engines:[...], graph, lsp, semantic, indexed, model}
    │
    ▼
_send_json(200, status)
    │
    ▼
HTTP client receives 200 + JSON body
```

## Component Legend

| Symbol | Meaning |
|--------|---------|
| `_Handler.do_POST` | Parses HTTP request body, validates JSON, routes to handler |
| `_Handler.do_GET` | Routes GET requests to status handler |
| `code_query_handler` | Transport-agnostic query handler in `server.py` |
| `code_status_handler` | Transport-agnostic status handler in `server.py` |
| `Gateway.query()` | Engine dispatch + safe-null contract + cache |
| `safe_null_result()` | Returns `{ok:true, result:null}` — never raises |
| `_send_json(status, data)` | Writes HTTP response with Content-Type: application/json |
