# Fix the minor findings the reviews left for later

Type: task
Status: open
Blocked by: 16
Test first: yes
Level: 3
Builder: backend
Touches: src/codeintel/engines/graph/backend.py, src/codeintel/engines/graph/provider.py, src/codeintel/core/redact.py, src/codeintel/transports/mcp.py, src/codeintel/core/containment.py, src/codeintel/engines/semantic/provider.py, src/codeintel/engines/semantic/db.py, src/codeintel/engines/graph/render.py, src/codeintel/setup/agent_prompt.py, src/codeintel/gateway/reindexer.py, src/codeintel/gateway/gateway.py, README.md, docs/trust.md, tests/test_agent_hints.py, tests/test_redaction_boundary.py, tests/test_mcp_server.py, tests/test_hardening.py, tests/test_background_index_failure.py, tests/test_render_names.py, tests/test_agent_prompt.py, tests/test_edge_staleness.py, tests/test_reindexer.py

The per-ticket reviews and the second feature review passed, but left minor findings that `plans/trust-fixes/followups.md` lists under "feature review (left for later)". The user asked for everything to be merged to main, so these are fixed before the acceptance ticket rather than left for a later release. Each item names its file and the fix.

## What to do

Fix items 1 to 13 of `followups.md` "feature review (left for later)":

1. `backend.py`: bound the stderr text before `_agent_facing` (or make `_SEARCH_ADVICE` linear) so a 100 KB stderr is cleaned in well under a second.
2. `backend.py`: withhold every slug-shaped id in a comma list after `project`/`projects` and inside `[...]`; do not rewrite tool names inside the backend's deprecation warning (drop that line or leave it).
3. `provider.py` `_did_you_mean`: offer close names only when the role may run `pattern` (the provider has no role: if the role is not reachable there, record the decision in the Answer and in `docs/trust.md`'s known limits instead of plumbing it, and say so).
4. `redact.py`: redact the home path after `file://`, a doubled `/`, and a `-X` flag prefix; also redact `os.path.realpath(home)` when it differs.
5. `mcp.py`: cut the echoed root in the `root-not-found` hint to 200 characters, and say "is not a directory" when the path exists but is not one.
6. `containment.py`: `_why_not_a_dir` returns `not-a-file` (not `dangling`) on ENOTDIR; close `fd` if `os.fdopen` raises in `_write_walk`.
7. `src/codeintel/engines/semantic/provider.py` + `src/codeintel/engines/semantic/db.py`: define the model-load failure prefix once in `db.py` and import it.
8. `render.py`: `Pre-1.0 notes for 2.x` is shown unchanged; real graph slugs are still stripped.
9. `src/codeintel/engines/semantic/db.py`: the dimension-mismatch warning says `codeintel reset --all --semantic-only`.
10. `agent_prompt.py`: `model_failed` matches only `model-load-failed`.
11. `provider.py` `_stamp_staleness`: compare the db mtime read before dispatch with the one after; a change means stale. Do not change the `tests/conftest.py` stub.
12. `reindexer.py`: use `os.lstat` in `_tree_fingerprint`. For roots that cannot be fingerprinted, do not flag `reindexing` on a pass submitted only because the debounce ran out; if that is not possible without changing the gateway contract, document the behaviour in README's "Keeping answers fresh" and say so in the Answer.
13. `gateway.py` `_index_moved`: a comment states the per-hit git cost.

## Not in this ticket

- CHANGELOG (ticket 13).

## Done when

- One test per item 1, 2, 4, 5, 6, 8, 10, 11 and 12 (lstat), each failing before the change.
- All existing tests still pass.

```check
$ .venv/bin/pytest tests/test_agent_hints.py tests/test_redaction_boundary.py tests/test_mcp_server.py tests/test_hardening.py tests/test_background_index_failure.py tests/test_render_names.py tests/test_agent_prompt.py tests/test_edge_staleness.py tests/test_reindexer.py tests/test_summary_integrity.py -q -o addopts=
prints passed
```

## Answer
