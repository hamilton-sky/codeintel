# SemanticProvider Reference

Embedding-based semantic code search backed by `fastembed` and `sqlite-vec`. Indexes source
files on demand and searches via cosine similarity. Never raises — always returns an envelope.

## Install prerequisite

`fastembed` and `sqlite-vec` must be importable (checked at module load via `try/except`).
Both are declared in the package's regular dependencies — `pip install -e .` (or the
published package) installs them automatically. No separate binary or PATH entry is required.

If either import fails, `available` is `False` and every call returns a **safe null** —
`ok` stays `true`; `result` is `null` and `reason: 'engine-unavailable'` carries the failure.

## What is not indexed

Retrieval quality is set by the corpus before it is set by the ranking. Excluded at index time:

- **Generated and vendored trees** — `dist/`, `build/`, `out/`, `target/`, `vendor/`,
  `third_party/`, `node_modules/`, `site-packages/`, `.next/`, `.nuxt/`, and friends.
- **Retired trees** — `.archive/`, `_archive/`, `.backup/`, `.old/`, `.deprecated/`.
- **Binary files wearing a source extension** — detected the way `git` does, by a NUL byte in the
  opening block. A compiled artifact named `.py` was otherwise embedded as replacement-character
  garbage that then competed for rank against real code.
- **Chunks with no word content** — a run of `---`, `===`, a lone `#` or brace. Such a chunk
  embeds to a vector that matches everything weakly, so it surfaces for any query.

The effect is not marginal. Re-indexing a 14,707-file monorepo after these landed took it from
**40,121 chunks / 208 MB to 18,133 chunks / 47 MB**, and moved the top hit for "websocket reconnect
logic" from an archived markdown file's blank line to `connect(wsUrl, accessToken, …)`.

> These apply at **index time**, so an already-indexed repository keeps its old chunks until you
> re-run `codeintel index <repo>`.


## Supported ops

| op | Description |
|---|---|
| `search` | Semantic similarity search over the indexed project |
| `context` | Alias for `search` — semantic's contribution to the `context` fan-out |

Any other `op` returns a safe null (`ok` stays `true`) with `reason: 'op-not-supported'`.

## Indexing

Indexing is triggered automatically on the first `search` call against a cold project (no index
yet). What happens next depends on the transport:

- **One-shot CLI** (`codeintel query`) — indexes **inline, synchronously**, with a live progress
  display, and answers the same call from the now-warm index. It has no other mechanism to ever
  build a cold index, and a human or agent waiting on one CLI invocation can afford to wait for it.
- **Long-lived MCP stdio / HTTP server** — a full cold pass is minutes long on a real repo (plus a
  one-time embedding-model download), which would block a request thread past any client tool
  timeout. Instead the server starts the pass **in the background** and returns immediately with
  `reason: 'indexing-in-progress'` (see below); a retry a short while later is served from the
  index once the pass lands. Concurrent queries against the same cold project root only ever start
  one background pass, not one per query.

Once an index exists, a warm repo is kept current by the debounced background
[`Reindexer`](architecture.md#freshness--reindex-seam) instead (`CODEINTEL_REINDEX=off` reverts to
indexing inline on every query, which then applies to both transports the same way).

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

`chunk_symbol` records the definition a chunk sits inside (the innermost one), or `NULL` at module
level and for files that could not be parsed — see **Result previews** below.

`chunk_start` is always the **0-based** start line and `chunk_end` its exclusive end, so a
syntax-aware chunk and a line chunk are schema-identical
(`chunk_id = "<project_key>:<rel_path>:<start_line>"`); a mixed index (some of each) is valid and
converges on the next re-index via orphan reconciliation (below). `chunk_end` records the span the
`content_hash` was taken over, which is what lets search verify a hit still describes the code it
was indexed from (see **Staleness verification**). Caches written before the column existed carry
`NULL` and are migrated by `ALTER` rather than a rebuild: an ordinary index pass backfills each
span in place, with no re-embedding.

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
                    rerank="on", rerank_candidates=60)
```

- Retrieve a cosine candidate set (`rerank_candidates`, default 60 — but never fewer than `k`, and
  hard-capped at 200 so a misconfigured value can't blow up a query) by cosine distance (via
  `vec_distance_cosine`), then return the top `k` after reranking. This key is the single knob for
  candidate breadth: the provider used to apply its own hardcoded widening on top, which silently
  swallowed any configured value at or below it (the documented default of 30 changed nothing, and
  the cap was bypassed).
- Results with `score = 1.0 - cosine_distance < 0.25` are dropped (below the floor). The floor
  gates the **cosine** candidate set, so reranking only re-orders what pure cosine already judged
  good enough — quality can't regress below the pure-cosine path.
- Each result includes: `path` (relative), `line` (chunk start), `snippet` (5 lines), `score`
  (the **cosine** similarity — the list order reflects the rerank, the score stays interpretable).
- An empty index (zero rows in `chunk_hashes`) is surfaced as `reason: 'no-index'` — before any
  search runs. `below-floor` is reserved for a non-empty index that yielded no match above the
  cosine floor.
- An inline pass that **ran and failed** — a blocked model download, an unwritable cache — is
  `reason: 'index-failed'`, never `'no-index'`, and the failure's own message travels in the `hint`.
  The two license opposite next steps ("nothing to find here" versus "the engine could not be
  asked"), and `Indexer.index` keeps the cause on `last_error` precisely so the answer can carry it
  instead of leaving it in a log line the reader has already scrolled past.

### Hybrid rerank

Cosine alone under-ranks exact lexical/symbol matches (searching `parse_config` when a
differently-worded but semantically-near chunk out-scores the literal match). With `rerank = "on"`
(the default), each candidate's chunk text — already read by staleness verification, so rerank
costs no additional reads — is scored two more ways:

- a **lexical** score — the fraction of query (sub)tokens present in the chunk (identifiers are
  split on camelCase/snake_case, so `parse` matches `parse_config`);
- a **symbol boost** — an additive bonus when the query is a single identifier that appears as a
  `def`/`class` name (full) or a standalone word (half).

The semantic rank and lexical rank are fused with Reciprocal Rank Fusion
(`1/(60+rank_sem) + 1/(60+rank_lex)`) plus the symbol boost, and the candidates are re-sorted.
When no candidate has any lexical overlap the lexical rank mirrors the semantic rank, so the
cosine order is returned unchanged. Everything is bounded (≤ `rerank_candidates` file reads, one
per candidate, shared with verification) and never raises — a missing/edited file scores that candidate 0 rather than failing the query, and
any rerank fault falls back to the cosine order. `rerank = "off"` restores the exact pure-cosine
path.

### Result previews

A hit renders as `path:line | <first meaningful line of the chunk>`. That is enough when a chunk
starts at a definition, but a def longer than `max_chunk_lines` is window-split, so most of its
chunks open mid-body and the first meaningful line is whatever the window happened to start on:

```
src/codeintel/searcher.py:373 | continue
src/codeintel/searcher.py:383 | except Exception as exc:
```

Correctly located, and useless — nothing says which function that is. Measured with `ast` across
the indexed repositories, **11–33% of Python chunks start strictly inside a definition** rather
than at one.

The parser already knows the enclosing definition when the chunk is cut, so it is recorded in
`chunk_hashes.chunk_symbol` and the preview leads with it when the line does not already name it:

```
src/codeintel/searcher.py:373 | search() … except Exception as exc:
```

The symbol index is a full walk of the parse tree (not the chunk spans), so the **innermost**
enclosing def wins — a method reports the method, not its class. A file that falls back to line
windowing (unparseable, or a language with no grammar) records no symbols: guessing one by scanning
backwards for a `def` would confidently name the wrong function. Backfills in place like
`chunk_end`, with no re-embedding.

### Staleness verification

A row stores a chunk's **line number**, and the snippet is re-read from the file at query time. On
its own that means an edited file silently reassigns a hit: delete `charge_credit_card()` from line
1 and a search for "charge the credit card" returned `app.py:1 | import logging` — ranked first and
reported as `confidence: complete`. That is worse than an empty result, because nothing in the
answer invites doubt.

Every candidate is therefore verified before it is ranked or shown. Its recorded span
`[chunk_start, chunk_end)` is re-read and re-hashed with `semantic_db.chunk_content_hash` (the same
function the indexer wrote it with — one definition, so the two sides cannot drift):

- **Hash matches** → the chunk still describes the code it was indexed from; it is kept, and the
  text is reused for reranking and the snippet, so verification costs no extra reads.
- **Hash differs** → the row is stale and is **dropped**, not annotated: a footnote would still put
  a wrong `path:line` in front of the caller. Dropped hits are counted and reported as a
  `freshness` / `stale-chunks-dropped` gap, which marks the answer `confidence: partial` — a
  thinned list is never passed off as a complete one. If *every* match was stale the result is
  `reason: 'index-stale'`, distinct from `'below-floor'` ("nothing was similar enough"), because
  telling an agent that code it just edited does not exist is the most damaging failure available.
- **`chunk_end IS NULL`** (a cache predating the column) → unverifiable, kept as before. An index
  pass backfills the span in place without re-embedding.

Verification is per-chunk, not per-file: editing one function does not blind the rest of its
module. It never raises — a fault in verification returns the candidates unverified rather than
returning nothing.

## Safe-null reasons

| reason | When returned |
|---|---|
| `'op-not-supported'` | `op` is not `'search'` |
| `'engine-unavailable'` | `fastembed` or `sqlite-vec` not importable |
| `'no-project-root'` | `project_root` is empty or falsy |
| `'no-index'` | The index is empty for this project (zero rows in `chunk_hashes`) after an inline index pass **completed** and found nothing to embed. The CLI always takes this path on a cold repo; the server does too when `CODEINTEL_REINDEX=off` (see **Indexing** above) — otherwise the server reports `'indexing-in-progress'` instead, below |
| `'index-failed'` | An inline index pass ran and could not finish, so there is still no index. The `hint` carries the underlying cause. A **could-not-ask** reason, not a finding: it is in the gateway's `unreachable` set, so a fan-out where it is the only outcome summarises as `engines-unavailable` with "this is NOT evidence the target does not exist" rather than as `no-result`. `'no-index'` is deliberately *not* in that set — a completed pass that found nothing IS an answer about the repository |
| `'indexing-in-progress'` | **Server transports only** (MCP stdio / HTTP): no index existed for this project, and a cold-index pass just started in the background rather than blocking this request. Not "nothing found" — retry shortly, or run `codeintel index <path>` to build it synchronously now. Never returned by the CLI, which indexes inline instead (see **Indexing** above) |
| `'below-floor'` | A non-empty index yielded no match above the cosine floor |
| `'index-stale'` | Matches were found, but every one failed staleness verification — the files changed since indexing. Distinct from `'below-floor'`: the code may well exist, the index just no longer locates it. Re-index to restore. |
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

On failure `ok` stays `true`; `result` is `null` and `reason` carries the failure.
