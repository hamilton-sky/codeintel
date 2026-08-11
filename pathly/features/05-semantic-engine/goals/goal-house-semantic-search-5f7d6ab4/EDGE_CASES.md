# EDGE CASES — In-House Semantic Search

Feature: `05-semantic-engine`  
Goal: In-house semantic search

---

## Phase 1 — DB schema edge cases

**EC1.1 — sqlite-vec extension not installed**  
Condition: `sqlite_vec.load(conn)` raises `sqlite3.OperationalError` (`.so` missing).  
Expected: `SemanticDb.init()` catches the error and re-raises as `RuntimeError("sqlite-vec not available")`. Caller (`SemanticProvider.build_result`) catches `RuntimeError` and returns `safe_null_result(reason="engine-unavailable")`.  
Do NOT swallow silently — the caller needs to know deps are missing.

**EC1.2 — DB directory does not exist**  
Condition: `~/.codeintel/` directory does not exist when `SemanticDb` opens the path.  
Expected: `SemanticDb.__init__` creates the parent directory with `mkdir(parents=True, exist_ok=True)` before opening the connection.

**EC1.3 — DB file is corrupted**  
Condition: DB file exists but is not a valid SQLite file (e.g., truncated by a previous crash).  
Expected: `init()` catches `sqlite3.DatabaseError` and re-creates the file (delete + re-open). Log at WARNING level.

**EC1.4 — `:memory:` DB (test mode)**  
Condition: `db_path=":memory:"` — do not try to create a parent directory.  
Expected: `SemanticDb.__init__` detects `:memory:` and skips `mkdir`.

---

## Phase 2 — Indexer edge cases

**EC2.1 — Empty project root**  
Condition: `project_root=""` or the path does not exist.  
Expected: `Indexer.index("")` returns `0` without walking. Provider sees `0` new chunks, proceeds to search (which returns empty → safe-null).

**EC2.2 — Very large file (chunk cap hit)**  
Condition: A single source file produces more than `MAX_CHUNKS` (500) chunks.  
Expected: Indexer logs `DEBUG "chunk cap hit for {rel_path}, truncating at 500"` and moves on to the next file. The first 500 chunks are embedded; the rest are silently dropped (documented behavior per SPEC).

**EC2.3 — File disappears mid-walk**  
Condition: A file is deleted between the directory walk and the file read.  
Expected: `open()` raises `FileNotFoundError`; indexer catches it, logs `DEBUG "file disappeared: {path}"`, skips the file, continues.

**EC2.4 — fastembed model download fails (no network)**  
Condition: First-time index on a machine with no internet; model not cached.  
Expected: `TextEmbedding(model_name=...)` raises a network error. Indexer catches it, returns `-1`. Provider sees `-1`, returns `safe_null_result(reason="engine-unavailable")`.

**EC2.5 — Non-UTF-8 file**  
Condition: A source file contains binary content or a non-UTF-8 encoding.  
Expected: `open(..., encoding="utf-8", errors="replace")` — replacement characters are embedded (not ideal but not a crash). The chunk is indexed with a hash of the replaced text.

**EC2.6 — Deleted files cleanup**  
Condition: A file was indexed in a previous run but has been deleted.  
Expected: Indexer collects chunk_ids for paths that no longer exist on disk (from `chunk_hashes`) and deletes those rows from both `chunk_hashes` and `code_embeddings`. This runs before the new-file walk.

---

## Phase 3 — Searcher edge cases

**EC3.1 — Empty vec0 table (no indexed files)**  
Condition: `code_embeddings` has zero rows.  
Expected: KNN query returns empty result set. `search()` returns `[]`. Provider converts to `safe_null_result(reason="no-index")`.  
Note: distinguish "no rows at all" (`reason="no-index"`) from "rows exist but all below floor" (`reason="below-floor"`). Check rowcount before the KNN query.

**EC3.2 — All KNN results below cosine floor**  
Condition: KNN query returns `k` results, all with `score < cosine_floor`.  
Expected: After filtering, `search()` returns `[]`. Provider returns `safe_null_result(reason="below-floor")`.

**EC3.3 — Source file missing at snippet read time**  
Condition: A chunk is in the DB but the source file was deleted after indexing.  
Expected: `Searcher` catches `FileNotFoundError` during snippet read, uses `"[file not found]"` as the snippet, still includes the match in results.

**EC3.4 — Very short query string**  
Condition: `target=""` or `target=" "`.  
Expected: Empty/whitespace query → `safe_null_result(reason="empty-query")` without embedding. Do NOT embed empty strings (fastembed may produce a zero vector, polluting results).

**EC3.5 — k=0 or k<0**  
Condition: Caller passes invalid `k`.  
Expected: Clamp to `k = max(1, k)` silently.

---

## Phase 4 — SemanticProvider edge cases

**EC4.1 — op != "search"**  
Condition: Provider called with `op="callers"` or any non-search op.  
Expected: Immediate `safe_null_result(reason="op-not-supported")` without any indexing or DB access.

**EC4.2 — project_root missing or empty**  
Condition: `build_result("search", "query", [], 0, "")`.  
Expected: `safe_null_result(reason="no-project-root")`. Do not attempt indexing without a known root.

**EC4.3 — Missing dependency at import time**  
Condition: `fastembed` or `sqlite_vec` not installed.  
Expected: Module-level try/except sets `_DEPS_OK = False`. `SemanticProvider.available` returns `False`. `build_result` returns `safe_null_result(reason="engine-unavailable")` immediately.

**EC4.4 — Concurrent queries on same DB**  
Condition: Two agent queries arrive simultaneously (gateway uses `ThreadPoolExecutor`).  
Expected: SQLite WAL mode handles concurrent reads. `SemanticDb` opens a new connection per `build_result` call (not a shared connection). No shared mutable state in `SemanticProvider`.

---

## Phase 5 — Test edge cases

**EC5.1 — Dep-check test isolation**  
Tests that mock import failures must patch the module-level `_DEPS_OK` flag, not the imports themselves (imports already ran before the test). Use `unittest.mock.patch("codeintel.providers.semantic._DEPS_OK", False)`.

**EC5.2 — Model download in CI**  
If the test environment has no internet, `test_search_returns_matches` must skip gracefully. Use `pytest.importorskip("fastembed")` at the top of the test; or mock `TextEmbedding` to return deterministic vectors. Prefer mocking in unit tests; have exactly one integration test guarded by `pytest.mark.slow` or a skip if model is absent.
