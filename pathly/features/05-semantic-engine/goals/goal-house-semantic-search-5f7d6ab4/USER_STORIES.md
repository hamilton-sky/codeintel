# USER STORIES — In-House Semantic Search

Feature: `05-semantic-engine`  
Goal: In-house semantic search

---

## Story 1 — Semantic query returns relevant matches

**As** a coding agent,  
**I want** to call `op=search, target="where is authentication handled?"` via `code.query`,  
**So that** I can find relevant code without knowing the exact symbol name.

### Acceptance Criteria

- AC1.1: A natural-language query returns a non-null result list containing `path:line` + a snippet when the index has relevant code.
- AC1.2: Results are ranked by cosine similarity (highest first).
- AC1.3: Matches below the cosine floor threshold are excluded; if all results are below the floor, the result list is empty (not null — the result dict is returned with an empty list).
- AC1.4: The response conforms to the `Result` TypedDict: `ok=True`, `engine="semantic"`, `result` is a formatted string of ranked matches or `None` if truly empty.

---

## Story 2 — Incremental indexing skips unchanged files

**As** a developer running a large repo,  
**I want** re-indexing to be fast when nothing has changed,  
**So that** repeated agent queries don't re-embed the entire codebase.

### Acceptance Criteria

- AC2.1: Re-indexing a repo where all source file contents are unchanged embeds zero new chunks (all content hashes already in `chunk_hashes`).
- AC2.2: Modifying one file causes only the chunks from that file to be re-embedded; unchanged files are untouched.
- AC2.3: Deleted files have their chunks removed from the vec0 table on the next indexing pass.

---

## Story 3 — Safe-null on failure or empty state

**As** a coding agent depending on safe-null,  
**I want** op=search to never raise an exception,  
**So that** the agent degrades to grep rather than crashing.

### Acceptance Criteria

- AC3.1: An empty index (no files indexed yet) returns `result=None` with `reason="no-index"` (not an error).
- AC3.2: A query where all KNN results fall below the cosine floor returns `result=None` with `reason="below-floor"`.
- AC3.3: Any internal exception (missing dep, DB corruption, model load failure) returns a safe-null result dict — never raises.
- AC3.4: A chunk cap drop (file exceeds `MAX_CHUNKS`) is logged at DEBUG level; the file is still partially indexed up to the cap.

---

## Story 4 — Engine is locally available, no API keys needed

**As** a privacy-conscious user,  
**I want** the semantic engine to use a local embedding model,  
**So that** no code leaves the machine.

### Acceptance Criteria

- AC4.1: `SemanticProvider.available` is `True` when `fastembed` and `sqlite-vec` are installed.
- AC4.2: `SemanticProvider.available` is `False` when either dep is missing; op=search returns `reason="engine-unavailable"` (not an error).
- AC4.3: The first index run downloads the embedding model if not already cached; subsequent runs are fully offline.
- AC4.4: `code_status` reports `semantic: true` when the provider is available.
