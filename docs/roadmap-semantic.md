# Semantic engine roadmap

> **Status: both phases shipped.** This reads as a forward plan and is no longer one. Phase 1
> (syntax-aware chunking) is the **default** — `chunk_strategy = "syntax"` in `config.py`, with
> `"lines"` kept as the escape hatch. Phase 2 (hybrid reranking) is `Searcher._rerank`, on by
> default. Kept for the reasoning behind both, and for the "explicitly out of scope" decision, which
> still holds. Details in it have drifted from the code — it specifies `rerank_candidates` default 30
> where the shipped default is 60 — so where it disagrees with [semantic.md](semantic.md) or the
> source, they win.

The **semantic** engine is the only fully in-house engine (graph and LSP wrap external backends).
It's a competent MVP — fixed line-window chunking + `bge-small` cosine KNN + a floor — so it's also
where owning more of the intelligence pays off most. This doc sequences two increments that raise
retrieval quality without new heavy dependencies, and records one thing we've decided **not** to do.

Ship each phase as its own reviewed + published release, exactly like 0.2 → 0.5.

## Contracts every change must keep

- **Never-raise / safe-null.** Providers and the gateway never propagate an exception; a failure
  degrades to a safe-null with a `reason`. New parse/scoring code must fall back, never crash.
- **Backward-compatible DB.** `~/.codeintel/semantic.db` is a regenerable cache. If a change alters
  chunk identity, handle the transition (re-index repopulates; orphaned rows are cleaned — see P1).
- **Config validated.** New options go through `config.py`'s `_coerce` (bad value → default + warn).
- **Tests with each change**, real-boundary where practical; keep the suite green and fast.
- **Adversarial review before publish** (a `reviewer` pass on correctness + any new surface), then
  tag `vX.Y.Z` to trigger the OIDC PyPI publish.

---

## Phase 1 — Syntax-aware chunking  (target: 0.6.0)

### Why
Today `Indexer._collect_new_chunks` cuts every file into fixed 20-line / 10-stride windows. A window
routinely splits a function in half or straddles two, so a search hit maps to an arbitrary span, not
a semantic unit. Chunking on real definition boundaries makes each embedding — and each returned
snippet — a whole function/method/class. This is the single biggest precision win available and is
self-contained to the indexer.

### Design
- **Python via stdlib `ast`; line-window fallback for everything else and on parse failure.** No new
  dependency in this phase. `ast.parse` a `.py` file; on `SyntaxError` (or any exception) fall back to
  the current windowing for that file. Non-`.py` files keep windowing. (tree-sitter for TS/Go/… is a
  *follow-up*, behind the same interface — see "Later".)
- **Chunk units (non-overlapping):**
  - Each top-level `FunctionDef` / `AsyncFunctionDef` → one chunk spanning its line range.
  - Each `ClassDef` → a **header chunk** (the class line through just before its first method/nested
    def: bases + class docstring) **plus one chunk per method / nested def**. Do *not* emit the whole
    class body as a chunk — that would overlap the per-method chunks and double-embed them.
  - Include decorators in a def's span (use `min(decorator linenos, node.lineno)` … `end_lineno`).
  - Remaining **module-level** lines not covered by any def (imports, top-level statements, module
    docstring) → window-chunk the uncovered runs, so coverage stays complete.
- **Oversized defs:** if a def span exceeds `max_chunk_lines` (default `2 * window`), window-chunk it
  internally (reuse the existing window/stride within the span) so every chunk stays embeddable
  (bge-small truncates ~512 tokens; don't feed it a 400-line function as one chunk).
- **Schema unchanged.** A syntax-aware chunk is still `(chunk_id, text, rel_path, chunk_start,
  content_hash)`; `chunk_start` is the **0-based** start line (`node.lineno - 1`, decorators
  included) so `Searcher._read_snippet` keeps working. `chunk_id` stays `{project_key}:{rel_path}:{start_line}`.
- **Orphan cleanup (needed because boundaries shift).** Switching strategy, or editing a file so a
  function moves/disappears, leaves rows whose `chunk_id` is no longer produced. `_cleanup_deleted`
  today only prunes deleted *files*. Add per-file reconciliation: after computing a file's new
  `chunk_id` set, delete rows for that `file_path` whose `chunk_id` isn't in it (from both
  `chunk_hashes` and `code_embeddings`). This also fixes a latent pre-existing bug (deleting a
  function never dropped its chunk).
- **Config:** add `chunk_strategy = "syntax" | "lines"` (default `"syntax"`), validated in
  `config.py` (`_ENUMS`). `"lines"` forces the old behavior (an escape hatch).

### Files
- `src/codeintel/indexer.py` — new `_chunk_python_ast(lines, source)` returning `(start_line,
  end_line)` spans; `_collect_new_chunks` chooses AST vs window per file + calls the new
  per-file orphan reconciliation. Keep `_embed_and_write` unchanged.
- `src/codeintel/config.py` — `chunk_strategy` default + enum validation.
- `src/codeintel/semantic_db.py` — optional small helper for per-file chunk-id deletion (or inline
  in the indexer).
- `docs/semantic.md` — document the strategy + the new config key.

### Acceptance criteria
- A search hit against a Python file returns a snippet whose start line is a real `def`/`class` line,
  and whose text is a complete definition (not a mid-function window).
- A syntactically-invalid `.py` file still indexes (via fallback) and never raises.
- A non-Python file is unchanged (line-window).
- Re-indexing a file after removing a function drops that function's chunk (no orphans).
- All existing `tests/test_semantic_provider.py` / `test_e2e.py` stay green.

### Tests (`tests/test_chunking.py`)
- Chunk a fixture module with a top-level function, a class with two methods, a module docstring +
  imports → assert def-aligned spans, non-overlapping, full coverage.
- Decorated function → span includes the decorator.
- A >`max_chunk_lines` function → split into multiple embeddable chunks.
- Syntax-error file → falls back to windowing (chunk count > 0, no raise).
- `chunk_strategy="lines"` → identical to today's output.
- Orphan cleanup: seed a chunk, re-index with that def removed → row gone.

### Risk / rollback
Low. Gated by `chunk_strategy` (set `"lines"` to revert behavior at runtime). Schema-compatible; a
mixed index (some line, some syntax chunks) is valid — the orphan cleanup converges it on re-index.

---

## Phase 2 — Hybrid reranking  (target: 0.7.0)

### Why
`Searcher.search` orders purely by cosine distance. Cosine alone under-ranks exact lexical/symbol
matches (e.g. searching `parse_config` when a differently-worded but semantically-near chunk out-scores
the literal match). A cheap fusion rerank fixes ordering with no model dependency.

### Design
- Retrieve a **candidate set** (top `rerank_candidates`, default 30) by the existing cosine KNN
  instead of top-k directly.
- For each candidate, **re-read its chunk text** from the file (`file_path` + `chunk_start`, bounded
  span) — no schema change, only ~30 reads. Compute a **lexical score** (BM25-lite or token-overlap
  of query terms vs chunk text; give an exact-symbol/def-name match an extra boost).
- **Fuse** the semantic rank and lexical rank with Reciprocal Rank Fusion
  (`score = 1/(k+rank_sem) + 1/(k+rank_lex)`, k≈60) plus the structural boost; re-sort; return top-k.
  Keep the `cosine_floor` gate on the *semantic* candidate set so quality can't regress below today.
- **Config:** `rerank = "on" | "off"` (default `"on"`), `rerank_candidates` (default 30), both
  validated in `config.py`. `"off"` = today's pure-cosine path.

### Files
- `src/codeintel/searcher.py` — candidate retrieval + `_lexical_score` + `_fuse`; `search` returns
  the reranked top-k. `config.py` — two new keys.

### Acceptance / tests (`tests/test_rerank.py`)
- A query that is an exact symbol name ranks the literal-match chunk above a purely-semantic-but-
  lexically-distant one; with `rerank="off"` the old order returns.
- Latency stays bounded (rerank over ≤`rerank_candidates`, file reads bounded); never raises (a
  missing file → that candidate scored 0, not a crash).
- Existing semantic tests green.

### Risk / rollback
Low. Gated by `rerank`; `"off"` restores exact current behavior.

---

## Explicitly out of scope: an in-house graph

**Decision: not now.** A lightweight in-house call/import graph would **duplicate `codebase-memory-mcp`**
— a working backend codeintel already wraps for the graph engine. Building a general multi-language
graph (parsing, storage, a query layer) is weeks of work to replace something functional; it's the
lowest ROI of the candidate improvements.

**Revisit only if** dropping the external backend becomes a deliberate strategic goal (e.g. to remove
the last third-party dependency, or because the backend's contract keeps drifting). Even then, scope
it to a **narrow proof** first: a Python-only, `ast`-based call graph (who-calls-whom within one repo)
behind the existing `GraphProvider` interface, measured against `codebase-memory-mcp` on a real repo
before committing to more languages. Do not start here.

---

## Later (after P1/P2, not specced here)
- **tree-sitter chunking** for TS/JS/Go/Rust/… behind the Phase-1 chunker interface (adds
  `tree-sitter` + grammars — a real but bounded dep; do it once `ast` proves the value on Python).
- **Cross-encoder reranker** as an optional, heavier alternative to the fusion rerank.

## How to execute
- **New session:** open one with — *"Implement Phase 1 of docs/roadmap-semantic.md (syntax-aware
  chunking). Follow the repo's contracts: never-raise, tests with each change, adversarial review
  before publishing, then tag v0.6.0."* The spec above is self-contained.
- **Or via pathly:** one feature per phase (P1 → 0.6.0, P2 → 0.7.0); paste each phase's Design +
  Acceptance as the feature spec.
