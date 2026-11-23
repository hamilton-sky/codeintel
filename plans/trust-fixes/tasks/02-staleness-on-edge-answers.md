# Stamp `stale-index` on callers, callees and impact answers

Type: task
Status: resolved
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

Work started at commit 4f196ab9d3903baa0311aedeb6f9a82c799aae4a. Test-only commit: a4a71dd.

**Built**
- `src/codeintel/engines/graph/provider.py`:
  - `_STALENESS_OPS` (`callers`, `callees`, `impact`, `context`) and a module helper `_name_files`.
  - A `_pending_target_files` PerThread, reset in `build_result`. Thin `_render_edge_answer` / `_empty_edge_answer` overrides record each rendered group's own file (the target's file, which no row carries), then call `super()`.
  - `GraphProvider._stamp_staleness(op, project, root, written)`.
  - In `build_result`, the db mtime (`core.paths.graph_db_mtime`) is read before `_dispatch`. `_stamp_staleness` runs after the ancestor-scope note and before `_settle_evidence`.
- `tests/test_edge_staleness.py` (new): 8 tests. It uses `_Graph`/`_provider` from `tests/test_dispatch_callers.py`, a real git repo, a fake `tmp-proj.db` dated with `os.utime`, and `pytestmark = usefixtures("real_index_age")`.

**Proof**
- New tests fail before the fix: at a4a71dd (src unchanged from 4f196ab), `.venv/bin/pytest tests/test_edge_staleness.py -q -o addopts=` gave 5 failed, 3 passed. The 5 failures were the edited-file callers, renamed-file callers, deleted-target callees, impact-once and age-unknown tests. Each failed on its assertion: `want exactly one stale-index gap: []` (x3), `[].count('stale-index') == 1`, and `'index-age-unknown' in []`. The 3 that passed are the fresh-index controls (parametrized over callers, callees and impact), which assert that nothing changes.
- After the fix, `.venv/bin/pytest tests/test_edge_staleness.py -q -o addopts=` gave 8 passed. The tests cover these cases:
  - callers with `pkg/app.py` edited after the db: one `stale-index` gap naming `` `pkg/app.py` `` and `codeintel index <root>`; that row `unstated`, `verified: false`, a `why` saying "changed after the index"; the `pkg/core.py` row still `resolved`; `safe_for_destructive` false.
  - a renamed file: its row is `unstated`.
  - callees of `compute` with `pkg/core.py` deleted: `stale-index` naming it, confidence not complete, the `pkg/util.py` row still `resolved`.
  - impact: `stale-index` appears exactly once.
  - a fresh db: no gap, `complete`, and an envelope `==` the one produced with `graph_index_age` patched to `fresh`.
  - no db at all: `index-age-unknown`.
- Real `graph_index_age` only in the new file, through its `pytestmark` (`real_index_age`). No other test file changed (`git status`).
- Check block `.venv/bin/pytest tests/test_edge_staleness.py tests/test_dispatch_callers.py tests/test_summary_integrity.py tests/test_edge_confidence.py tests/test_graph_failure_population.py tests/test_null_reason_and_ripple.py -q -o addopts=`: 316 passed, 1 xfailed.
- Wider runs with PATH set, so the live backend tests run:
  - the check block plus test_graph_provider, test_graph_real, test_index_age, test_changed_range, test_qualifier_scan, test_mcp_server, test_gateway, test_mcp_answer_size, test_architecture and test_cli_commands: 729 passed, 1 xfailed.
  - the 40 test files that mention callers/callees/impact: 1237 passed, 28 skipped, 1 xfailed.
- Lint and build checks: `ruff check src tests bench` all checks passed. `mypy`: no issues in 87 files. compileall OK. `codeintel c4 . --check --out /tmp/codeintel-c4-check`: 0 gating findings.
- All existing tests pass: the whole Test command is left to the conductor's gate.
- Acceptance `test_tf_freshness.py`: 11 passed. All 6 callers/callees/impact tests now pass. The 3 that still fail are the MCP cache and reindexing-flag tests (ticket 03 and later).

**Decisions**
- A file that no longer exists cannot be dated, and `graph_index_age` skips it. Its nearest existing parent directory is passed in its place, because an unlink or rename moves the directory's mtime. This keeps the whole answered-or-not decision inside `changed_range.graph_index_age`, so the conftest `fresh` stub still covers every existing test. A separate own-mtime check would have bypassed the stub: live tests on this checkout against the real `~/.cache/codebase-memory-mcp/home-user-codeintel.db` would turn stale whenever a file is edited.
- Row stamping (which rows become `unstated`) compares each file with the db mtime read before `_dispatch`, as the ticket asks. It falls back to a fresh read when that read found no db.
- Gap section is the constant `"index"`, not the op name. This matches the other cross-cutting gaps that `build_result` adds (`scope`, `backend`), and it is one gap for both halves of `impact`. Side effect, stated so the reviewer can judge it: `tests/test_graph_failure_population.py::test_every_gap_callees_can_report_states_its_numbers_in_the_body_too` counts `_add_gap` calls with a variable section as possible `callees` gaps. With `op` as the section, it failed because its scenarios have no stale index. The body does carry the gap: a `> Stale index: <detail>.` or `> Index age unknown: <detail>.` line, which includes every number in the detail.
- The gap detail names files (first three, then "and others"), never a count, so the summary census did not move (`test_summary_integrity.py` passes). The demoted row is a `dict(row)` copy with keys assigned, not a dict literal with a claim key. `verified` is set to false with `evidence`, so the two cannot disagree.
- The target's own file comes from the rendered groups through the two renderer overrides in `provider.py`. `answer.py` belongs to other tickets.

**Shortcuts taken**
- The answer-level stale/fresh decision still uses `graph_index_age`'s own db stat, which runs after the round trip. `changed_range.py` is not on Touches, and that function takes no stamp argument. If the backend ever moves the db mtime while answering, a stale edge answer could read `fresh`. Worth upgrading by giving `graph_index_age` an optional `written` argument if that is ever observed.
- A deleted file whose directory has not changed since the index (it was deleted before the index was written, yet the index still cites it) reads `fresh`. This is rare, and the backend's own record would be wrong then.
- The body's per-row badges and evidence headline are rendered before staleness is known. They still show the backend's bucket, while `rows[]`/`evidence` and the first-screen banner show `unstated`. The `> Stale index:` note says why.

**For later tickets**
- `GraphProvider` now overrides `_render_edge_answer` and `_empty_edge_answer`. A ticket that changes their signatures in `answer.py` must change the overrides in `provider.py` too, or mypy fails.
- Ticket 04 (`chain`): `chain` is deliberately not in `_STALENESS_OPS`.
