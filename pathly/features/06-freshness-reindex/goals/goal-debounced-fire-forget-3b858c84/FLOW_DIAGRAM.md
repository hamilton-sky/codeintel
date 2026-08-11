# Flow Diagram — Debounced fire-and-forget incremental reindex seam

```
MCP Client
    |
    | code.query(op, target, project_root)
    v
server.py: code_query_handler()
    |
    | _build_gateway(reindexer=_REINDEXER)
    v
Gateway.query()
    |
    +---> maybe_reindex(project_root)       [non-blocking]
    |          |
    |          | enabled? ──No──> return
    |          |
    |          | debounced? ──Yes──> return
    |          |
    |          | update last_fired
    |          |
    |          +---> ThreadPoolExecutor.submit(_do_reindex)
    |                     |                    [daemon thread]
    |                     |  Semantic: SemanticDb + Indexer.index()
    |                     |  Graph:    subprocess detect_changes
    |                     |  try/except wraps all — never raises
    |                     v
    |                 [background, completes async]
    |
    | policy check → cache lookup → provider dispatch
    |
    | engine=semantic ──> SemanticProvider.build_result()
    |                          |
    |                          | Searcher.search(target, project_root)
    |                          | [reads DB — may be slightly stale on 1st call]
    |                          v
    |                        Result
    |
    | engine=graph ───> GraphProvider.build_result()
    |                          |
    |                          | codebase-memory-mcp CLI subprocess
    |                          v
    |                        Result
    v
{"ok": True, "result": "...", "engine": "..."}
    |
MCP Client receives response
    [before reindex thread finishes — non-blocking]


Config gate (CODEINTEL_REINDEX=off):
  _REINDEXER._enabled = False
  maybe_reindex() ──> return  [instant no-op, no thread]
```

## Fallback paths

```
Reindexer._do_reindex() failure:
  exception caught by try/except
  logger.warning(...)
  thread exits cleanly
  Gateway cache unaffected

SemanticProvider (cold DB, no index yet):
  Searcher returns []
  safe_null_result(reason="below-floor")
  [Reindexer fires in background]
  [next query after debounce window sees data]
```
