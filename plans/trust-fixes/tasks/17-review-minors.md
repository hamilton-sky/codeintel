# Fix the minor findings the reviews left for later

Type: task
Status: resolved
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

- **Built**: `src/codeintel/engines/graph/backend.py` (1, 2), `src/codeintel/engines/graph/provider.py` (11), `docs/trust.md` (3, new "Known limits"), `src/codeintel/core/redact.py` (4), `src/codeintel/transports/mcp.py` (5), `src/codeintel/core/containment.py` (6), `src/codeintel/engines/semantic/db.py` + `src/codeintel/engines/semantic/provider.py` (7, 9), `src/codeintel/engines/graph/render.py` (8), `src/codeintel/setup/agent_prompt.py` (10), `src/codeintel/gateway/reindexer.py` (12), `src/codeintel/gateway/gateway.py` (13). Tests (test-only commit b56478f): `tests/test_agent_hints.py`, `tests/test_redaction_boundary.py`, `tests/test_mcp_server.py`, `tests/test_hardening.py`, `tests/test_background_index_failure.py`, `tests/test_render_names.py`, `tests/test_agent_prompt.py`, `tests/test_edge_staleness.py`, `tests/test_reindexer.py`.
- **Proof**:
  - One test per item 1, 2, 4, 5, 6, 8, 10, 11, 12 (lstat), failing first: with the implementation stashed on b56478f the nine touched test files gave `23 failed, 262 passed`. The failures were item 1 `test_a_huge_stderr_is_cleaned_in_well_under_a_second`; item 2 four `test_every_project_id_in_a_list_is_withheld[...]` plus `test_the_backends_deprecation_warning_is_not_rewritten...`; item 4 five `test_a_home_path_after_a_scheme_a_doubled_slash_or_a_flag_is_redacted[...]` plus `test_the_resolved_home_is_redacted_when_it_differs`; item 5 `test_root_not_found_echoes_at_most_200_characters_of_the_root` and `test_a_file_given_as_the_root_is_said_to_be_not_a_directory`; item 6 `test_write_contained_names_why_an_in_tree_target_is_refused[<lambda>-not-a-file1]` and `test_write_contained_closes_the_temp_descriptor_when_fdopen_fails`; item 7 `test_the_model_load_failure_prefix_is_the_one_the_exception_is_built_with`; item 8 `test_a_heading_whose_second_segment_starts_with_a_digit_and_a_space_is_left_alone`; item 10 `test_an_index_failure_that_is_not_the_model_gets_no_model_download_advice`; item 11 `test_an_index_rewritten_while_the_answer_was_read_marks_it_stale`; item 12 `test_the_fingerprint_does_not_follow_a_tracked_symlink` and `test_a_root_without_a_fingerprint_is_not_flagged_reindexing_for_a_debounce_pass`. The 23rd failure was the uptime case described below, which is not caused by this ticket.
  - The check block with the change gave `1 failed, 350 passed, 1 xfailed`. The one failure, `tests/test_reindexer.py::test_an_unchanged_tree_is_fingerprinted_at_most_once_per_debounce_window`, also fails at HEAD without this change. The machine had just restarted (uptime 146 s), so `time.monotonic()` is below the test's `debounce_seconds=1000`, and `_last_fired` defaults to 0.0, so the first `maybe_reindex` is debounced. It depends on the environment, not on this ticket (see For later tickets). A rerun after uptime passed 1000 s is reported in the build report.
  - Neighbouring tests (backend_failure_detail, cache, cli_commands, could_not_ask_semantic, doctor minus the deselected root-only test, gateway, gateway_freshness, graph_failure_population, graph_provider, graph_stdin, index_age, semantic_provider, architecture): `413 passed, 2 skipped`. Also cli_install_offer_injection, dispatch_callers, http_server, mapper, rbac: `184 passed`.
  - `ruff check src tests bench`: All checks passed. `mypy`: no issues in 88 files. `codeintel c4 . --check`: 0 gating findings, exit 0. compileall: exit 0. The whole Test command is left to the conductor's gate.
- **Decisions**:
  - 1: every pattern `_agent_facing` runs is linear (`[^"\\]*?` in `_SEARCH_ADVICE`, `[^()]*` in the call rewrite), rather than cutting the text before cleaning, because a cut first could split a project id or a home path.
  - 2: the backend's raw-JSON deprecation line is dropped like log noise. Ids in a comma list after `project(s)` and in any `[...]` are withheld (one hyphen is enough there).
  - 3: `GraphProvider` has no role (no `role` anywhere in `engines/graph/provider.py`), so nothing was plumbed. The limit is recorded under `docs/trust.md` "Known limits": names come only from the role's own root, never an ancestor project.
  - 4: `_SLASHED_HEAD` also accepts `file://`, a doubled `/` not preceded by `:` (so `https://home/...` stays a URL), and a lone `-X` flag. `realpath(home)` is redacted when it differs (cached per home).
  - 5: the root is redacted first, then cut to 200 characters with `…`, so the cut cannot split the home path before redaction sees it.
  - 6: ENOTDIR on the walk gives `not-a-file` (dir_fd and lstat branches). The temp file is opened with `os.fdopen(..., closefd=False)`, and an outer `finally` closes `fd` exactly once. The interrupted builder had swapped `fdopen` for `os.write` and edited the committed test to patch `os.write`. I reverted both so the failing-test commit stays as written.
  - 7: `MODEL_LOAD_FAILURE_PREFIX` lives in `db.py`. The provider imports it inside `_is_model_load_failure`, because `db.py` imports `sqlite_vec` at module scope.
  - 11: `_stamp_staleness` reads `graph_db_mtime` again. If it differs from `written`, or cannot be read, the answer is stale with "the graph index was rewritten while this answer was being read". The conftest stub is unchanged.
  - 12: lstat in `_tree_fingerprint`. A pass for a root without a fingerprint is still submitted per window, but `maybe_reindex` returns False, so the gateway does not flag `reindexing`. This needed no gateway contract change, so README was not edited.
  - 13: a comment states the cost: two or three git calls per hit, each capped at 30 s, about 5 ms here.
- **Shortcuts taken**: none.
- **For later tickets**: `test_an_unchanged_tree_is_fingerprinted_at_most_once_per_debounce_window` (and any reindexer test using `debounce_seconds` greater than the uptime) fails on a freshly booted machine, because `_last_fired` defaults to 0.0 on the `time.monotonic()` clock. This is a latent bug outside this ticket's items: a root is debounced for its first window after boot. It is worth a follow-up (default to `-inf`).
