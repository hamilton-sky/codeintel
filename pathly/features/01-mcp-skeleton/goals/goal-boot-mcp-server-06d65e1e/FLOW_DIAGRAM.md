# F1 MCP Skeleton — Flow Diagram

## Happy Path: Agent Calls code.query

```
Host agent (Claude / Codex)
        │  MCP stdio JSON-RPC
        │  tools/call code.query
        │  {op, target, engine?, project_root?}
        ▼
  [ server.py — MCP handler ]
        │  .get("op","")  .get("target","")
        │  (safe field extraction)
        ▼
  [ gateway.py — Gateway.query() ]
        │  outer try/except ──── exception ──► safe_null_result(reason="gateway-error")
        │
        │  loop providers:
        ├─ provider try/except ─ exception ──► skip, try next
        ▼
  [ providers/none.py — NoneProvider.build_result() ]
        │  try/except ─────────── exception ──► inline safe-null fallback
        │
        │  safe_null_result(op, target, "none", "no-engine")
        ▼
  Result: {ok:True, result:None, engine:"none", cached:False, reason:"no-engine"}
        │
        ▼
  [ server.py — serialize to JSON ]
        │
        ▼
  Host agent receives safe-null envelope
  (degrades to grep — no crash)
```

## Happy Path: Agent Calls code.status

```
Host agent
        │  tools/call code.status  {}
        ▼
  [ server.py — code_status handler ]
        │  try/except (never raises)
        │
        ├─► returns: {ok:True, engines:["none"],
        │             indexed:False, model:None}
        ▼
  Host agent learns: no engines installed, index empty
```

## Startup Flow

```
codeintel serve
        │
        ▼
  [ __main__.py — main() ]
        │  parse args: subcommand == "serve"
        ▼
  [ server.py — run() ]
        │
        ├─ instantiate Gateway()
        │   └─ no providers → default to [NoneProvider()]
        │
        ├─ register code.query tool
        ├─ register code.status tool
        │
        ▼
  MCP SDK stdio loop (blocks, waiting for JSON-RPC)
        │
        ├─ initialize handshake ──► ok
        ├─ tools/list ──────────► [{code.query}, {code.status}]
        └─ tools/call ──────────► routes to handler above
```

## Error Containment

```
Exception anywhere in providers
        │
        ├─► Gateway per-provider try/except
        │         └─► skip provider, try next
        │
Exception anywhere in Gateway
        │
        ├─► Gateway outer try/except
        │         └─► safe_null_result(reason="gateway-error")
        │
Exception anywhere in MCP handler
        │
        └─► handler try/except
                  └─► safe_null_result(reason="server-error")

Result at every path: a valid {ok, op, target, result, engine, cached, reason?} dict
No exception escapes to the MCP transport layer.
```

## Component Legend

| Component | Role in F1 |
|---|---|
| `server.py` | Registers MCP tools; thin delegation shell |
| `gateway.py` | Provider registry, routing, outer safety boundary |
| `providers/none.py` | The only registered provider; always returns safe-null |
| `provider.py` | Shared types: CodeProvider protocol, Result TypedDict, safe_null_result |
| `__main__.py` | CLI entry — routes `serve` to `server.run()` |
