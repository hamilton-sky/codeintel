# Benchmarks

Real, reproducible numbers for the **semantic** engine at scale — the one engine that does heavy
local work (chunk → embed → index → search). The graph and LSP engines delegate to external
backends and are not measured here.

> Scope, honestly: this is **one repo on one machine**, CPU embeddings (no GPU). Treat the
> throughput and per-chunk figures as the scaling constants; treat the wall-clock as machine-specific.
> Reproduce it yourself with the commands at the bottom.

## Test machine

| | |
|---|---|
| CPU | Apple M5 Pro — 15 cores (5 performance) |
| RAM | 24 GB |
| OS | macOS 26.5 (arm64) |
| Embedding model | `BAAI/bge-small-en-v1.5` (384-dim), via `fastembed` on CPU |
| codeintel | 0.10.0 (semantic engine unchanged since; re-measure if that changes) |

## Corpus

A full production TypeScript/React monorepo (`bright-sky`): **1,449 code files** (`.ts/.tsx/.js/.py/.go`,
excluding `node_modules`), syntax-aware chunked (tree-sitter) into **25,313 chunks** (~17.5 chunks/file).

## Cold index (one-time)

The embedding model is downloaded once (~50 MB) and excluded from the timing below.

| Metric | Value |
|---|---|
| Chunks indexed | **25,313** |
| Wall time | **499.7 s** (~8.3 min) |
| Throughput | **~51 chunks/sec** |
| CPU parallelism | **6.7×** (3,339 s user / 500 s real — embedding fans out across cores) |
| Peak memory (RSS) | **~1.7 GB** |
| On-disk index (`semantic.db`) | **60 MB** (~2.4 KB/chunk: a 384-d float32 vector + metadata + chunk text) |

Cold indexing is a **one-time** cost. Steady state is **incremental**: the reindexer re-embeds only
the files a `git` diff touched, so day-to-day it's seconds, not minutes — a background reindex
triggered by `code.query`, not something a user waits on.

## Warm query latency

End-to-end `code.query op=search` (embed the query on CPU → `sqlite-vec` KNN over 25,313 vectors →
hybrid rerank → render), measured warm over 11 realistic queries:

| Metric | Value |
|---|---|
| p50 | **235 ms** |
| p95 | **251 ms** |
| mean / min / max | 235 / 218 / 255 ms |
| First query (incl. model warm) | 301 ms |
| Relevant hit rate | 11 / 11 |

The latency is dominated by **embedding the query string** on CPU (~230 ms); the vec0 KNN over 25 k
vectors is sub-millisecond. A GPU or a smaller model would cut the bulk of it. For an agent making a
handful of `code.query` calls while reasoning, sub-¼-second is comfortably interactive.

## Extrapolation to the configured ceiling

codeintel caps a single index at **100,000 chunks** (`max_total_chunks`, tunable). Linear from the
measured constants, the ceiling is roughly:

| At 100 k chunks (≈4× this corpus) | Estimate |
|---|---|
| Cold index (this machine) | ~33 min |
| `semantic.db` size | ~240 MB |
| Query latency | unchanged (~235 ms — KNN over 100 k vectors is still sub-ms; latency is the query embedding, not the search) |

Query latency is flat in corpus size (the cost is embedding the *query*, not scanning the index), so
the engine stays interactive as the repo grows; index time and disk scale linearly with chunk count.

## Reproduce it

Into a throwaway `HOME` so your real cache is untouched:

```bash
export HOME=/tmp/ci-bench && mkdir -p "$HOME/.codeintel"
codeintel index /path/to/small-repo >/dev/null   # warm-up: downloads the model, not timed
/usr/bin/time -l codeintel index /path/to/large-repo   # wall time + max RSS; prints "Indexed N chunks"
ls -la "$HOME/.codeintel/"*.db                          # on-disk index size
```

Query latency: load the provider once and time warm searches (see `codeintel query --op search
--target "..."`, or drive `SemanticProvider.build_result("search", q, [], 0, root)` in a loop and
take the median of runs after the first).
