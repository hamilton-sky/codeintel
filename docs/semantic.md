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

- **Extensions**: `.py` `.md`; TS/JS `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs`; `.go` `.rs` `.java`;
  C/C++ `.c` `.h` `.cpp` `.cc` `.cxx` `.hpp` `.hh`
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

  **Non-Python languages** — TypeScript/TSX, JavaScript, Go, Rust, Java, C, and C++ — are chunked
  the same way via **tree-sitter** (`tree-sitter-language-pack`): functions/methods become whole
  chunks and classes/impls/traits/namespaces decompose into a header plus one chunk per member,
  through the exact same `_cover` pipeline. tree-sitter is a normal dependency but never a hard
  requirement: if it (or a grammar) is unavailable, or a language isn't mapped, the file falls back
  to line windowing.

  Anything else (e.g. `.md`), any file that fails to parse (`SyntaxError`, a NUL byte, an
  unavailable grammar), silently falls back to line windowing — the never-raise contract holds per
  file, so one malformed file never aborts the pass. tree-sitter is error-tolerant, so a
  syntactically-broken source still yields a partial tree (useful spans) rather than a fallback.

  A chunk is keyed by its **start line**, so two definitions on the *same* physical line (dense
  single-line `.d.ts` interfaces, minified/bundled JS) can't be split — they collapse into one
  line-granular chunk. This is an inherent limit of line-based chunking, not a bug; coverage stays
  complete and chunk ids stay unique.
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

`~/.codeintel/semantic.db` for the default model (created automatically on first use). The cache is
**partitioned by embedding model**: a repo configured with a non-default `model` (`.codeintel.toml`)
uses its own `~/.codeintel/semantic-<hash>.db`. A sqlite-vec vec0 table is single-dimension and
different models' vectors are incompatible, so isolating them by file means different-model repos
can never corrupt or wipe each other; the vec0 table is sized to the model's real vector length on
first embed. Rows are still partitioned by `project_root` within a shared-model file. `codeintel
reset` sweeps every model file; changing a repo's `model` just switches it to a different file
(reclaim the old one with `codeintel reset`).

## Search behaviour

```python
Searcher(db).search(query, project_root, k=10, cosine_floor=0.25,
                    rerank="on", rerank_candidates=30)
```

- Retrieve a cosine candidate set (`rerank_candidates`, default 30 — but never fewer than `k`, and
  hard-capped so a misconfigured value can't blow up a query) by cosine distance (via
  `vec_distance_cosine`), then return the top `k` after reranking.
- Results with `score = 1.0 - cosine_distance < 0.25` are dropped (below the floor). The floor
  gates the **cosine** candidate set, so reranking only re-orders what pure cosine already judged
  good enough — quality can't regress below the pure-cosine path.
- Each result includes: `path` (relative), `line` (chunk start), `snippet` (5 lines), `score`
  (the **cosine** similarity — the list order reflects the rerank, the score stays interpretable).
- An empty index (zero rows in `chunk_hashes`) returns an empty list immediately, which is
  then surfaced as `reason: 'below-floor'`.

### Hybrid rerank

Cosine alone under-ranks exact lexical/symbol matches (searching `parse_config` when a
differently-worded but semantically-near chunk out-scores the literal match). With `rerank = "on"`
(the default), each candidate's chunk text is re-read (bounded — up to 40 lines from its start,
capped at the next candidate in the same file) and scored two more ways:

- a **lexical** score — the fraction of query (sub)tokens present in the chunk (identifiers are
  split on camelCase/snake_case, so `parse` matches `parse_config`);
- a **symbol boost** — an additive bonus when the query is a single identifier that appears as a
  `def`/`class` name (full) or a standalone word (half).

The semantic rank and lexical rank are fused with Reciprocal Rank Fusion
(`1/(60+rank_sem) + 1/(60+rank_lex)`) plus the symbol boost, and the candidates are re-sorted.
When no candidate has any lexical overlap the lexical rank mirrors the semantic rank, so the
cosine order is returned unchanged. Everything is bounded (≤ `rerank_candidates` file reads) and
never raises — a missing/edited file scores that candidate 0 rather than failing the query, and
any rerank fault falls back to the cosine order. `rerank = "off"` restores the exact pure-cosine
path.

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
