# Fix the findings of the feature review, round 1

Type: task
Status: open
Blocked by: —
Test first: no
Level: 2
Check: none the feature review that follows checks these fixes

The review of the whole feature (from commit 23e7ab74dde64b646c0a861ff8bb6c0d210a5234) failed. Fix each finding below that is a blocker or a major, in the files it names. A minor is optional. If a finding is wrong, say why in the Answer instead.

## Not in this ticket

- Anything the findings do not name.

## Done when

- Every blocker and major finding below is fixed, or the Answer says why it stays.
- commands.md's Test command passes.

## Findings

> FEATURE
> - tickets met: 02, 04, 05
> 1. major src/codeintel/gateway/gateway.py (ticket 02 and 05, alongside reviewed ticket 03): the gateway cache can serve an answer that hides a stale index. The cache key is made only from the reindex generation and the graph and semantic db stamps. Ticket 02's `stale-index` stamp comes from source-file mtimes and ticket 05's blind-spot rules from source text, but `_states_live_file_contents` only keeps qualifier-scan answers out of the cache. Failure on `serve`: ask `callers X` on a fresh tree (cached as complete/safe), edit a row's file without reindexing, ask again: cache hit (`cached: true`), no `stale-index` gap, `safe_for_destructive: true`. With `CODEINTEL_REINDEX=off` not even the `reindexing` flag shows. Breaks the spec goal "Every graph answer says when the index is stale". No test covers a cache hit after an edit. Options: (a) on a hit for callers/callees/impact/context, re-stat the cached answer's row and target files and HEAD's commit time against the db mtime and recompute on any newer one; (b) exclude those ops from the cache; (c) store the newest mtime among the answer's files in the entry and check it on a hit. Add a gateway test: query, `os.utime` a row file past the db, query again, assert `cached` is false or a `stale-index` gap is present.
> 2. minor src/codeintel/engines/graph/ops.py `_chain_between` (ticket 04): when B is ambiguous, `chain A->B` returns the candidate list with `confidence: complete` and no gap. Add a gap (e.g. `target-ambiguous`) naming the end that matched several symbols, and a test asserting partial.
> 3. minor src/codeintel/engines/graph/ops.py:518 (ticket 04): the new `target-not-found` text tells the agent to call `index_repository` with `incremental=true` over MCP; the spec says no hint names `index_repository`. Name only `codeintel index <root>` (or "ask the user to run it"). Pre-existing at provider.py:1026 too.
> 4. minor src/codeintel/engines/graph/provider.py `_stamp_staleness` / `build_result` (ticket 02): the db mtime is read before `_dispatch` only to pick demoted rows; the stale/fresh verdict from `changed_range.graph_index_age` re-reads it after the round trip. Make the verdict use the stamp taken before dispatch (e.g. let `graph_index_age` accept a pre-read `written`).
>
> Done when re-run at HEAD: 02: 316 passed, 1 xfailed; 04: 243 passed, 1 xfailed; 05: 441 passed, 1 xfailed (live test ran). `grep -n "falls back to" docs/graph.md` finds only line 376 (qualifier scan).
>
> codeintel: changed 23e7ab74dde64b646c0a861ff8bb6c0d210a5234: helped (one wrong: it listed `_graph_slug` as removed and still mentioned, but reset.py re-imports it as `graph_slug as _graph_slug`)
>
> REVIEW: FAIL

## Answer
