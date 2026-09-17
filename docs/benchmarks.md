# Benchmarks

> **Scope: the semantic engine only.** For **call-edge accuracy** — precision and recall of `callers`/`impact` against labelled ground truth, per question, across engines — see [../bench/README.md](../bench/README.md), which is a different measurement with a different method.
>
> **Re-measured 2026-09-17 at 0.23.4.** The previous figures were taken at 0.10.0 and this document
> had marked its own latency row stale, with a prediction attached: `_verify` reads each candidate
> from disk inside the measured window, so the query path should have got slower. It did not — see
> [What changed against the 0.10.0 numbers](#what-changed-against-the-0100-numbers). The read count
> that prediction was about is now **enforced by a test** rather than re-argued: see
> [The enforced budget](#the-enforced-budget).

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
| OS | macOS 26.6.2 (arm64) |
| Embedding model | `BAAI/bge-small-en-v1.5` (384-dim), via `fastembed` on CPU |
| codeintel | 0.23.4, run from the checkout (`PYTHONPATH=src python -m codeintel`), not the installed snapshot |

## Corpus

A full production TypeScript/React monorepo (`bright-sky`): **1,334 files chunked**, syntax-aware
(tree-sitter), into **29,903 chunks** (~22.4 chunks/file).

The tree is not a git checkout at the measured root; its code lives in a `brightsky-ai` subdirectory
which is clean at `3109977`. That is the most this corpus can be pinned to, and it is stated rather
than dressed up as a revision of the thing actually measured. At 0.10.0 the same repository chunked
to 25,313 chunks from 1,449 counted files, so **the corpus has grown ~18% and is not identical** —
which matters for the wall-clock rows and not for the per-chunk ones.

## Cold index (one-time)

The embedding model is downloaded once (~50 MB) and excluded from the timing below; it was already
cached, and loading it takes 0.5 s.

| Metric | 0.23.4 (2026-09-17) | 0.10.0 (prior) |
|---|---|---|
| Chunks indexed | **29,903** | 25,313 |
| Wall time | **607.1 s** (~10.1 min) | 499.7 s |
| Throughput | **~49.3 chunks/sec** | ~51 chunks/sec |
| CPU parallelism | **6.68×** (4,054 s user / 607 s real) | 6.7× |
| Peak memory (RSS) | **1.72 GB** | ~1.7 GB |
| On-disk index (`semantic.db`) | **68.1 MB** (~2.39 KB/chunk) | 60 MB (~2.4 KB/chunk) |

Scan and chunk is 1 s of that; the other 9 m 49 s is embedding. Throughput and bytes-per-chunk are
flat against 0.10.0 — the wall-clock difference is the larger corpus, not a slower engine.

Cold indexing is a **one-time** cost. Steady state is **incremental**: the reindexer re-embeds only
the files a `git` diff touched, so day-to-day it's seconds, not minutes — a background reindex
triggered by `code.query`, not something a user waits on.

## Warm query latency

End-to-end `code.query op=search` (embed the query on CPU → `sqlite-vec` KNN over 29,903 vectors →
verify each candidate against current source → hybrid rerank → render), over 11 realistic queries,
**four passes, pooled n = 40** after discarding each pass's first query:

| Metric | 0.23.4 (2026-09-17) | 0.10.0 (prior) |
|---|---|---|
| p50 | **233 ms** | 235 ms |
| p95 | **242 ms** | 251 ms |
| min / max | 229 / 254 ms | 218 / 255 ms |
| standard deviation | 4.7 ms | not recorded |
| First query in a fresh process | **262–298 ms** | 301 ms |
| Relevant hit rate | 11 / 11 (10 rows each) | 11 / 11 |

Two things are worth separating, because the first run of this measurement conflated them and
reported `p50 = 272 ms`:

* **First query in a fresh process is 262–298 ms**, and roughly 40 ms of that is page cache, not
  model warm — the process has to fault in parts of a 68 MB database and the source spans of 60
  candidates. That is a real user-visible cost on the first query after a boot, and it is reported
  as its own row rather than averaged into the steady state.
* **Steady state is 233 ms at p50 with a 4.7 ms standard deviation.** Once warm, this engine is
  extremely consistent.

The latency is dominated by **embedding the query string** on CPU (~230 ms); the vec0 KNN over 30 k
vectors is sub-millisecond, and verifying 60 candidates against disk is a few milliseconds more. A
GPU or a smaller model would cut the bulk of it. For an agent making a handful of `code.query` calls
while reasoning, sub-¼-second is comfortably interactive.

## What changed against the 0.10.0 numbers

Four releases after the original measurement touched the semantic engine, and this document
predicted a latency regression from one of them:

| Release | Change | Predicted effect | Measured |
|---|---|---|---|
| 0.17.0 | live progress for `codeintel index` | reporting inside the timed pass | no visible cost; throughput flat |
| 0.18.0 | hits verified against current source | **query latency** — `_verify` reads each candidate from disk | **none at p50** (233 vs 235 ms) |
| 0.19.0 | an index that cannot be verified is reported `unconfirmed` | query path | none measurable |
| 0.20.0 | per-edge confidence no longer discarded | envelope, not the semantic hot path | none |

**The predicted regression did not materialise, and the reason is worth stating because it is the
thing that keeps being true here: the query embed dominates everything else by two orders of
magnitude.** Reading 60 short spans off a warm page cache costs single-digit milliseconds against a
~230 ms CPU embed. The prediction was sound about direction and wrong about whether it would be
observable — which is exactly why this document said "re-measure" rather than "adjust".

That result is contingent, not structural. It holds while the read set stays bounded by the
candidate limit. If a change made the search read every row, or read each candidate twice, the same
argument would stop protecting it — so that bound is now a test rather than a paragraph.

## The enforced budget

`tests/test_query_budget.py` asserts the work a single `search` is allowed to do. It counts rather
than times, because **a wall-clock assertion on a shared CI runner is a summary whose referent is
the runner** — true of that machine on that morning, and read as a claim about the code. The
quantities below are properties of the algorithm, identical on a laptop and on a cold shared
runner, so they can be asserted instead of eyeballed:

| Budget | Why it is the one to hold |
|---|---|
| **≤ 1 file read per candidate** | verification, rerank and the snippet share one read. A change that re-reads for the snippet doubles the disk cost of every query and changes no result. |
| **reads flat in corpus size** | the extrapolation below is only valid while the read set is bounded by the candidate limit rather than by the number of chunks indexed. |
| **exactly 1 embed per search** | the embed IS the latency. One per candidate would be a 60× regression that no functional test would notice. |
| **exactly 1 vector query per search** | an N+1 over candidates changes nothing a user can see, and only makes every query slower. |
| **rerank=off reads only `k`** | otherwise switching the widening off still pays for it. |

Measured values today, at `k=5`: 10 reads for 10 candidates, 20 for 20, 20 for 20 on a corpus 3×
larger, 5 with rerank off, and 1 embed / 1 KNN throughout. The budget is a ceiling, so a smaller
number is an improvement and only a larger one fails.

## Extrapolation to the configured ceiling

codeintel caps a single index at **100,000 chunks** (`max_total_chunks`, tunable). Linear from the
measured constants, the ceiling is roughly:

| At 100 k chunks (≈3.3× this corpus) | Estimate |
|---|---|
| Cold index (this machine) | ~34 min |
| `semantic.db` size | ~234 MB |
| Query latency | unchanged (~233 ms — KNN over 100 k vectors is still sub-ms; latency is the query embedding, not the search) |

Query latency is flat in corpus size (the cost is embedding the *query*, not scanning the index), so
the engine stays interactive as the repo grows; index time and disk scale linearly with chunk count.
The flatness is not merely asserted here — it is the second row of
[the enforced budget](#the-enforced-budget).

## Reproduce it

Into a throwaway `CODEINTEL_HOME` so your real cache is untouched:

```bash
export CODEINTEL_HOME=/tmp/ci-bench && rm -rf "$CODEINTEL_HOME" && mkdir -p "$CODEINTEL_HOME"
export PYTHONPATH="$PWD/src"          # measure the checkout, not the installed snapshot
/usr/bin/time -l .venv/bin/python -m codeintel index /path/to/large-repo > /tmp/index.log 2>&1
grep -Ei "Indexed|real|maximum resident" /tmp/index.log
ls -l "$CODEINTEL_HOME"/semantic.db   # on-disk index size
```

Do **not** pipe `/usr/bin/time` into `head`. The first attempt at this re-measurement did, which
closed the pipe and SIGPIPE-killed the indexer at 1% — and the latency step then happily reported
figures for a 600-chunk index while the header said 29,903. The run looked entirely normal.

Query latency: load the provider once and time warm searches, discarding the first (model load and
page cache), and run several passes rather than one — a single pass on a cold cache reads ~8% high.

```bash
CODEINTEL_HOME=/tmp/ci-bench PYTHONPATH=src .venv/bin/python -c '
import os, statistics, time
from codeintel.providers.semantic import SemanticProvider
p, root = SemanticProvider(), os.path.expanduser("/path/to/large-repo")
ts = []
for i in range(11):
    t = time.monotonic(); p.build_result("search", f"query {i}", [], 0, root)
    if i: ts.append((time.monotonic() - t) * 1000)
print(f"p50 {statistics.median(ts):.0f} ms")'
```
