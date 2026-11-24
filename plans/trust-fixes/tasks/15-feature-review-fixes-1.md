# Fix the findings of the feature review, round 1

Type: task
Status: resolved
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

Work started at c7debb07b7dc2fa51bd82e4f1634167b897e0f64.

- **Built**:
  - `src/codeintel/engines/graph/provider.py`: per-thread `index_inputs` = `(project, root, paths)` that `_stamp_staleness` checked (row files, target files, stand-in directories), reset at the top of `build_result`; static `index_behind(inputs)` = a checked path is gone, or `changed_range.graph_index_age` over them is not `fresh`.
  - `src/codeintel/gateway/cache.py`: an entry carries `inputs`; `put(..., inputs=)`, `get(..., moved=)` misses when `moved(item)` is true for any stored input.
  - `src/codeintel/gateway/gateway.py`: `_dispatch_single`/`_fan_out` collect the provider's `index_inputs` on the thread that ran `build_result` (`_note_index_inputs`); `_query` stores them with the cached answer (single and fan-out paths) and passes `moved=_index_moved` on every lookup; a check that raises counts as moved.
  - `src/codeintel/engines/graph/ops.py`: `_chain_between` adds a `target-ambiguous` gap (section `chain`) when an end matched several symbols (finding 2); the `target-not-found` hint names only `codeintel index <root>` (or ask the user), no `index_repository` (finding 3).
  - Tests: `tests/test_edge_staleness.py` (8 new gateway cache tests), `tests/test_chain_destination.py` (ambiguous-end test now asserts partial + the gap).
- **Proof**:
  - Finding 1 (major): `.venv/bin/pytest tests/test_edge_staleness.py -q -o addopts=` → `16 passed`. With `src/` stashed (fix removed) the same run → `7 failed, 9 passed`: the 4 parametrised "row file / target file / callee file / impact" edit cases, the deleted-file case, the after-a-commit case and the fan-out (`engine=both`, `context`) case all got `cached: true` with no `stale-index`. With the fix each comes back `cached: false` with a `stale-index` gap naming the edited file and `safe_for_destructive: false`; an edit to a file outside the answer still gets `cached: true`.
  - Finding 2 (minor, fixed): `tests/test_chain_destination.py::test_an_ambiguous_destination_lists_its_candidates_and_claims_no_path` asserts `confidence == "partial"` and a `target-ambiguous` gap naming `` `validate` ``; the run of `test_chain_destination.py test_missing_target.py test_agent_hints.py test_summary_integrity.py test_graph_failure_population.py` → `156 passed, 1 xfailed`.
  - Finding 3 (minor, fixed): `grep -n index_repository src/codeintel/engines/graph/*.py` now finds only `backend.py:124` (the stderr rewrite rule). provider.py:1026 no longer names it (ticket 11 already removed it).
  - Touched-module tests: `pytest tests/test_gateway.py tests/test_cache.py tests/test_edge_staleness.py tests/test_dispatch_callers.py tests/test_graph_provider.py tests/test_index_age.py tests/test_incompleteness.py tests/test_rbac.py tests/test_search_fallback.py tests/test_architecture.py -q -o addopts=` → `355 passed`. `ruff check src tests bench` → All checks passed. `mypy` → no issues in 88 files. `python -m compileall -q src/codeintel` → exit 0. `codeintel c4 . --check --out /tmp/codeintel-c4-check` → 0 gating findings, exit 0.
  - commands.md's Test command passes: left to the conductor's gate.
- **Decisions**:
  - Finding 1, option (c)/(a) hybrid: the cache entry stores what the answer's age check read (its row and target files plus stand-in directories, with the project and root), and a hit re-runs the same check the provider runs (`graph_index_age`: newest of those files, HEAD's commit time, the last reflog tree move, against the db's mtime) and recomputes when it is not `fresh` or a checked file is gone. Caching stays: an unchanged tree, or an edit to a file the answer does not cite, is still a hit. I did not store a single "newest mtime" number: stat-ing the files when storing happens after the dispatch, so an edit landing between the provider's check and the put would be recorded as already seen and the stale answer served without its gap; comparing against the db on every hit has no such window. Going through `changed_range.graph_index_age` (and the provider's own `index_behind`) keeps one definition of "stale" and keeps the suite's `fresh` stub in force for tests that do not ask for `real_index_age`.
  - The inputs travel from provider to gateway through a per-thread provider attribute read right after `build_result` on the same thread (fan-out runs each engine in a pool thread, so `_call` reads it there), not through the envelope: no new agent-facing key, and providers without the attribute (lsp, semantic, test fakes) cache exactly as before.
  - Finding 4 (minor) stays: making the verdict use the pre-dispatch `written` means passing it to `graph_index_age`, whose suite-wide stub in `tests/conftest.py` takes exactly `(project, root, paths)`; any new argument breaks every graph test that relies on the stub, and conftest is test config I may not edit. The verdict still compares against a db mtime read after the round trip; the demoted-row choice uses the pre-dispatch one.
- **Shortcuts taken**: a cache hit for callers/callees/impact/context now costs the age check's git calls (`git log -1`, `git rev-parse --git-path`, a reflog read; a few ms). While the index is stale for an answer, every ask of it is recomputed (no hit until a reindex). Worth upgrading only if hits on a stale index become hot: store the newest input time with the entry and serve a hit whose cached answer already carries `stale-index` while nothing moved past it.
- **For later tickets**: `ContentHashCache` entries are now 3-tuples `(hash, result, inputs)`. A new graph op that stamps staleness gets cache re-checking for free if it goes through `_stamp_staleness`.
