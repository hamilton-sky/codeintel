# F9 — Docs + test suite · Flow Diagram

## Test layers and doc layers

```
 Goal deliverables
  ┌──────────────────────────────────────────────────────┐
  │  Conv 1: Fix & Strengthen Tests                      │
  │                                                      │
  │  test_semantic_provider.py ──fix──> 61/61 green      │
  │  test_never_raise.py ─────expand─> groups 9-13 added │
  │                                                      │
  └──────────────────────────┬───────────────────────────┘
                             │ baseline: all tests green
  ┌──────────────────────────▼───────────────────────────┐
  │  Conv 2: E2e Smoke + CI                              │
  │                                                      │
  │  tests/test_e2e.py ──────────> fixture repo indexed  │
  │         │                      ranked result checked  │
  │         │                      gateway never-raise   │
  │         ▼                                            │
  │  .github/workflows/ci.yml ──> pytest on push/PR      │
  │                                                      │
  └──────────────────────────┬───────────────────────────┘
                             │ CI green
  ┌──────────────────────────▼───────────────────────────┐
  │  Conv 3: Docs                                        │
  │                                                      │
  │  README.md ──────────────> quickstart (< 5 min)      │
  │       │                    safe-null explanation     │
  │       │                    links to per-engine docs  │
  │       │                    CI badge                  │
  │       ▼                                              │
  │  docs/graph.md             ops + reasons + prereqs   │
  │  docs/lsp.md               state machine + reasons   │
  │  docs/semantic.md          indexer + search + floor  │
  │                                                      │
  └──────────────────────────────────────────────────────┘
```

## How the tests relate to the source

```
 tests/test_never_raise.py     ──covers──>  providers/none.py
                                            providers/graph.py
                                            providers/lsp.py
                                            providers/semantic.py
                                            gateway.py
                                            server.py
                                            http_server.py

 tests/test_semantic_provider.py ─covers─>  providers/semantic.py
                                             indexer.py
                                             searcher.py
                                             semantic_db.py

 tests/test_e2e.py             ──covers──>  gateway.py (full pipeline)
                                            providers/ (all, via gateway)
                                            indexer.py (real file walk)
```

## Happy path: a query from agent to result

```
 Agent                Gateway              Provider            Index
   │                     │                    │                  │
   │── code.query ──────▶│                    │                  │
   │   op=search          │── build_result ──▶│                  │
   │                      │   op=search        │── Indexer ──────▶│
   │                      │                   │   (incremental)  │
   │                      │                   │◀── chunks ───────│
   │                      │                   │                  │
   │                      │                   │── Searcher.KNN ──▶│
   │                      │                   │◀── ranked hits ──│
   │                      │◀── {ok,result} ───│                  │
   │◀── {ok,result} ──────│                   │                  │
```

## Fallback: safe-null on any failure

```
 Any failure at any layer
          │
          ▼
     caught by try/except
          │
          ▼
  {ok: true, result: null, reason: "<why>"}
          │
          ▼
  Agent degrades to grep — never crashes
```
