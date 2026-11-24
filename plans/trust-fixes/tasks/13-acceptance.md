# Run the bar end to end, prove each regression test fails on eb0902f, and write the CHANGELOG

Type: task
Status: open
Blocked by: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12, 14, 16, 17
Test first: no
Level: 1
Touches: CHANGELOG.md

The acceptance ticket. It proves the map's bar and records the change for users.

1. Run the whole gate from commands.md: the Test command (with its one deselect), `.venv/bin/ruff check src tests bench` (CI's lint command, which replaces the brief's `ruff check .` by the user's decision; spec.md Decisions, "The lint bar"), `.venv/bin/mypy`, and `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check`.
2. Prove each regression test against the old code. Ticket 14's c4 tests are not red-team findings and are not rows of the table.
   - In a scratch worktree outside the repo (`git worktree add /tmp/trust-fixes-main eb0902f`), copy in the new test files and the fixtures under `tests/fixtures/blindspots/`.
   - Run each regression test listed in tickets 01-12's Answers against eb0902f's `src`, using the repo venv with `PYTHONPATH` pointed at the worktree's `src`, and record that each one fails.
   - Remove the worktree afterwards.
   - Put the table (finding id → test → fails on eb0902f / passes on branch) in this ticket's Answer.
   - A test that passes on eb0902f is a finding not proven: reopen the ticket that claimed it.
3. Write CHANGELOG entries under `## [Unreleased]`: one line per T-group under Fixed (and Security for T6 and T7's leak), each naming the user-visible change, plus one Fixed line for ticket 14 (`c4 --check` matches `[[layers.allow]]` against file-level import edges at any depth). Do not bump the version.

## Not in this ticket

- The acceptance tests under `plans/trust-fixes/acceptance/` are written by the tester from spec.md and run by the conductor.
- No tag or release: the user tags from their Mac.

## Done when

- `.venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read` passes.
- `.venv/bin/ruff check src tests bench` prints `All checks passed!` and `.venv/bin/mypy` prints `Success`.
- `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check` exits 0 and prints `0 gating finding(s)`.
- The Answer holds the 34-row table, and every row reads "fails on eb0902f, passes on branch" (the reviewer checks this).
- `grep -c "" CHANGELOG.md` grows, and `## [Unreleased]` holds a Fixed section naming T1-T10's changes and ticket 14's c4 allowlist fix (the reviewer checks this).

```check
$ .venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
prints passed
$ .venv/bin/ruff check src tests bench
prints All checks passed!
$ .venv/bin/mypy
prints Success
$ .venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check
prints 0 gating finding(s)
```

## Reference

- map.md, "The bar"
- spec.md, Goal and the bar

## Answer

Work started at commit 8bddb2021338a1c5e9f0eec954de9c5553f3db11.

**Built**
- `CHANGELOG.md`: under `## [Unreleased]`, a new `### Fixed` section (one bullet per T-group T1-T10 plus one for ticket 14's c4 allowlist fix) and a new `### Security` section (T6's symlink writes, T7's backend-error id leak). No version bump. `grep -c "" CHANGELOG.md`: 3224 before, 3309 after.
- No code changed.

**Proof**
- `.venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read` (run on 8bddb20, before the CHANGELOG edit; the edit touches no code): `2435 passed, 35 skipped, 1 deselected, 1 xfailed, 1 warning in 199.40s`, exit 0, coverage 91.02% (required 83%). After the CHANGELOG edit, the test files that read CHANGELOG.md (`test_docs_deadcode_withdrawal, test_graph_failure_population, test_graph_provider, test_model_dimension, test_rerank, test_version_skew`): 153 passed.
- `.venv/bin/ruff check src tests bench`: `All checks passed!`. `.venv/bin/mypy`: `Success: no issues found in 88 source files`.
- `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check`: with the default PATH it prints `c4 failed: engine-unavailable` and exits 1, because the graph backend binary is not on this host's PATH (learnings (11), the same environment failure tickets 03 and 10 recorded). With `PATH=/root/.cache/codebase-memory-mcp/0.10.8:$PATH`: `VIOLATIONS (0 gating, 1 allowlisted)`, `CYCLES (1)` allowed, `0 gating finding(s) — exit 0`, exit 0.
- Regression tests against eb0902f: `git worktree add /tmp/trust-fixes-main eb0902f`; the branch's whole `tests/` (new files, changed files, `conftest.py`, `tests/fixtures/blindspots/`) copied over it; for the one doc finding with no unit test, `plans/trust-fixes/acceptance/test_tf_small_fixes.py` with its `conftest.py`/`_fakegraph.py` copied to the same path, unchanged (its `REPO_ROOT` resolves to the worktree, so it reads eb0902f's `docs/deploy.md`). Run from the worktree with `PYTHONPATH=/tmp/trust-fixes-main/src` and the repo venv (`codeintel.__file__` = `/tmp/trust-fixes-main/src/codeintel/__init__.py`, version 0.29.3), `-o addopts=`, one pytest run per finding. The same node ids then run on the branch from the repo. Worktree removed afterwards (`git worktree remove --force`, `git worktree prune`).
- Two test files fail at import on eb0902f (`test_gateway_freshness.py`: `graph_slug` not in `core.paths`; `test_agent_hints.py`: `_agent_facing` not in `backend`). To show those rows fail on the behaviour and not only on the import, scratch copies inside the worktree swapped just that import (`setup.reset._graph_slug`, which eb0902f has; `_agent_facing = None`, which the chosen tests never call). Every chosen test then failed on its assertion, as listed in the table.

| # | Finding | Test(s) | eb0902f: how it fails | Result |
|---|---|---|---|---|
| 1 | uncommitted-changed-no-staleness | `test_index_age.py::test_an_uncommitted_edit_with_no_reindex_is_stale_and_partial`, `::test_an_untracked_file_the_index_has_no_symbols_for_is_stale` | 2 failed: `'stale-index' in []` | fails on eb0902f, passes on branch |
| 2 | nongit-uncommitted-claims-clean | `test_index_age.py::test_uncommitted_changed_outside_a_git_repo_is_not_a_git_repo` | 1 failed: "working tree clean", `None == 'not-a-git-repo'` | fails on eb0902f, passes on branch |
| 3 | index-age-misses-out-of-diff-commits | `test_index_age.py::test_a_commit_after_the_index_is_stale_even_when_no_file_in_the_diff_is` | 1 failed: `'stale-index' in []` | fails on eb0902f, passes on branch |
| 4 | index-age-2s-slack | `test_index_age.py::test_an_edit_a_tenth_of_a_second_after_the_index_is_stale` | 1 failed: `'stale-index' in []` | fails on eb0902f, passes on branch |
| 5 | callers-callees-stale-complete | `test_edge_staleness.py::test_callers_row_in_a_file_edited_after_the_index_is_unstated`, `::test_callers_row_in_a_file_renamed_after_the_index_is_unstated`, `::test_callees_of_a_target_whose_file_was_deleted_are_stale_and_not_complete`, `::test_impact_has_the_stale_index_gap_once_not_once_per_half` | 4 failed: `want exactly one stale-index gap: []` | fails on eb0902f, passes on branch |
| 6 | server-cache-survives-external-reindex | `test_gateway_freshness.py::test_a_graph_db_rewritten_by_another_process_is_a_cache_miss`, `::test_a_deleted_graph_db_is_a_cache_miss` | collection ImportError (`graph_slug`); with the import shim, 2 failed: `cached` `True is False` | fails on eb0902f, passes on branch |
| 7 | reindexing-flag-self-triggered | `test_reindexer.py::test_an_unchanged_git_tree_submits_nothing_and_flags_nothing` | 1 failed: `maybe_reindex` returned `None`, not a bool | fails on eb0902f, passes on branch |
| 8 | chain-ignores-destination | `test_chain_destination.py::test_a_reachable_destination_gives_the_path_with_its_hops_in_order`, `::test_an_unreachable_destination_is_no_path_found_and_partial`, `::test_a_missing_destination_is_target_not_found` | 3 failed: B dropped, `## Call chain for main` neighbourhood printed | fails on eb0902f, passes on branch |
| 9 | chain-no-resolution-labels | `test_chain_destination.py::test_hops_without_a_strategy_print_unstated_and_add_one_gap`, `::test_a_bare_chain_also_labels_unstated_hops` | 2 failed: hops printed with no `[unstated]` | fails on eb0902f, passes on branch |
| 10 | complete-callees-miss-aliased-import | `test_blindspot_gaps.py::test_callees_of_a_body_calling_an_import_alias_with_no_row_is_not_complete` | 1 failed: no alias gap (`assert []`) | fails on eb0902f, passes on branch |
| 11 | nested-fn-false-verified-caller | `test_blindspot_gaps.py::test_a_caller_that_defines_its_own_nested_namesake_is_not_resolved` (py, ts) | 2 failed: `outer` `verified True is False` | fails on eb0902f, passes on branch |
| 12 | ts-safe-for-destructive-wrong-half | `test_blindspot_gaps.py::test_callers_of_a_symbol_imported_under_an_alias_is_not_safe` (2 params), `::test_an_lsp_ts_local_row_is_possible_not_verified`, `::test_ts_callers_of_a_method_an_interface_declares_has_the_gap` | 4 failed: no alias gap; `lsp_ts_local` row verified; no interface gap | fails on eb0902f, passes on branch |
| 13 | dynamic-dispatch-asymmetric | `test_blindspot_gaps.py::test_callers_of_a_method_lists_the_call_a_sibling_was_bound_to_by_name` | 1 failed: `StopIteration` (no `total_area` row) | fails on eb0902f, passes on branch |
| 14 | callers-collapse-to-module-scope | `test_caller_counts.py::test_a_caller_only_at_module_scope_is_an_import_and_not_a_direct_call` | 1 failed: `Confidence: complete`, `## Callers of safe_null_result (2)` | fails on eb0902f, passes on branch |
| 15 | usage-rows-from-shadowing-names | `test_caller_counts.py::test_a_parameter_that_shadows_the_name_is_not_a_reference_under_a_narrowed_target` | 1 failed: `hier.param_shadow` listed | fails on eb0902f, passes on branch |
| 16 | ambiguous-count-wrong | `test_caller_counts.py::test_the_banner_counts_the_symbols_with_edges_and_the_ones_without`, `::test_an_unqualified_target_never_prints_a_settle_it_qualifier` | 2 failed: old banner / "Settle it" printed | fails on eb0902f, passes on branch |
| 17 | map-symlink-write-through | `test_hardening.py::test_map_refuses_symlinked_code_intel_md`, `::test_index_refresh_refuses_symlinked_code_intel_md`, `::test_map_refuses_code_intel_md_linked_anywhere`, `::test_inject_refuses_context_files_linked_outside_the_repo`, `::test_inject_refuses_agents_md_linked_into_the_git_dir`, `::test_mcp_code_map_reports_a_symlinked_code_intel_md` | 13 failed (all params): `map.run` returned 0, victim written | fails on eb0902f, passes on branch |
| 18 | install-inject-advice-noop | `test_cli_commands.py::test_map_inject_creates_agents_md_when_the_repo_has_no_context_file` | 1 failed: `FileNotFoundError` for `AGENTS.md` | fails on eb0902f, passes on branch |
| 19 | deploy-doc-stale-symlink-hole | `plans/trust-fixes/acceptance/test_tf_small_fixes.py::test_deploy_doc_says_the_snippet_symlink_hole_is_closed_via_open_contained` (ticket 08 added no unit test for the doc; this is the only test that pins it) | 1 failed: `'open_contained' in` eb0902f's deploy.md | fails on eb0902f, passes on branch |
| 20 | status-root-denied-200-fake-fallback | `test_rbac.py::test_status_for_a_root_outside_the_role_is_403`, `::test_a_bare_status_under_a_role_with_restricted_roots_is_403` | 2 failed: `200 == 403` | fails on eb0902f, passes on branch |
| 21 | nonexistent-root-says-not-indexed | `test_mcp_server.py::test_a_root_that_does_not_exist_is_root_not_found_before_any_query` | 1 failed: `'first-time indexing started'`, reason `''` | fails on eb0902f, passes on branch |
| 22 | redact-mid-path | `test_redaction_boundary.py::test_a_home_string_in_the_middle_of_a_path_is_left_alone` (3 params) | 3 failed: `'/srv~/app' == '/srv/root/app'` | fails on eb0902f, passes on branch |
| 23 | mcp-budget-measured-on-wrong-bytes | `test_mcp_answer_size.py::test_the_text_fastmcp_sends_stays_under_the_byte_budget` | 1 failed: `44366 <= 40000` | fails on eb0902f, passes on branch |
| 24 | search-no-graph-fallback-contradictory | `test_search_fallback.py::test_auto_search_falls_back_to_graph_name_search` (4 reasons) | 4 failed: graph `pattern` never called (`[] == [('pattern', 'helper')]`) | fails on eb0902f, passes on branch |
| 25 | search-flaps-first-time-indexing | `test_background_index_failure.py::test_a_new_process_fails_fast_with_the_recorded_cause` (2 params), `test_cli_commands.py::test_a_failed_index_leaves_a_record_the_next_search_honours_at_once` | 3 failed: `'indexing-in-progress' == 'index-failed'` | fails on eb0902f, passes on branch |
| 26 | semantic-blocked-reported-runnable | `test_doctor.py::test_doctor_reports_semantic_not_runnable_after_a_recorded_model_failure`, `test_semantic_provider.py::test_the_probe_reports_the_recorded_cause_instead_of_the_download_note` | 2 failed: `runnable` `True is False` | fails on eb0902f, passes on branch |
| 27 | hints-name-tools-agent-lacks | `test_agent_hints.py::test_a_typo_lists_the_close_name_and_names_no_backend_tool` | collection ImportError (`_agent_facing`); with the shim, failed: hint names `index_repository`, no `compact_for_mcp` | fails on eb0902f, passes on branch |
| 28 | no-edges-hint-garbled-and-wrong-op | `test_agent_hints.py::test_a_no_edges_hint_off_callers_is_about_outgoing_calls_not_dead_code` (callees, impact, context) | collection ImportError; with the shim, 3 failed: "dead code" and `1 1` in the hint | fails on eb0902f, passes on branch |
| 29 | symbol-dead-without-lsp | `test_agent_hints.py::test_graph_symbol_points_at_lsp_not_at_itself` | collection ImportError; with the shim, failed: "did you mean symbol? ops: … changes …" | fails on eb0902f, passes on branch |
| 30 | backend-error-hint-leaks-other-projects | `test_agent_hints.py::test_a_backend_error_never_carries_other_projects_ids` (2 params), `::test_a_backend_project_not_found_is_project_not_indexed` | collection ImportError; with the shim, 3 failed: `a-b-c`/`d-e-f` in the hint; `'backend-error' == 'project-not-indexed'` | fails on eb0902f, passes on branch |
| 31 | reset-help-understates-graph-wipe | `test_cli_help.py::test_reset_help_names_both_indexes_and_offers_semantic_only`, `test_reset.py::test_scoped_semantic_only_clears_the_semantic_index_and_keeps_the_graph` | 2 failed: help says "semantic index" only; `run_reset() got an unexpected keyword argument 'semantic_only'` | fails on eb0902f, passes on branch |
| 32 | ctrl-c-traceback | `test_cli_commands.py::test_ctrl_c_in_a_command_exits_130_with_one_line_and_no_traceback` | KeyboardInterrupt escaped `main()` at test_cli_commands.py:317 and aborted the session (exit 2, "no tests ran"), as learnings (12) says it does when it fails | fails on eb0902f, passes on branch |
| 33 | prompt-reinstalls-for-model-download | `test_agent_prompt.py::test_a_failed_model_download_is_not_fixed_by_reinstalling`, `::test_the_prompt_lists_code_status` | 2 failed: `'pip install'` in the prompt; no `code.status` | fails on eb0902f, passes on branch |
| 34 | qualified-name-space-unstripped | `test_render_names.py::test_a_path_with_spaces_renders_without_the_project_id` | 1 failed: `tmp-claude-0-x-scratchpad-mixed.ünï cødé…` kept its prefix | fails on eb0902f, passes on branch |

On the branch every row's tests passed (34 runs, 0 failed). `status-doctor-no-graph-age`, the 35th finding, is out of scope (map.md). Ticket 14's c4 tests are not rows.

- CHANGELOG: `grep -c "" CHANGELOG.md` 3224 → 3309. `## [Unreleased]` now holds `### Fixed` naming T1 (blind-spot gaps), T2 (staleness), T3 (chain), T4 (server cache / reindexing flag), T5 (caller counts), T6 (`map --inject` creates AGENTS.md, refusal reporting, deploy doc), T7 (status 403, root-not-found, redaction, project-not-indexed), T8 (semantic failure record, doctor/status, search fallback), T9 (hints), T10 (reset, Ctrl-C, prompt, names with spaces, MCP wire bytes), and ticket 14 (`c4 --check` matches `[[layers.allow]]` against file-level import edges at any depth); `### Security` holds T6 and T7's leak.

**Acceptance run** (`.venv/bin/pytest plans/trust-fixes/acceptance -q -o addopts=`, not this ticket's Done when; run to see where the feature stands, daemon stopped): `5 failed, 70 passed in 378.52s`. The 5 failures are all in `test_tf_safe_for_destructive.py` and are not host problems:
- `test_callers_of_a_python_symbol_imported_as_an_alias_is_partial_with_aliased_import_gap`, `test_callers_of_a_ts_symbol_imported_as_an_alias_…`, `test_callers_of_a_ts_symbol_reexported_under_an_alias_…`: no `aliased-import-unbound` gap. In these fakes the aliasing file (`b.py`, `src/main.ts`, `src/lib/index.ts`) has no row in the answer; ticket 05's rule reads only the answer's row files and the target's file, so it never sees the alias. (The real 0.9.0 backend gives the importing file a module-scope USAGE row, which is why the unit tests and 05's live test pass.)
- `test_callers_of_a_method_whose_sibling_has_a_name_matched_call_…` and `…_protocol_member_has_a_name_matched_call_…`: `total_area`/`totalArea` not listed, no `dispatch-sibling-unbound`. Ticket 06 finds siblings through `_Dispatch` (the classes that define the method); the acceptance fake has no class nodes, only the `*.area` method nodes and the `suffix_match` edge.
- In all five the answer also carries `stale-index` ("the graph index was last written 59s before HEAD (a commit) last changed"): the fixture commits at the current time and then `fresh_index()` stamps the db 60 s in the past, and ticket 02's callers check reuses `graph_index_age`, which counts HEAD's commit time. That gap is what made the answers `partial`. I re-ran a scratch copy of the three acceptance files with the commit dated 200 s back (scratchpad only; the acceptance files are untouched): the same 5 still fail, now `confidence: complete` with no alias / sibling gap, so the staleness is not the cause.

**Decisions**
- Picked, per finding, the test(s) the owning ticket's Done when and Answer name as its regression test; where a finding has several cases (row 5, 12, 17), all the named cases are in the row, so a row is "fails" only when every listed test fails on eb0902f.
- Copied the branch's whole `tests/` into the worktree rather than only new files, because several findings' tests were added to existing files (test_hardening, test_rbac, test_mcp_server, test_reindexer, …) and the branch `conftest.py` is needed (its `graph_index_age` stub patches names that exist on eb0902f too, checked).
- CHANGELOG T6 has a Fixed bullet (the non-security parts: `map --inject` creates AGENTS.md, refusal exit code, deploy doc) as well as the Security bullet, because Done when asks Fixed to name T1-T10. The T1 bullet is worded to what was built (an alias in a file the answer reads; a sibling class or a TS interface), not the broader "anywhere" of the spec, given the acceptance results above.

**Shortcuts taken**: none.

**For later tickets**
- The 5 acceptance failures above need a new ticket (or reopened 05/06) if the conductor holds the spec's "imported as `h` somewhere" and "a same-named sibling method" to the acceptance fake's shape: the alias rule would have to search beyond the answer's own files (the fake answers `search_code`), and the sibling rule would have to find `*.m` methods without class nodes (the fake answers `MATCH (n:Method) WHERE n.name=…`). Separately, whether `fresh_index()` after a fresh commit should read as stale for `callers` (HEAD commit time, ticket 01/02) is a spec question: the spec's callers story names only files, the `changed <ref>` story names HEAD's commit time.
- `codeintel c4 . --check` needs the backend on PATH here: `PATH=/root/.cache/codebase-memory-mcp/0.10.8:$PATH`.

## Review findings (round 1, checkpoint, done when)

its Done when checks failed at the checkpoint, with every ticket since the last one built.

$ .venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
  wanted: exit 0 and output containing 'passed'
  got: exit 1
  the last lines of its output:
src/codeintel/engines/graph/dispatch.py           819     24    97%   369, 418, 447, 658, 694, 853, 902, 1007, 1043, 1070, 1133, 1152, 1217, 1222, 1226, 1229-1230, 1325, 1396, 1450-1451, 1505, 1509, 1515
src/codeintel/engines/graph/ops.py                536     37    93%   114, 274, 352, 391-393, 395, 419, 431, 443, 463, 488, 512, 547, 631, 648, 650-651, 674, 781, 787, 789-790, 871, 888-889, 933, 951, 966, 971, 1001-1006, 1008-1009, 1066, 1097, 1147
src/codeintel/engines/graph/provider.py           457     23    95%   287, 331, 354, 483, 485, 696-697, 707-710, 717-718, 785, 812, 828-829, 893, 920-921, 1024-1025, 1226
src/codeintel/engines/graph/qualifier_scan.py     186      5    97%   164-165, 324, 386-387
src/codeintel/engines/graph/render.py              94      7    93%   59, 94, 117-118, 269, 281-282
src/codeintel/engines/graph/resolution.py         112     17    85%   60-61, 160-161, 164, 170, 173, 191, 231, 259, 264-274
src/codeintel/engines/graph/symbol_diff.py        398     26    93%   194-197, 232, 387, 389, 398, 428-429, 522, 545, 553, 560, 577-578, 581-582, 612-614, 625-626, 668-669, 675
src/codeintel/engines/graph/targets.py             60      5    92%   120, 123-124, 137-138
src/codeintel/engines/graph/wire_text.py          186      6    97%   93, 228, 253, 277, 375-376
src/codeintel/engines/lsp/lang_config.py          145     28    81%   97-98, 109-112, 171, 186, 213-216, 220-221, 223, 238-248, 258-260, 281
src/codeintel/engines/lsp/provider.py             624     77    88%   187, 190-191, 301-302, 315-321, 326-327, 406, 513-515, 562-564, 615-617, 645, 653-656, 677, 682-684, 686, 689, 724-728, 761-767, 837-838, 942-944, 974-989, 1008, 1027-1028, 1047, 1050, 1053-1054, 1061, 1089, 1092, 1095, 1223-1226, 1280, 1286, 1344, 1352, 1354-1356
src/codeintel/engines/semantic/db.py              235     37    84%   198-199, 204-205, 208-209, 269, 275, 277, 294, 360-361, 368-369, 377-382, 444-449, 464-465, 503-505, 538-539, 544-546, 558-559, 571-572, 602-603
src/codeintel/engines/semantic/indexer.py         481     68    86%   179-180, 189-190, 201-202, 312-320, 342-361, 365-366, 370, 391-392, 415, 469, 491, 513-514, 554-555, 563, 605, 640-641, 665, 723-724, 778-779, 782, 800-802, 805-806, 832-833, 864-869, 897-898, 924-926, 931, 938, 961-962, 966-967, 977
src/codeintel/engines/semantic/provider.py        420     33    92%   21-22, 60-61, 192-195, 222, 225, 264-266, 277, 279-280, 411, 418-420, 425-426, 566, 675-676, 684-688, 703, 705, 1006-1010, 1090, 1098
src/codeintel/engines/semantic/searcher.py        223     16    93%   113, 140-142, 164-166, 203-205, 365, 423-425, 448-449
src/codeintel/gateway/auth.py                      87      2    98%   137, 148
src/codeintel/gateway/cache.py                     54      2    96%   32-33
src/codeintel/gateway/factory.py                   31      6    81%   27-28, 33-34, 47-48
src/codeintel/gateway/gateway.py                  380     40    89%   73, 142-144, 176, 202, 212, 216, 234-236, 318-319, 331, 356-364, 501-503, 532, 586, 666, 676-677, 696-697, 721-723, 819, 829, 837, 966-968
src/codeintel/gateway/policy.py                    38      3    92%   14-15, 92
src/codeintel/gateway/reindexer.py                181      9    95%   57, 68-69, 101, 172, 194, 197, 225-226
src/codeintel/mapping/grapher.py                   74      7    91%   29-30, 33-34, 47, 129-130
src/codeintel/mapping/mapper.py                   173     19    89%   133, 136, 141-142, 172, 182, 209-210, 239-240, 248-250, 262-263, 323, 379, 395-397
src/codeintel/setup/agent_prompt.py                60      3    95%   107, 140-141
src/codeintel/setup/doctor.py                     247     39    84%   73, 94-97, 109-110, 119-120, 152, 160-161, 193, 197, 220-221, 245-248, 264-265, 275, 285-286, 317-318, 326-327, 336-337, 345-346, 356-357, 453-455, 514-517
src/codeintel/setup/injector.py                   112     17    85%   124, 138-140, 175, 178-180, 190-192, 201-203, 235, 246, 271
src/codeintel/setup/installer.py                  309     26    92%   54-55, 59-60, 156, 183-184, 215, 218, 228-229, 252, 263-264, 286, 308-309, 380-381, 454, 483, 514, 527, 544, 548-549
src/codeintel/setup/onboarding.py                 190     48    75%   36-40, 52, 55-58, 67-68, 91-92, 104, 143, 147-148, 156-158, 182-183, 186-195, 207-208, 229, 232-233, 238-239, 241-242, 270-271, 302-303, 316-317
src/codeintel/setup/reset.py                      162      4    98%   145-146, 174-175
src/codeintel/setup/verify.py                     179     25    86%   38-39, 61, 66-67, 80-81, 92, 161-162, 174-175, 180-181, 193, 207, 212, 215, 261-262, 289, 325, 335, 360-361
src/codeintel/transports/http.py                  232     48    79%   57-58, 63-64, 66-69, 116-117, 134-135, 189-191, 207-209, 246-247, 254-255, 269, 319-356
src/codeintel/transports/mcp.py                   228     18    92%   90-91, 212-213, 307, 326-329, 354-355, 391-392, 444-445, 602, 612, 627
-----------------------------------------------------------------------------
TOTAL                                           12921   1143    91%

28 files skipped due to complete coverage.
Required test coverage of 83% reached. Total coverage: 91.15%
=========================== short test summary info ============================
FAILED tests/test_graph_real.py::test_live_callers_of_safe_null_result - Asse...
1 failed, 2470 passed, 31 skipped, 1 deselected, 1 xfailed, 1 warning in 469.78s (0:07:49)
