# Persist semantic model failures, report them in doctor and status, and fall back to graph name search

Type: task
Status: open
Blocked by: 03
Test first: yes
Level: 3
Builder: backend
Touches: src/codeintel/engines/semantic/provider.py, src/codeintel/setup/doctor.py, src/codeintel/gateway/gateway.py, tests/test_background_index_failure.py, tests/test_doctor.py, tests/test_semantic_provider.py, tests/test_search_fallback.py

This ticket fixes the semantic half of T8: `search-no-graph-fallback-contradictory` (the fallback half), `search-flaps-first-time-indexing` and `semantic-blocked-reported-runnable`. Today the failure memo `_BG_INDEX_FAILED` in `src/codeintel/engines/semantic/provider.py` lives in one process, so every new process retries the blocked download for about 40 s, and after the cooldown the state flips back to "first-time indexing ~0s ago".

It is level 3 because the record decides when a model download (about 40 s and ~50 MB) is retried: a retry limit. A bad or hostile record must never block indexing for good, nor make search retry on every call. A record that does not parse, has a future `at`, or is older than 10 minutes is ignored (and not trusted for its `cause` text either), and the record is written and read only under `codeintel_home()`.

1. **Persist the failure.** When an index pass fails to load the model (the classification `index-failed` already uses), write `codeintel_home()/semantic_failure.json`:
   - `{cause, at, model, fastembed_cache_path, model_files_present}`
   - Write it atomically (temp file plus `os.replace`).
   - Remove the file when an index pass succeeds.
2. **Fail fast.** Before an inline or background index starts, read that record. While it is recent (under 10 minutes) and its inputs are unchanged (same model, same `FASTEMBED_CACHE_PATH`, model files still absent), `build_result("search", …)` returns `index-failed` at once with the recorded cause and remediation. Never relabel it `indexing-in-progress` / "first-time indexing".
3. **Report it.**
   - `SemanticProvider.probe` reports `runnable: False` with detail "last index could not load model: <cause>" when the record applies.
   - `_with_model_cache` uses that cause instead of "the next index downloads ~50 MB".
   - `src/codeintel/setup/doctor.py` maps that state to a failing semantic row whose remediation names `FASTEMBED_CACHE_PATH` / network.
   - `status` reads the probe, so it follows without a change to `src/codeintel/cli/status.py`.
4. **Fall back to graph name search.** In `Gateway.query`, next to the existing `overview` auto-fallback: when `op == "search"` was auto-routed and the semantic answer's reason is `index-failed`, `indexing-in-progress`, `engine-unavailable` or `no-index`, dispatch the graph engine with op `pattern` and the same target. Then:
   - set `fallback: "graph-name-search"`
   - add the gap `name-only-fallback` ("matched by name, not meaning")
   - keep the semantic reason in the hint
   
   An explicit `engine=semantic` gets no fallback.
   - **RBAC.** The fallback runs op `pattern`, which a role may not be allowed. Check `pattern` against the caller's role with `Gateway.allows(role, "pattern")`, the tiering check `Gateway._query` applies to the requested op; when the role may not run `pattern`, return the semantic answer unchanged (no fallback). A reader role limited to `search` must never receive a `pattern` answer through this path.
   - The fallback is a second dispatch inside the same query: it does not call `maybe_reindex` again and does not run the RBAC root check again (`tests/test_gateway.py::test_query_calls_maybe_reindex` expects one call).
5. **Keep what other tests read.**
   - `tests/test_index_failure_reason.py` reads the text of `src/codeintel/engines/semantic/provider.py`: keep `if indexer.index(project_root) < 0:` and `background cold index failed`.
   - `tests/test_hardening.py` counts inline index passes of `SemanticProvider.build_result("search", …)`; with no failure record (each test has its own `CODEINTEL_HOME`) the counts do not change.
   - `tests/test_summary_integrity.py` registers `SemanticProvider.probe`, `doctor._probe_engine` and `doctor.run_doctor` as claim sites. Put the new `runnable: False` inside those functions; a new function that builds a dict with `runnable` (or `confidence`) is a new census site and fails `test_every_summary_site_has_a_verifier`. That file is not on Touches; the check block runs it.

## Not in this ticket

- The reindexer's change detection and graph-first order are ticket 03.
- The `--semantic-only` reset in doctor's corrupt-DB remedy is ticket 12.

## Done when

- These tests use the existing fastembed fakes, with no network, and fail on main:
  - `tests/test_background_index_failure.py`: after a recorded failure, a second `search` from a fresh provider instance (simulating a new process: clear `_BG_INDEX_FAILED`) returns `index-failed` in under 1 s, and the hint never says "first-time indexing"
  - setting `FASTEMBED_CACHE_PATH` to a different value lets an index be tried again
  - a record older than 10 minutes, with a future `at`, or that is not valid JSON, lets an index be tried again
  - `tests/test_doctor.py`: semantic `runnable` is False, with the recorded cause, after a failure
  - new `tests/test_search_fallback.py`: auto `search` with semantic `index-failed` returns graph `pattern` results with `fallback: graph-name-search` and the gap
- `tests/test_search_fallback.py` also shows that a role allowed `search` but not `pattern` gets no fallback (`Gateway.allows(role, "pattern")`). This guards the new path; it is not a finding's regression test.
- `.venv/bin/pytest tests/test_background_index_failure.py tests/test_semantic_provider.py tests/test_search_fallback.py tests/test_index_failure_reason.py tests/test_hardening.py tests/test_gateway.py tests/test_rbac.py tests/test_summary_integrity.py -q -o addopts=` passes; the last five are not on Touches and pass unchanged. `tests/test_doctor.py` passes, except the root-only test the Test command deselects. All existing tests still pass.

```check
$ .venv/bin/pytest tests/test_background_index_failure.py tests/test_semantic_provider.py tests/test_search_fallback.py tests/test_index_failure_reason.py tests/test_hardening.py tests/test_gateway.py tests/test_rbac.py tests/test_summary_integrity.py -q -o addopts=
prints passed
$ .venv/bin/pytest tests/test_doctor.py -q -o addopts= --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
prints passed
```

## Reference

- spec.md, the story "A user on a host where the embedding model cannot download"
- redblue-fixplan.md § T8
- core/paths.py `codeintel_home` (read-only)

## Answer

Work started at commit cbc476df60570c3a3b952601e68113435ff082cb. Test-only commit: 5319ec9 (24 failed, 107 passed there, each for the missing behaviour: no record file, `indexing-in-progress`/second model load instead of `index-failed`, no `fallback` key, doctor row `runnable: True`).

**Built**
- `src/codeintel/engines/semantic/provider.py`: the persisted record `codeintel_home()/semantic_failure.json` = `{cause, at, model, fastembed_cache_path, model_files_present}`.
  - `_write_model_failure(model, cause)`: `mkstemp` in the same dir, then `os.replace`. Cause capped at 2000 chars. Never raises; an unwritable home costs only the record.
  - `_read_failure_record()`: `O_NOFOLLOW` open (a symlink is not followed), regular file only, at most 64 KiB, JSON object with the five typed fields. Anything else gives `None`.
  - `_model_failure(model)`: the cause only while `0 <= age < 600 s`, same model, same `FASTEMBED_CACHE_PATH` (raw env value, `None` when unset), `model_files_present` false then and `model_is_cached(model) is not True` now. Never raises.
  - `_clear_model_failure(model)`: removes the record after a successful pass for the same model (or an unreadable record).
  - `build_result`: before any inline or background pass (`background_reindex_off or no_index`), an applying record returns `index-failed` at once with the cause and `MODEL_FAILURE_FIX` (`_recorded_failure_hint`). It never reaches `indexing-in-progress`. Both pass sites write the record on `index() < 0` and clear it on success. The literal `if indexer.index(project_root) < 0:` and `background cold index failed` are kept.
  - `probe` computes `failure = _model_failure(named_model)` and passes it to every `_with_model_cache` call. When it is set, `_with_model_cache` puts "last index could not load model: <cause>" first in `detail`, sets `runnable: False` and `reason: "model-load-failed"`, and puts `MODEL_FAILURE_FIX` in front of the remediation. The "the next index downloads ~50 MB" note is not added.
- `src/codeintel/setup/doctor.py` `_probe_engine`: a row with `reason == "model-load-failed"` gets `runnable: False` (so `status: fail`), and a remediation naming network/proxy and `FASTEMBED_CACHE_PATH` (`MODEL_FAILURE_FIX`) when the probe's own text lacks either.
- `src/codeintel/gateway/gateway.py`: `_SEARCH_FALLBACK_REASONS` = {index-failed, indexing-in-progress, engine-unavailable, no-index}. In `_query`, after the overview fallback: if the search was auto-routed, semantic answered null with one of those reasons, and `self.allows(role, "pattern")`, it dispatches `self.graph` with op `pattern` and the same target. If that answers, `_name_search_fallback` returns the graph envelope with `fallback: "graph-name-search"`, the graph's own gaps plus `{section: "scope", kind: "name-only-fallback", detail: "matched by name, not meaning: ..."}` (so `confidence: partial`), and a hint that keeps the semantic reason and hint, plus the graph hint if any. A fallback answer is not cached. No second `maybe_reindex` and no second root check.
- Tests: `tests/test_background_index_failure.py` (+15 cases), `tests/test_doctor.py` (+2), `tests/test_semantic_provider.py` (+2), new `tests/test_search_fallback.py` (12 cases).

**Proof**
- Fail-fast from a fresh provider: `test_a_new_process_fails_fast_with_the_recorded_cause[False|True]` passes. The record comes from a real inline pass against the `_BlockedEmbedding` fake (raises fastembed's "Could not load model ... from any source."). The test then clears `_BG_INDEX_FAILED`/`_BG_INDEX_STARTED` and asserts: `index-failed` in < 1 s, "could not load" in the hint and "first-time indexing" not in it, the model constructed only once, and no background start. The record's shape and atomic write are covered by `test_a_model_load_failure_is_written_to_a_record_under_codeintel_home`. It failed at 5319ec9 and passes now.
- `FASTEMBED_CACHE_PATH` changed means retry: `test_a_changed_fastembed_cache_path_lets_an_index_be_tried_again` (model constructed twice) passes. A different model and model files appearing also retry (`test_a_different_model_...`, `test_model_files_that_appear_...`).
- Untrusted records mean retry: `test_a_record_that_cannot_be_trusted_lets_an_index_be_tried_again[older-than-10-minutes|future-at|not-json|not-an-object|at-not-a-number|cause-not-text]` all pass, and the answer's cause comes from the new pass, not the planted text. A symlinked record is ignored. A successful pass removes the record. A failed background pass writes it, and a new process then answers `index-failed` without starting a second pass. An unwritable `CODEINTEL_HOME` still returns `index-failed`.
- Doctor: `test_doctor_reports_semantic_not_runnable_after_a_recorded_model_failure` passes. The record comes from a real failed pass; after that `run_doctor` gives semantic `runnable False`, `status fail`, detail `last index could not load model: could not load embedding model ...`, and a remediation with `FASTEMBED_CACHE_PATH` and `network`. `test_doctor_maps_a_model_load_failure_reason_to_a_failing_row` also passes.
- Fallback: `tests/test_search_fallback.py` passes 12/12. Auto search with each of the four reasons returns graph `pattern` rows with `fallback: graph-name-search`, the `name-only-fallback` gap and `confidence: partial`. Explicit `engine=semantic` and `below-floor` get no fallback; no name match returns the semantic answer unchanged; `maybe_reindex` is called once; the fallback is not cached.
- RBAC: `test_a_role_allowed_search_but_not_pattern_gets_no_fallback` passes. `gw.allows("searcher", "pattern") is False`, the graph is never called, and the answer is `index-failed` with no `fallback`; an admin gets the fallback.
- Check block, run after the last edit:
  - `.venv/bin/pytest tests/test_background_index_failure.py tests/test_semantic_provider.py tests/test_search_fallback.py tests/test_index_failure_reason.py tests/test_hardening.py tests/test_gateway.py tests/test_rbac.py tests/test_summary_integrity.py -q -o addopts=` gives `316 passed, 1 xfailed`. The xfail is the existing strict `test_c4_coverage_partitions_...` marker.
  - `.venv/bin/pytest tests/test_doctor.py -q -o addopts= --deselect ...cannot_be_read` gives `50 passed, 1 deselected`.
  - The five check-block files not on Touches are unchanged (`git diff --stat cbc476d -- tests` lists only the four test files on Touches).
- Every test file that mentions Gateway/SemanticProvider/doctor/code_query_handler (50 files): `1398 passed, 4 skipped, 1 deselected, 1 xfailed`. The whole Test command is left to the conductor's gate.
- Also run: `ruff check src tests bench` all passed; `mypy` no issues in 88 files; compileall OK; `tests/test_architecture.py` 6 passed. `codeintel c4 . --check --out /tmp/codeintel-c4-check` printed `c4 failed: engine-unavailable`. The graph backend is unreachable in this container, the same environmental failure ticket 03 recorded.
- Live, with the real blocked huggingface download (403) in a scratch repo and an empty `FASTEMBED_CACHE_PATH`:
  - The first `codeintel query --op search --engine semantic` took 40.6 s and returned `index-failed`, and the record was written.
  - The second took 0.86 s and returned `index-failed` with no "first-time".
  - `status` printed "last index could not load model: ...".
  - `doctor --json` gave semantic `runnable False`, status `fail`, and the FASTEMBED_CACHE_PATH remediation.

**Decisions**
- The record is written on any failed pass (`index() < 0`, the same condition that yields `index-failed`), not by matching the `EmbeddingModelUnavailable` text. It only applies while the model files are absent, so a failure with the weights present never blocks a retry.
- One machine-wide file, as the ticket names it, keyed by its inputs, not per repo. The model failure is a property of the host.
- `at` is wall-clock `time.time()`: it has to compare across processes. A record dated in the future is ignored with no skew allowance.
- Fail-fast also covers the `CODEINTEL_REINDEX=off` inline pass on a warm repo. With the weights absent, the query itself cannot be embedded either.
- `model_is_cached(...) is not True` counts as "still absent", so an unknown answer does not lift the record early. That is bounded by the 10-minute window.
- The fallback envelope keeps the graph's `op: pattern` and `engine: graph`: that is what ran. `fallback` marks it. It is built as a plain dict and `cast` to `Result`, because `fallback` is not a `Result` key and `core/provider.py` is outside Touches. There is no type-ignore.
- The fallback is not cached. The cache key's semantic db stamp does not move while semantic is down, so a cached name-only answer would stop semantic from being asked again until the db changed.
- The `runnable: False` and `reason` for the record are set by subscript in `_with_model_cache` and `_probe_engine`, so no new dict-literal census site was added. `test_summary_integrity.py` passes unchanged.

**Shortcuts taken**
- Any failed pass with absent weights is recorded, including a non-model failure such as a locked db that happens while the weights are absent. That blocks retries for up to 10 minutes per cache path. Worth tightening if `Indexer` ever exposes the exception class (it keeps only `last_error` text).
- A pass that succeeds for a repo with nothing to embed clears the record for that model without proving the weights load. The next search elsewhere retries once and records again.

**For later tickets**
- `codeintel index` (`src/codeintel/cli/index.py`), the background reindexer's semantic pass (`gateway/reindexer.py`) and `setup/onboarding.py` do NOT write or clear the record: they were outside Touches. Only the provider's inline and background passes do. As a result, 4 of the 9 acceptance tests in `plans/trust-fixes/acceptance/test_tf_semantic_failure.py` still fail: search-within-1s, doctor, status, and FASTEMBED_CACHE_PATH-change. Each sets up its failure with `codeintel index`, so no record exists. Measured: 4 failed, 5 passed. The fallback, RBAC and reindexer-order tests pass. The fix is one call each: `_write_model_failure(model, indexer.last_error)` on `< 0` and `_clear_model_failure(model)` otherwise, at least in `cli/index.py`. That needs a new ticket or a widened Touches before ticket 13's acceptance run.
- Ticket 12 can key the prompt steps on doctor's semantic `reason == "model-load-failed"`. Its remediation is `MODEL_FAILURE_FIX` (exported from `engines/semantic/provider.py`).

**Round 2** (reopened by c68543e after the plan repair d2e1906; work started at c68543e)
- No code change. The gap named under For later tickets (`codeintel index`, the reindexer's semantic pass and onboarding do not write or clear the failure record) is now ticket 16 (`16-semantic-failure-record-everywhere.md`, blocked by 10); it is not built here.
- Check block re-run on HEAD c68543e: the first command gives `316 passed, 1 xfailed`; the doctor command gives `50 passed, 1 deselected`.

## Review findings (round 1, pair review)

QUALITY
1. major src/codeintel/engines/semantic/provider.py: a hostile or corrupt record can set its own retry limit. `_read_failure_record` accepts any int/float `at`, and `json.loads` turns `NaN` into `float('nan')`; in `_model_failure`, `age = time.time() - nan` is NaN, so both `age < 0` and `age >= _FAILURE_RECORD_MAX_AGE_S` are False and the record applies forever. Checked: `{"cause":"evil","at":NaN,...}` makes `_model_failure` return "evil" (Infinity, -Infinity, 1e400 are ignored). While it stands every search gets `index-failed` with attacker text, the index never runs, and doctor reports not runnable. Breaks the level-3 rule that a bad record never blocks indexing for good. Fix: reject non-finite `at` (`math.isfinite`) and add a NaN case to the ignored-record tests in tests/test_background_index_failure.py.
2. minor src/codeintel/engines/semantic/provider.py: the inline path writes the record for any failed `Indexer.index` (unwritable db, walk error), not only a model-load failure. Fix: write only in the model-load case.
3. minor src/codeintel/gateway/gateway.py: the fallback's RBAC gate uses `Gateway.allows`, which returns True if `policy.is_allowed` raises; the main gate calls `is_allowed` directly. Prefer failing closed (no fallback) on this path.

Probes: record contents/types/age/size, path (O_NOFOLLOW, mkstemp 0600, os.replace, unlink link only), cause capping, FASTEMBED_CACHE_PATH/model changes, gateway fallback routing: no way through apart from findings 1 and 3. Done when: 316 passed, 1 xfailed; 50 passed, 1 deselected. END eb00c6a = HEAD. Acceptance failures in test_tf_semantic_failure.py belong to ticket 16.

codeintel: changed cbc476df60570c3a3b952601e68113435ff082cb: helped

REVIEW: FAIL

SPEC
- Tests on the fastembed fakes that fail on main (fresh provider index-failed under 1 s without "first-time indexing"; FASTEMBED_CACHE_PATH change retries; old/future/invalid record retries; doctor runnable False with cause; search fallback with gap): MET.
- test_search_fallback: a role allowed search but not pattern gets no fallback: MET.
- The 8-file pytest command passes; last five files unchanged: MET.
- tests/test_doctor.py passes except the deselected root-only test: MET.
- All existing tests still pass: MET (2360 passed, 31 skipped, 1 deselected, 1 xfailed).
- Acceptance test_tf_semantic_failure.py: 5 passed, 4 failed; all 4 set up the failure with `codeintel index`, which ticket 16 covers.
- scope: OK. Production changes only provider.py, gateway.py, doctor.py; cli/status.py unchanged.
- test first: OK (5319ec9).

Range cbc476d..eb00c6a (HEAD). Answer not read.

LEVEL: 3

REVIEW: PASS
