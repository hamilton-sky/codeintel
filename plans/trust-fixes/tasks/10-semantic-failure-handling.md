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
