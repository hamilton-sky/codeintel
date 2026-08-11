# FLOW_DIAGRAM — LspProvider (F3 LSP Engine Adapter)

---

## LspSession state machine

```
                     first build_result call
                     for a new project_root
                            │
                            ▼
                      ┌──────────┐
                      │  WARMING │◀────────────────────────────┐
                      └────┬─────┘                             │
                           │ daemon thread                      │
                           │ asyncio loop                       │
                  ┌────────┴────────┐                          │
                  │                 │                          │
                 OK               FAIL                         │
                  │                 │                          │
                  ▼                 ▼                          │
           ┌──────────┐    ┌──────────────┐    cooldown       │
           │  READY   │    │    FAILED    │──── expires ───────┘
           └──────────┘    └──────────────┘
```

---

## build_result call flow

```
caller (Gateway)
     │
     ▼
LspProvider.build_result(op, target, files, budget, project_root)
     │
     ├─── available=False ──────────────────────────────────────▶ safe_null(reason="engine-unavailable")
     │
     ├─── _get_or_create_session(root)
     │         │
     │         ├── session.state == WARMING ────────────────────▶ safe_null(reason="warming")
     │         │
     │         ├── session.state == FAILED
     │         │     ├── cooldown active ───────────────────────▶ safe_null(reason="boot-failed")
     │         │     └── cooldown expired ── delete session ─────▶ [recurse: creates new WARMING session]
     │         │
     │         └── session.state == READY
     │               │
     │               ├── op == "symbol" ──── call_tool("find_symbol") ────────────▶ Result
     │               │                    + call_tool("find_referencing_symbols") │
     │               │                    (via run_coroutine_threadsafe + timeout) │
     │               │                                                             │
     │               ├── op == "overview" ─ call_tool("get_symbols_overview") ────▶ Result
     │               │
     │               └── other op ────────────────────────────────────────────────▶ safe_null(reason="unsupported-op")
     │
     └─── any uncaught exception ──────────────────────────────▶ safe_null(reason="error")
```

---

## Layers touched

```
  MCP server (server.py)
       │
       ▼
  Gateway (gateway.py)          ← provider chain unchanged
       │
       ▼
  LspProvider (providers/lsp.py)   ← NEW (Phase 1)
       │
       ├── _LspSession (threading + asyncio)
       │
       └── uvx serena [subprocess, stdio MCP]
                │
                └── language server (e.g. pyright / clangd)
```
