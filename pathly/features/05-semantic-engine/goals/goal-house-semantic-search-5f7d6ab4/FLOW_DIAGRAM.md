# FLOW DIAGRAM — In-House Semantic Search

Feature: `05-semantic-engine`  
Goal: In-house semantic search

---

## Happy Path — op=search request

```
Agent / MCP client
    │
    │  code.query(op="search", target="...", engine="semantic", project_root="/repo")
    ▼
┌─────────────────────────────────────────────────────────────┐
│ server.py  →  Gateway.query()                               │
│   engine="semantic" → auto-routes to SemanticProvider       │
└───────────────────────┬─────────────────────────────────────┘
                        │ build_result("search", target, project_root)
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ providers/semantic.py  SemanticProvider                     │
│   available? ──No──→  safe_null(reason="engine-unavailable")│
│   └─ Yes                                                    │
│   project_root empty? ─Yes─→ safe_null(reason="no-project-root")
│   └─ No                                                     │
│   SemanticDb.init()                                         │
└──────────┬──────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│ indexer.py  Indexer.index(project_root)                     │
│   walk .py/.ts/... files                                    │
│   for each file:                                            │
│     chunk lines (window=20, stride=10)                      │
│     compute sha256[:16] per chunk                           │
│     chunk_id in chunk_hashes? ──Yes──→ skip (unchanged)     │
│     └─ No: collect new chunks                               │
│   embed new chunks in batches of 32  (fastembed)            │
│   write embeddings → code_embeddings (vec0)                 │
│   write hashes → chunk_hashes                               │
│   return N (new chunks embedded)                            │
└──────────┬──────────────────────────────────────────────────┘
           │ index ready
           ▼
┌─────────────────────────────────────────────────────────────┐
│ searcher.py  Searcher.search(query, project_root)           │
│   rowcount=0? ──→ return []  (provider: reason="no-index")  │
│   embed query  (fastembed, same model)                      │
│   KNN query on code_embeddings:                             │
│     SELECT chunk_id, file_path, chunk_start,                │
│            vec_distance_cosine(embedding, q_vec) AS dist    │
│     JOIN chunk_hashes                                       │
│     ORDER BY dist LIMIT 10                                  │
│   score = 1.0 - dist                                        │
│   filter: score < cosine_floor (0.25) → drop               │
│   for each remaining result:                                │
│     read snippet from source file (lines chunk_start..+5)  │
│   return sorted list of {path, line, snippet, score}        │
└──────────┬──────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│ providers/semantic.py  (format + return)                    │
│   matches empty? ──→ safe_null(reason="below-floor")        │
│   format: "path:line | snippet_first_line"  (one per match) │
│   return Result{ok=True, engine="semantic", result=<str>}   │
└─────────────────────────────────────────────────────────────┘
           │
           ▼
    Agent receives ranked path:line + snippets
```

---

## Fallback Path — any exception

```
Any layer raises
    │
    ▼  (try/except in SemanticProvider.build_result)
safe_null_result(op, target, engine="semantic", reason="provider-error")
    │
    ▼
Agent degrades to grep — never crashes
```

---

## Incremental Re-index (warm path)

```
Second call on unchanged repo
    │
    ▼
Indexer.index(project_root)
    │  for each chunk: hash in chunk_hashes? ──Yes──→ skip
    │  (all chunks unchanged)
    ▼
return 0  (zero new embeddings — fast, < 100ms on a typical repo)
    │
    ▼
Searcher.search(...)  [vec0 table already populated]
```
