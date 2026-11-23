# Record and clear the semantic model failure on every index pass, not only the provider's own

Type: task
Status: open
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
