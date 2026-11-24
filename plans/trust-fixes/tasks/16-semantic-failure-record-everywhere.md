# Record and clear the semantic model failure on every index pass, not only the provider's own

Type: task
Status: resolved
Blocked by: 10
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/cli/index.py, src/codeintel/gateway/reindexer.py, tests/test_cli_commands.py, tests/test_reindexer.py

Ticket 10 added the persisted semantic failure record (`codeintel_home()/semantic_failure.json`, written by `_write_model_failure` and removed by `_clear_model_failure` in `src/codeintel/engines/semantic/provider.py`), but only the semantic provider's own inline and background passes write and clear it. `codeintel index` (`src/codeintel/cli/index.py`) and the reindexer's semantic pass (`src/codeintel/gateway/reindexer.py`) run the indexer without touching the record, so after `codeintel index` fails to load the model the next `search` still waits ~40 s and `doctor`/`status` show no cause. This is why 4 of the 9 tests in `plans/trust-fixes/acceptance/test_tf_semantic_failure.py` (search within 1 s, doctor, status, and the `FASTEMBED_CACHE_PATH` change) still fail after ticket 10: each sets up its failure with `codeintel index`.

## What to do

1. In `src/codeintel/cli/index.py`, after the semantic indexer runs: when it returns below 0, call the provider's record writer with the model and the indexer's last error; otherwise clear the record. Use the provider's helpers; do not write the file a second way.
2. Do the same in the reindexer's semantic pass.
3. Keep the provider's record rules as ticket 10 built them (symlink, size, age, model and `FASTEMBED_CACHE_PATH` match). Do not change `src/codeintel/engines/semantic/provider.py` beyond exposing a helper if one is private; if it must change, say so in the Answer.

## Not in this ticket

- The record's format and rules (ticket 10).
- CHANGELOG (ticket 13).

## Done when

- `tests/test_cli_commands.py`, with the existing fastembed fakes and no network: `codeintel index` on a repo where the model fails leaves a record that the next `search` from a fresh provider honours (`index-failed` in under 1 s); a later successful `codeintel index` clears it. Fails on main.
- `tests/test_reindexer.py`: a reindexer semantic pass that fails writes the record; one that succeeds clears it. Fails on main.
- `plans/trust-fixes/acceptance/test_tf_semantic_failure.py` passes.
- All existing tests still pass.

```check
$ .venv/bin/pytest tests/test_cli_commands.py tests/test_reindexer.py tests/test_background_index_failure.py tests/test_semantic_provider.py tests/test_search_fallback.py plans/trust-fixes/acceptance/test_tf_semantic_failure.py -q -o addopts=
prints passed
```

## Answer

Work started at 4ee584896581045cc9bf398f9eb65febfab1c53d (the prompt named cae11ca; HEAD had moved on by the plan-repair commit 4ee5848). Test-only commit: e9ea8f2.

- **Built**: `src/codeintel/cli/index.py` and `src/codeintel/gateway/reindexer.py`: after `Indexer.index()` returns, a result below 0 whose `last_error` passes `_is_model_load_failure` calls `_write_model_failure(model_name, last_error)`. Any other result of 0 or more calls `_clear_model_failure(model_name)`. Both import the provider's helpers lazily inside the function. `model_name` is now bound once and passed to the `Indexer`. Tests: `tests/test_cli_commands.py` (3 new tests plus local `_BlockedEmbedding`/`_WorkingEmbedding` fakes and a `blocked_model` fixture) and `tests/test_reindexer.py` (2 new tests with the same fakes).
- **Proof**:
  - CLI: at the test-only commit e9ea8f2, `test_a_failed_index_leaves_a_record_the_next_search_honours_at_once` and `test_a_successful_index_clears_the_record` failed because `semantic_failure.json` did not exist. After the change, `.venv/bin/pytest tests/test_cli_commands.py tests/test_reindexer.py -q -o addopts=` gave 124 passed. The first test checks that a fresh `SemanticProvider(blocking_index=True)` search returns `index-failed` in under 1 s, with no "first-time indexing" and no second model load (`built == 1`).
  - Reindexer: both new tests failed at e9ea8f2 for the same reason (no record). They pass now (same run as above).
  - Acceptance: `.venv/bin/pytest plans/trust-fixes/acceptance/test_tf_semantic_failure.py -q -o addopts=` gave 9 passed. It runs here: its fixture sets `PATH=/usr/bin:/bin`, so it never reaches the graph backend and the running daemon does not block it.
  - Check block: `.venv/bin/pytest tests/test_cli_commands.py tests/test_reindexer.py tests/test_background_index_failure.py tests/test_semantic_provider.py tests/test_search_fallback.py plans/trust-fixes/acceptance/test_tf_semantic_failure.py -q -o addopts=` gave 218 passed.
  - All existing tests: left to the conductor's gate. Also run: `ruff check src tests bench` gave All checks passed; `mypy` gave no issues in 88 files; `tests/test_index_failure_reason.py tests/test_doctor.py tests/test_e2e.py` gave 94 passed, 1 deselected.
- **Decisions**: The record is written only for a model-load failure, as in ticket 10's provider passes and the caller's note. A new test pins that an `OSError: disk full` failure writes no record. A failed non-model pass leaves an existing record alone; it does not clear it, the same as the provider. `provider.py` is unchanged: the underscore helpers are imported as they are.
- **Shortcuts taken**: none.
- **For later tickets**: none.
