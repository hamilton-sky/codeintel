# PLAN ARCHITECTURE — In-House Semantic Search

Feature: `05-semantic-engine`  
Goal: In-house semantic search

---

## Design Decisions

### DD1 — fastembed over sentence-transformers

**Decision:** Use `fastembed` (ONNX runtime) instead of `sentence-transformers` (PyTorch).  
**Rationale:** `sentence-transformers` pulls in torch (~2 GB). `fastembed` uses ONNX (~50 MB for runtime + model), downloads the model to `~/.cache/fastembed/` on first use, then runs fully offline. Both produce compatible 384-dim vectors for `BAAI/bge-small-en-v1.5`.  
**Trade-off:** `fastembed` is less flexible for custom models, but the SPEC only requires one local model in v1.

### DD2 — sqlite-vec vec0 table over a dedicated vector DB

**Decision:** Use `sqlite-vec` (vec0 virtual table) embedded in SQLite instead of chroma, hnswlib, or pgvector.  
**Rationale:** No separate process, no extra install step beyond `pip install sqlite-vec`. The code graph engine (feature F2) already uses SQLite (codebase-memory-mcp). Keeping everything in SQLite simplifies the install story.  
**Trade-off:** vec0 does not support dynamic HNSW. At scale (>100k chunks), an HNSW library (hnswlib) would be faster. For v1 (single-repo, capped at 500 chunks/file), linear KNN over vec0 is acceptable.

### DD3 — Content-hash keying on chunks, not files

**Decision:** Hash the chunk text (not the file modification time) as the dedup key.  
**Rationale:** mtime is unreliable across git checkouts and CI. A content hash is stable across platforms and tolerates any file touch that doesn't change content.  
**Trade-off:** SHA-256 hashing adds ~1 ms per chunk during indexing. Acceptable for v1.

### DD4 — Three-module split: SemanticDb / Indexer / Searcher

**Decision:** Separate concerns into three small modules rather than one large `SemanticProvider`.  
**Rationale:** The SPEC mandates `~<= 400 lines/file`. More importantly, DB schema (Phase 1), indexing (Phase 2), and search (Phase 3) have different change rates — the cosine floor and KNN query are tunable without touching the indexer.  
**Trade-off:** Slightly more file plumbing; offset by testability — each module can be tested in isolation.

### DD5 — Cosine floor as a hard filter, not a soft re-rank

**Decision:** Drop all results below `cosine_floor=0.25` entirely. The SPEC says "weak matches return empty, not noise."  
**Rationale:** An agent receiving low-confidence matches may confidently act on wrong context. Returning empty forces the agent to use a different op (grep, LSP) rather than a misleading answer.  
**Trade-off:** If the corpus is domain-specific (rare vocabulary), even relevant results may score below 0.25. The floor is configurable in the `Searcher` constructor; the default is conservative.

### DD6 — Index on every build_result call (no background daemon)

**Decision:** `SemanticProvider.build_result` calls `Indexer.index(project_root)` on every op=search request. The indexer's content-hash dedup makes repeated calls cheap (zero new chunks on unchanged files).  
**Rationale:** No daemon, no file-watcher, no inter-process coordination. The SPEC says "incremental content-hash-keyed indexing" — that property makes on-demand indexing acceptable in v1.  
**Trade-off:** The first query on a large repo incurs the full index build latency. Feature F6 (freshness/reindex) can add background indexing in a later feature if needed.

---

## Module Map

```
src/codeintel/
├── semantic_db.py       ← Phase 1  (SemanticDb: schema, init, connection mgmt)
├── indexer.py           ← Phase 2  (Indexer: walk → chunk → hash → embed → write)
├── searcher.py          ← Phase 3  (Searcher: embed query → KNN → filter → snippets)
└── providers/
    └── semantic.py      ← Phase 4  (SemanticProvider: routes op=search, wires DB/Indexer/Searcher)
```

No changes to `gateway.py`, `server.py`, `provider.py`, `policy.py`, or `cache.py`.

---

## Phase Mapping

### Phase 1 — semantic_db.py
DB layer. No upstream imports from new code; only `sqlite3` and `sqlite_vec`. Tests can use `:memory:` for speed.

### Phase 2 — indexer.py
Imports `SemanticDb`. Imports `fastembed.TextEmbedding`. No dependency on `searcher.py` or `providers/semantic.py`.

### Phase 3 — searcher.py
Imports `SemanticDb`. Imports `fastembed.TextEmbedding`. No dependency on `indexer.py`. Both modules use the same model name constant (defined in `semantic_db.py` or as a shared constant — define `DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"` in `semantic_db.py`, import in both).

### Phase 4 — providers/semantic.py
Replaces the placeholder. Imports `SemanticDb`, `Indexer`, `Searcher`. Imports `safe_null_result` from `codeintel.provider` (unchanged). Module-level try/except for `fastembed` + `sqlite_vec` sets `_DEPS_OK`.

### Phase 5 — tests/test_semantic_provider.py
Imports all four modules. Uses `tmp_path` fixture for FS isolation. Mocks `TextEmbedding` with a deterministic stub (returns fixed 384-dim vectors) for unit tests; marks one slow integration test.
