# HAPPY FLOW — In-House Semantic Search

Feature: `05-semantic-engine`  
Goal: In-house semantic search

---

## Phase 1 — DB schema initializes cleanly

**Actor:** Builder / test runner  
**Input:** Fresh environment with `sqlite-vec` and `fastembed` installed.

1. `SemanticDb("~/.codeintel/semantic.db").init()` is called.
2. sqlite-vec extension loads via `sqlite_vec.load(conn)`.
3. `code_embeddings` vec0 table is created (or already exists — idempotent).
4. `chunk_hashes` metadata table is created (or already exists — idempotent).
5. Init returns cleanly with no exceptions.

**Outcome:** DB file exists at `~/.codeintel/semantic.db`; both tables present.

---

## Phase 2 — Indexer walks repo and embeds new chunks

**Actor:** `Indexer.index(project_root)`  
**Input:** A repo root with Python source files.

1. Indexer walks the repo, skipping `__pycache__`, `.git`, `node_modules`, `*.egg-info`.
2. For each source file, it windows lines into 20-line chunks with 10-line overlap.
3. For each chunk, it computes `sha256[:16]` of the chunk text.
4. Looks up the chunk_id in `chunk_hashes`; if hash matches, skips embedding (unchanged).
5. New chunks are collected and embedded in batches of 32 via `fastembed.TextEmbedding`.
6. Embeddings are written to `code_embeddings`; hashes to `chunk_hashes`.
7. Returns `N` = count of newly embedded chunks.

**Outcome:** `code_embeddings` has all current chunks; `chunk_hashes` has their hashes. Second call on same repo returns `0`.

---

## Phase 3 — Searcher finds relevant code

**Actor:** `Searcher.search(query, project_root, k=10, cosine_floor=0.25)`  
**Input:** Natural-language query, e.g. `"where is authentication handled?"`.

1. Query is embedded via the same fastembed model.
2. KNN query issued against `code_embeddings` with `LIMIT 10`.
3. Results joined with `chunk_hashes` to get `file_path` and `chunk_start`.
4. For each result, cosine similarity computed: `score = 1.0 - dist`.
5. Results with `score < cosine_floor` (0.25) are dropped.
6. Remaining results sorted by score descending.
7. Snippet is read from the source file (`chunk_start` to `chunk_start + 5` lines).
8. Returns list of `{"path", "line", "snippet", "score"}` dicts.

**Outcome:** Non-empty list of ranked matches above the cosine floor.

---

## Phase 4 — SemanticProvider wires everything together

**Actor:** `Gateway.query(op="search", target="where is auth handled?", engine="semantic", project_root="/repo")`  
**Input:** Upstream agent calls `code.query` MCP tool.

1. Gateway routes `op=search` to `SemanticProvider` (already wired in `server.py`).
2. `SemanticProvider.available` is `True` (deps installed).
3. `build_result("search", "where is auth handled?", [], 0, "/repo")` is called.
4. `SemanticDb` init → `Indexer.index("/repo")` (skips all chunks on second call) → `Searcher.search(...)`.
5. Matches formatted as `"path/to/file.py:40 | def authenticate(user, token):\n  ..."`.
6. Result dict returned: `{ok: True, op: "search", engine: "semantic", result: "<matches>", cached: False}`.

**Outcome:** Agent receives a ranked, readable list of `path:line | snippet` matches. Total latency on warm index: < 2s.

---

## Phase 5 — Tests green, CI passes

**Actor:** `pytest tests/test_semantic_provider.py -v`  
**Input:** All 5 phases implemented.

1. Tests import `SemanticProvider`, `Indexer`, `Searcher`, `SemanticDb`.
2. Each test uses a tmp dir or `:memory:` DB — no side effects.
3. All 7 test cases pass; no warnings about missing deps.
4. Full suite (`pytest tests/ -x -q`) still passes (no regressions in gateway or LSP tests).

**Outcome:** CI green; feature ships.
