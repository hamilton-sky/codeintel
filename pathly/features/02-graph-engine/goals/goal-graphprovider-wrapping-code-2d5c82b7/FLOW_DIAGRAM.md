# Flow Diagram — GraphProvider (F2)

ASCII diagrams showing the call flow for F2. Max ~70 chars wide.

---

## Happy path: `op=impact` with backend installed + repo indexed

```
Agent / MCP caller
  │
  │  code.query(op=impact, target=X, project_root=P)
  ▼
server.py: _code_query()
  │
  │  code_query_handler({op, target, project_root})
  ▼
gateway.py: Gateway.query()
  │ iterates providers
  ▼
providers/graph.py: GraphProvider.build_result()
  │
  ├─ _resolve_project(P)
  │    │
  │    │  codebase-memory-mcp cli list_projects {}
  │    ▼  (subprocess, 3s timeout)
  │    returns project_name or None
  │
  ├─ None? → safe_null(reason="project-not-indexed")
  │
  ├─ _op_impact(target, project, budget_ms)
  │    │
  │    ├─ _op_callers → query_graph Cypher (CALLS edges in)
  │    │    (subprocess, timeout_ms)
  │    │
  │    └─ _op_callees → query_graph Cypher (CALLS edges out)
  │         (subprocess, timeout_ms)
  │
  └─ Result{"ok":true, "engine":"graph", "result":"## Impact..."}
       │
       ▼
Agent receives structured impact block
```

---

## Fallback: backend not installed

```
GraphProvider.__init__()
  │
  │  shutil.which("codebase-memory-mcp") → None
  ▼
self.available = False
self._cmd = None

build_result(any op, ...)
  │
  └─ safe_null_result(reason="engine-unavailable")
       │
       ▼
Gateway falls through to NoneProvider
  │
  └─ safe_null_result(reason="no-engine")
       (NoneProvider is the final backstop)
```

---

## Fallback: subprocess timeout

```
_run(method, payload, timeout_ms)
  │
  │  subprocess.run([..., "cli", method, json], timeout=t)
  │  → TimeoutExpired raised
  │
  └─ except: return None

_op_*(target, project, timeout_ms)
  │  _run returns None
  └─ return None

build_result()
  │  _op_* returned None
  └─ safe_null_result(reason="timeout")
```

---

## Op → CLI method mapping

```
op=impact   → query_graph (callers Cypher)
              query_graph (callees Cypher)
              [merged result]

op=callers  → query_graph (CALLS edges in)
op=callees  → query_graph (CALLS edges out)
op=chain    → trace_path (mode=calls, src→dst)
op=pattern  → search_code (pattern=target)
op=overview → get_architecture
```

---

## Server initialization

```
server.py: run()
  │
  ├─ _build_providers()
  │    │
  │    ├─ GraphProvider()
  │    │    └─ _detect_backend() → available: bool
  │    │
  │    ├─ available? → [GraphProvider, NoneProvider]
  │    └─ else      → [NoneProvider]
  │
  ├─ Gateway(providers)
  │
  └─ MCPServer registers code.query + code.status
```
