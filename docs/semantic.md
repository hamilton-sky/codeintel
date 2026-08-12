# SemanticProvider Reference

Embedding-based semantic code search backed by `fastembed` and `sqlite-vec`. Indexes source
files on demand and searches via cosine similarity. Never raises — always returns an envelope.

## Install prerequisite

`fastembed` and `sqlite-vec` must be importable (checked at module load via `try/except`).
Both are declared in the package's regular dependencies — `pip install -e .` (or the
published package) installs them automatically. No separate binary or PATH entry is required.

If either import fails, `available` is `False` and every call returns `ok: false` with
`reason: 'engine-unavailable'`.

## Supported ops

| op | Description |
|---|---|
| `search` | Semantic similarity search over the indexed project |

Any other `op` returns `ok: false` with `reason: 'op-not-supported'`.

## Indexing

Indexing is triggered automatically on every `search` call before querying.

### What gets indexed

- **Extensions**: `.py` `.ts` `.js` `.go` `.rs` `.java` `.c` `.cpp` `.h` `.md`
- **Skipped directories**: `__pycache__`, `.git`, `node_modules` (and `*.egg-info`)

### Chunk strategy

Controlled by `chunk_strategy` (default `"syntax"`; set `"lines"` to force the legacy windower):

- **`"syntax"`** — Python (`.py`) files are cut on real definition boundaries using the stdlib
  `ast` module, so each embedding is a whole semantic unit and a search hit maps to a complete
  definition rather than an arbitrary line window:
  - each top-level `def` / `async def` → one chunk (its decorators are included in the span);
  - each top-level `class` → a **header** chunk (the class line through just before its first
    method — bases + class docstring) **plus one chunk per method / nested def** (the whole class
    body is never emitted as a chunk, so per-method chunks are never double-embedded);
  - remaining module-level and inter-method lines (imports, top-level statements) are line-windowed
    so coverage stays complete. The def-aligned chunks are whole, distinct units; the window-filled
    runs (and any oversized-def split) reuse the same `window`/`stride`, so adjacent windows inside
    one run overlap exactly as the legacy line windower does — always strictly less overlap than
    `"lines"` mode, since the def bodies are carved out first;
  - a def longer than `max_chunk_lines` (`2 × window`) is line-windowed internally so no single
    chunk overflows the embedder (`bge-small` truncates ~512 tokens).

  Anything that isn't a `.py` file, and any file that fails to parse (`SyntaxError`, a NUL byte,
  etc.), silently falls back to line windowing — the never-raise contract holds per file, so one
  malformed file never aborts the pass.
- **`"lines"`** — every file is cut into fixed overlapping windows (the pre-0.6 behaviour), a
  runtime escape hatch that reverts chunking without touching the schema.

`chunk_start` is always the **0-based** start line, so a syntax-aware chunk and a line chunk are
schema-identical (`chunk_id = "<project_key>:<rel_path>:<start_line>"`); a mixed index (some of
each) is valid and converges on the next re-index via orphan reconciliation (below).

### Chunking parameters

| Parameter | Value |
|---|---|
| Window (lines per chunk) | 20 |
| Stride (lines between chunk starts) | 10 |
| Max chunks per file | 500 |
| Max chunk lines (oversized-def split threshold) | 40 — derived as `2 × window`, not a config key |
| Chunk strategy | `syntax` \| `lines` (default `syntax`) |

### Incremental strategy

Each chunk is keyed by `"<project_key>:<rel_path>:<line_start>"`. The chunk text is hashed with
SHA-256 (first 16 hex chars stored). A chunk is re-embedded only when its hash changes; unchanged
chunks are skipped.

Two levels of cleanup keep the cache consistent as code changes:

- **Deleted files** are pruned from the DB before each index pass (scoped by project root).
- **Per-file reconciliation** — after re-chunking a file, rows for `chunk_id`s the file no longer
  produces (a function that moved or was deleted, or a chunk-strategy switch) are dropped from both
  `chunk_hashes` and `code_embeddings`. This is scoped by `(project_root, file_path)`, so it can
  only ever touch that one file's rows in that one project.

### Model

`BAAI/bge-small-en-v1.5` via `fastembed.TextEmbedding`. The same model is used for both
indexing and query embedding. Embeddings are stored as raw float32 bytes in `sqlite-vec`.

### DB location

`~/.codeintel/semantic.db` (created automatically on first use).

## Search behaviour

```python
Searcher(db).search(query, project_root, k=10, cosine_floor=0.25)
```

- Top-`k` nearest neighbours by cosine distance (via `vec_distance_cosine`).
- Results with `score = 1.0 - cosine_distance < 0.25` are dropped (below the floor).
- Each result includes: `path` (relative), `line` (chunk start), `snippet` (5 lines), `score`.
- An empty index (zero rows in `chunk_hashes`) returns an empty list immediately, which is
  then surfaced as `reason: 'below-floor'`.

## Safe-null reasons

| reason | When returned |
|---|---|
| `'op-not-supported'` | `op` is not `'search'` |
| `'engine-unavailable'` | `fastembed` or `sqlite-vec` not importable |
| `'no-project-root'` | `project_root` is empty or falsy |
| `'below-floor'` | No matches meet the cosine floor, or the index is empty |
| `'provider-error'` | Unexpected exception during indexing or searching |

## Envelope shape

```json
{
  "ok": true,
  "op": "search",
  "target": "authentication handler",
  "result": "src/auth.py:40 | def authenticate(token: str) -> bool:",
  "engine": "semantic",
  "cached": false
}
```

Each line of `result` is `<rel_path>:<line> | <first line of snippet>`.

On failure `ok` is `false` and `result` is `null`.
