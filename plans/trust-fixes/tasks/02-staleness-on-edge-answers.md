# Stamp `stale-index` on callers, callees and impact answers

Type: task
Status: open
Blocked by: 01
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/engines/graph/provider.py, tests/test_edge_staleness.py

Fix T2's edge-answer half: finding `callers-callees-stale-complete`. On a stale tree, `callers helper` listed a deleted function as verified, `callees compute` was complete and safe for a function that no longer exists, and rows still cited a file that had been renamed.

In `GraphProvider.build_result`, after `_dispatch` returns a body for `callers`, `callees`, `impact` or `context`, and before `_settle_evidence` is called:

1. Collect the distinct files of every row in `_pending_rows` (the `file` field `_structured_row` writes), plus the target's own file from the rendered groups.
2. Call `changed_range.graph_index_age(project, root, files)` from ticket 01, which now includes the HEAD commit time. Call it through the module attribute, not a `from … import graph_index_age` binding: ticket 01's autouse stub in `tests/conftest.py` patches `changed_range.graph_index_age`, and this ticket may not edit `conftest.py`. A by-name import here would escape the stub and turn every existing callers/callees/impact test `index-age-unknown`.
3. If the result is `stale`:
   - add a `stale-index` gap whose detail names what changed after the index, and say "re-run `codeintel index <root>`"
   - set each row whose file is missing, or newer than the db, to `evidence: unstated`, with a `why` saying the file changed after the index
4. If the result is `unknown`, add `index-age-unknown`.
5. A fresh index changes nothing in the answer: no gap and no wording change.

**Keep the summary census unchanged.** `tests/test_summary_integrity.py::test_every_summary_site_has_a_verifier` fails on any new string in `src/` that is a markdown heading with `→` or a `(N …)` count, a tally phrase (a count followed by a unit noun such as `file`, `row`, `caller`, `symbol`), or a dict literal with a claim key (`confidence`, `runnable`, `ready`, …) in a function not already in its `_VERIFIED_BY`. Word the `stale-index` detail with file names, not a count ("`core.py` changed after the index", not "3 files changed"), and add no new claim-key dict. The file is not on this ticket's Touches; the check block runs it to prove the census did not move.

This costs one `stat` per distinct file. Put it in a small `GraphProvider` method, for example `_stamp_staleness(project, root, age)`, so `build_result` stays readable. Keep it in `provider.py`: the blind-spot and caller-count tickets own `answer.py`. Read the db mtime before the backend round trip, as `changed.py` does: take the stamp at the start of `build_result`'s dispatch.

## Not in this ticket

- `changed` (both modes) is ticket 01.
- The gateway cache key is ticket 03.
- `chain` hops are ticket 04. `chain` gets no row files here.

## Done when

- New `tests/test_edge_staleness.py` uses the fake `_run` graph from `tests/test_dispatch_callers.py`, a real fake db file and `os.utime`. These fail on main and pass here:
  - callers whose row file was edited after the db: `stale-index` gap, that row `unstated`, `safe_for_destructive` false
  - callees of a target whose file was deleted: `stale-index`, not complete
  - impact: the same gap once, not twice
  - a fresh db: no gap, same body as before
  
  `.venv/bin/pytest tests/test_edge_staleness.py -q -o addopts=` passes.
- The new tests request ticket 01's `real_index_age` fixture. Every other test keeps the conftest default (`fresh`), so the bodies and gaps of existing tests do not change.
- All existing tests still pass, unchanged, including `tests/test_dispatch_callers.py`, `tests/test_summary_integrity.py`, `tests/test_edge_confidence.py`, `tests/test_graph_failure_population.py` and `tests/test_null_reason_and_ripple.py` (none of them is on Touches).

```check
$ .venv/bin/pytest tests/test_edge_staleness.py tests/test_dispatch_callers.py tests/test_summary_integrity.py tests/test_edge_confidence.py tests/test_graph_failure_population.py tests/test_null_reason_and_ripple.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "An agent working right after an edit"
- redblue-fixplan.md § T2, "Callers, callees and impact"
- `plans/dogfood-fixes/tasks/08-*` describes the same change. This ticket supersedes it.

## Answer
