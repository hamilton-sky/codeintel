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

### Chunking parameters

| Parameter | Value |
|---|---|
| Window (lines per chunk) | 20 |
| Stride (lines between chunk starts) | 10 |
| Max chunks per file | 500 |

### Incremental strategy

Each chunk is keyed by `"<rel_path>:<line_start>"`. The chunk text is hashed with SHA-256
(first 16 hex chars stored). A chunk is re-embedded only when its hash changes; unchanged
chunks are skipped. Deleted files are pruned from the DB before each index pass.

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
