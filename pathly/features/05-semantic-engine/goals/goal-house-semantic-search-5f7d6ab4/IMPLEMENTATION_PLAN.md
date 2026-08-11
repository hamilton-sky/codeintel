# IMPLEMENTATION PLAN — In-House Semantic Search

Feature: `05-semantic-engine`  
Goal: In-house semantic search  
Rigor: standard

---

## Conversation 1 — Foundation: DB schema + line-window indexer

Leaves codebase runnable: yes (SemanticProvider still returns unavailable; gateway unchanged).  
Do NOT touch `providers/semantic.py` yet — that is Conv 2.  
Do NOT touch `server.py` — no wiring changes needed in this conversation.

Verify: `python -m pytest tests/ -x -q` passes (no regressions).

---

### Phase 1 — Dependencies + DB schema

**File:** `pyproject.toml` AND `src/codeintel/semantic_db.py`

**Purpose:** Install the two new library deps and create the DB layer that the indexer and searcher share.

**Depends on:** Nothing (foundational). `pyproject.toml` already exists.

**Enables:** Phase 2 (indexer imports `SemanticDb`); Phase 3 (searcher uses the same DB).

**Done when:** `semantic_db.py` can be imported cleanly; `SemanticDb(db_path).init()` creates a SQLite DB file with a `code_embeddings` vec0 table (dimension=384, metric=cosine) and a `chunk_hashes` table (`chunk_id TEXT PRIMARY KEY, file_path TEXT, chunk_start INT, content_hash TEXT`). Running `python -c "from codeintel.semantic_db import SemanticDb; SemanticDb(':memory:').init()"` exits without error.

**Verify:** `python -c "from codeintel.semantic_db import SemanticDb; SemanticDb(':memory:').init(); print('ok')"`

**Implementation notes:**

1. Add to `pyproject.toml` dependencies: `"sqlite-vec>=0.1"`, `"fastembed>=0.3"`.
2. Create `src/codeintel/semantic_db.py` with class `SemanticDb`:
   - `__init__(self, db_path: str)` — accepts file path or `:memory:`.
   - `init(self) -> None` — loads sqlite-vec extension (`sqlite_vec.load(conn)`), creates `code_embeddings` vec0 table with 384 dimensions, creates `chunk_hashes` metadata table, commits. Wraps everything in try/except; raises `RuntimeError` only if the extension load fails (caller catches it).
   - `dimension` class constant: `384` (matches `BAAI/bge-small-en-v1.5` from fastembed, 384-dim).
3. `chunk_hashes` schema:
   ```sql
   CREATE TABLE IF NOT EXISTS chunk_hashes (
       chunk_id   TEXT PRIMARY KEY,
       file_path  TEXT NOT NULL,
       chunk_start INT NOT NULL,
       content_hash TEXT NOT NULL
   )
   ```
4. `code_embeddings` vec0 schema (sqlite-vec syntax):
   ```sql
   CREATE VIRTUAL TABLE IF NOT EXISTS code_embeddings USING vec0(
       chunk_id TEXT PRIMARY KEY,
       embedding FLOAT[384]
   )
   ```
5. Keep file under 200 lines.

---

### Phase 2 — Line-window indexer

**File:** `src/codeintel/indexer.py`

**Purpose:** Walk source files, window lines into chunks, compute content hashes, skip unchanged, embed new chunks, write to DB.

**Depends on:** Phase 1 (`SemanticDb` exists).

**Enables:** Phase 4 (SemanticProvider calls `Indexer.index(project_root)` before search).

**Done when:** `Indexer(db).index(project_root)` walks all `.py` files under `project_root`, chunks them by 20-line windows with 10-line overlap, skips chunks whose `content_hash` is already in `chunk_hashes`, embeds new chunks via fastembed, writes embeddings to `code_embeddings` and hashes to `chunk_hashes`. Calling `index()` twice on an unchanged repo writes zero new rows (verified by checking `chunk_hashes` row count before/after).

**Verify:** (test in Phase 5) — no standalone verify command for this phase; `pytest tests/test_semantic_provider.py::test_unchanged_repo_skips_embed` covers it.

**Implementation notes:**

1. Create `src/codeintel/indexer.py` with class `Indexer`:
   - `__init__(self, db: SemanticDb, model_name: str = "BAAI/bge-small-en-v1.5", window: int = 20, stride: int = 10, max_chunks: int = 500)`
   - `index(self, project_root: str) -> int` — returns count of new chunks embedded; logs dropped chunks at DEBUG.
2. Chunk ID format: `f"{rel_path}:{chunk_start}"` (stable, deterministic).
3. Content hash: `hashlib.sha256(chunk_text.encode()).hexdigest()[:16]`.
4. Skip extensions: index only `.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.c`, `.cpp`, `.h`, `.md` — ignore `__pycache__`, `.git`, `node_modules`, `*.egg-info`.
5. Fastembed usage: `TextEmbedding(model_name=self.model_name)` — lazy init on first `index()` call. Embed in batches of 32.
6. On `max_chunks` hit per file: log `DEBUG` "chunk cap hit for {rel_path}, truncating at {max_chunks}" and continue to next file.
7. Deletion cleanup: before indexing, collect current `chunk_id`s for files that no longer exist; delete from both tables.
8. Never raises — wraps all I/O and embedding calls in try/except; returns `-1` on unrecoverable init failure (caller treats as engine-unavailable).
9. Keep file under 400 lines.

**Recovery:** If verify fails and the fix requires changing `SemanticDb`, stop and report. If fundamentally broken, `git checkout src/codeintel/indexer.py` and retry.

---

## Conversation 2 — Integration: searcher + real provider + tests

Depends on: Conversation 1 complete (`semantic_db.py` and `indexer.py` exist and pass verify).  
Do NOT touch `gateway.py` or `server.py` — the wiring is already correct.  
Do NOT touch `indexer.py` or `semantic_db.py` unless a bug is found during testing.

Verify: `python -m pytest tests/ -x -q` passes with semantic tests included.

---

### Phase 3 — KNN searcher with cosine floor

**File:** `src/codeintel/searcher.py`

**Purpose:** Embed the natural-language query and run a KNN search on the vec0 table, filtering results below the cosine floor.

**Depends on:** Phase 1 (`SemanticDb`) and Phase 2 (`Indexer` confirms DB is populated).

**Enables:** Phase 4 (SemanticProvider calls `Searcher.search()`).

**Done when:** `Searcher(db).search(query, k=10, cosine_floor=0.25)` embeds `query`, issues a KNN query against `code_embeddings`, joins on `chunk_hashes` to get `file_path` + `chunk_start`, filters out results where distance > `(1 - cosine_floor)` (vec0 uses L2/inner-product depending on config — see note), and returns a list of `{"path": str, "line": int, "snippet": str, "score": float}` dicts sorted by score descending. Empty list if no match clears the floor.

**Verify:** (covered by Phase 5 tests)

**Implementation notes:**

1. Create `src/codeintel/searcher.py` with class `Searcher`:
   - `__init__(self, db: SemanticDb, model_name: str = "BAAI/bge-small-en-v1.5")`
   - `search(self, query: str, project_root: str, k: int = 10, cosine_floor: float = 0.25) -> list[dict]`
2. sqlite-vec KNN query (cosine distance via `vec_distance_cosine`):
   ```sql
   SELECT c.chunk_id, c.chunk_start, c.file_path,
          vec_distance_cosine(e.embedding, ?) AS dist
   FROM code_embeddings e
   JOIN chunk_hashes c ON e.chunk_id = c.chunk_id
   ORDER BY dist
   LIMIT ?
   ```
   - Score = `1.0 - dist` (cosine similarity). Filter: `score >= cosine_floor`.
3. Retrieve snippet: read `chunk_start` to `chunk_start + 5` lines from the source file. If the file is gone, use `"[file not found]"` as snippet (never raise).
4. Result format per match: `f"{file_path}:{chunk_start} | {snippet_first_line}"` — builder formats this into the final `result` string.
5. Returns empty list (not None) on zero matches above floor — the provider converts that to `result=None, reason="below-floor"`.
6. Never raises — all SQL and file I/O wrapped in try/except; on error returns `[]`.
7. Keep file under 300 lines.

**Recovery:** If verify fails and requires Schema changes, stop and report. `git checkout src/codeintel/searcher.py` if fundamentally broken.

---

### Phase 4 — Real SemanticProvider

**File:** `src/codeintel/providers/semantic.py`

**Purpose:** Replace the always-unavailable placeholder with a real provider that indexes on first search and answers op=search queries.

**Depends on:** Phase 2 (`Indexer`) and Phase 3 (`Searcher`).

**Enables:** Phase 5 (tests against the full provider); gateway auto-routes `op=search` to semantic engine.

**Done when:** `SemanticProvider().available` is `True` when `fastembed` and `sqlite-vec` are installed. Calling `build_result("search", "where is auth handled?", [], 0, "/some/repo")` returns a `Result` dict with `ok=True`, `engine="semantic"`, and `result` containing a formatted match string (or `None` with a reason if no matches). Any exception returns a safe-null result — never raises.

**Verify:** `python -c "from codeintel.providers.semantic import SemanticProvider; sp = SemanticProvider(); print('available:', sp.available)"`

**Implementation notes:**

1. At module load, attempt `import fastembed; import sqlite_vec` in a try/except. If either import fails, set module-level `_DEPS_OK = False`.
2. `SemanticProvider.available` is a property: returns `_DEPS_OK`.
3. DB path: `~/.codeintel/semantic.db` (created on first use). Use `pathlib.Path.home() / ".codeintel" / "semantic.db"`.
4. `build_result(op, target, files, budget, project_root)`:
   - If `op != "search"`: return `safe_null_result(op, target, engine="semantic", reason="op-not-supported")`.
   - If not `available`: return `safe_null_result(op, target, engine="semantic", reason="engine-unavailable")`.
   - If `project_root` is empty: return `safe_null_result(op, target, engine="semantic", reason="no-project-root")`.
   - Otherwise: init `SemanticDb`, call `Indexer(db).index(project_root)`, call `Searcher(db).search(target, project_root)`.
   - Format matches into a result string: `"\n".join(f"{m['path']}:{m['line']} | {m['snippet']}" for m in matches)`.
   - If matches is empty: return `safe_null_result(op, target, engine="semantic", reason="below-floor")`.
   - Wrap entire body in try/except — any exception → `safe_null_result(reason="provider-error")`.
5. Do NOT modify `server.py` — it already instantiates `SemanticProvider()`.
6. Keep file under 120 lines (it delegates everything to `SemanticDb`, `Indexer`, `Searcher`).

**Recovery:** `git checkout src/codeintel/providers/semantic.py` restores the safe placeholder.

---

### Phase 5 — Tests

**File:** `tests/test_semantic_provider.py`

**Purpose:** Verify all acceptance criteria: op=search returns matches, unchanged repo skips re-embed, safe-null on empty/below-floor, provider availability check.

**Depends on:** Phases 1–4 complete.

**Enables:** Feature acceptance; CI green.

**Done when:** `pytest tests/test_semantic_provider.py -x -q` passes with all tests green. All 4 acceptance criteria from USER_STORIES.md are covered by at least one test.

**Verify:** `python -m pytest tests/test_semantic_provider.py -v`

**Test cases to implement:**

1. `test_available_when_deps_present` — mock `fastembed` and `sqlite_vec` imports; assert `SemanticProvider().available is True`.
2. `test_search_returns_matches` — create a temp dir with a `.py` file; call `build_result("search", "test query", [], 0, tmp_dir)`; assert `result` is not None and contains the file path.
3. `test_unchanged_repo_skips_embed` — index a temp dir twice; assert `new_chunks` count from second `Indexer.index()` call is `0`.
4. `test_empty_index_safe_null` — call `build_result("search", "query", [], 0, empty_tmp_dir)`; assert `result is None` and `reason` is present.
5. `test_below_floor_returns_none` — mock `Searcher.search` to return `[]`; assert provider returns `result=None, reason="below-floor"`.
6. `test_unsupported_op_safe_null` — call `build_result("callers", "foo", [], 0, "/")` ; assert `result is None, reason="op-not-supported"`.
7. `test_provider_never_raises` — mock internals to raise; assert `build_result(...)` returns a dict (not raises).

**Recovery:** If tests reveal a bug in Phase 1–4 code, fix the upstream file. Do not add workarounds to tests.
