# Flow Diagram — F4: Unified Gateway (Engine Selector)

## Engine Selection and Dispatch

```
code.query(op, target, engine, role, project_root)
        |
        v
  [TieringPolicy.is_allowed(role, op)?]
        | NO → safe-null (reason: op-not-allowed-for-role)
        | YES
        v
  [ContentHashCache.get(op, target, engine, project_root)]
        | HIT → return cached Result (cached=True)
        | MISS
        v
  resolve engine
  ┌───────────────────────────────────────────────────┐
  │  engine=graph    → dispatch to GraphProvider      │
  │  engine=lsp      → dispatch to LspProvider        │
  │  engine=semantic → dispatch to SemanticProvider   │
  │  engine=both     → fan-out: [graph, lsp]          │
  │  engine=all      → fan-out: [graph, lsp, semantic]│
  │  engine=auto     → _AUTO_ENGINE[op] lookup        │
  │  engine=""       → same as auto                   │
  │  engine=unknown  → safe-null (reason: unknown-eng)│
  └───────────────────────────────────────────────────┘
        |
        v (single engine path)
  provider.build_result(op, target, files, budget, root)
        | null → safe-null (reason: engine-unavailable
        |              or unsupported-op)
        | Result → store in cache → return (cached=False)

        v (fan-out path: both / all)
  ThreadPoolExecutor(max_workers=3)
  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐
  │ graph thread │  │  lsp thread │  │ semantic thread   │
  │build_result()│  │build_result()│  │ build_result()   │
  └──────┬──────┘  └──────┬──────┘  └────────┬─────────┘
         └─────────────────┼──────────────────┘
                           v
              collect non-null Results
              ┌─────────────────────────────────────┐
              │  _merge(): prepend ## [engine] label │
              │  join non-null result strings        │
              │  all null → safe-null                │
              └─────────────────────────────────────┘
                           |
                           v
              store merged Result in cache
                           |
                           v
              return merged Result (cached=False)
```

## Auto-Engine Routing Table

```
op=impact   → graph      op=callers  → graph
op=callees  → graph      op=chain    → graph
op=pattern  → graph      op=overview → graph (lsp fallback)
op=symbol   → lsp        op=search   → semantic
op=context  → both (graph + lsp)
op=<other>  → NoneProvider safe-null (unsupported-op)
```

## Fallback Chain

```
provider returns null?
  → safe-null envelope returned to caller
  → reason field identifies why (unavailable / warming / unsupported-op / error)
  → caller degrades to grep or retries later
  → NEVER an exception or 500
```
