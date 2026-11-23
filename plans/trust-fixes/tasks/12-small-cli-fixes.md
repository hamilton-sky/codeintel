# Fix reset help with --semantic-only, Ctrl-C exit 130, agent prompt steps, and names with spaces

Type: task
Status: resolved
Blocked by: 01, 08, 10
Test first: yes
Level: 3
Touches: src/codeintel/__main__.py, src/codeintel/cli/reset.py, src/codeintel/setup/reset.py, src/codeintel/engines/semantic/provider.py, src/codeintel/setup/agent_prompt.py, src/codeintel/engines/graph/render.py, tests/test_cli_help.py, tests/test_cli_commands.py, tests/test_reset.py, tests/test_agent_prompt.py, tests/test_render_names.py

These are the rest of T10. Each one is small, and each gets its regression test from its finding.

It is level 3 because `--semantic-only` changes which indexes `setup.reset.run_reset` deletes: data that could be lost. The default (no flag) must still delete both, exactly the files it deletes today, and `--semantic-only` must delete no graph file (`.db`, `-wal`, `-shm`, `.corrupt`). It is blocked by 01, which moves the graph slug out of `src/codeintel/setup/reset.py`; by 08, which also changes `tests/test_cli_commands.py`; and by 10, which also changes `src/codeintel/engines/semantic/provider.py`.

1. **`reset-help-understates-graph-wipe`.**
   - The `reset` entry in `_DESCRIPTIONS` in `__main__.py` reads "Clear the semantic and graph indexes (recover from a corrupt or stale DB)".
   - Add `--semantic-only` to the reset parser, and pass it through `src/codeintel/cli/reset.py` to `setup.reset.run_reset`, which then skips `_reset_graph_project`.
   - `run_reset` takes it as a keyword with default `False`: `tests/test_model_dimension.py` calls `run_reset(root, apply=True)` and `run_reset(root, apply=True, db_path=p)` and passes unchanged. `src/codeintel/cli/reset.py` reads it with `getattr(args, "semantic_only", False)`, because `tests/test_cli_commands.py::_reset_args` builds a namespace without it.
   - `reset._reset_all` holds a registered census tally (`tests/test_summary_integrity.py`); any new count text goes in that function or carries no count.
   - The corrupt-DB remediation strings in `src/codeintel/engines/semantic/provider.py` (`codeintel reset X && codeintel index X`) become `codeintel reset --semantic-only X && codeintel index X`.
2. **`ctrl-c-traceback`.** In `__main__.main()`, wrap the command dispatch in `except KeyboardInterrupt`: print `interrupted` to stderr and `sys.exit(130)`. `cli/_common.never_raise` already lets KeyboardInterrupt through, so nothing else changes.
3. **`prompt-reinstalls-for-model-download`.** In `setup/agent_prompt.build_prompt`, drive the steps from each engine's doctor `reason` and remediation, not `status != "ok"`:
   - an installed package with a failed model download gives the `FASTEMBED_CACHE_PATH`/network step and no `pip install`
   - an lsp `warn`/unverified row is "unverified", not missing
   - the tool list includes `code.status`
4. **`qualified-name-space-unstripped`.** In `engines/graph/render._strip_project_prefix`, stop returning a name unstripped just because it contains a space when the head is a project slug. Strip by the slug shape of the head (the part before the first `.` that contains `-`), keeping the filename and extension guards. `EC-1.1: Empty workflow plan` (a heading whose head has no dot before the space) must still be left alone; the test pins both. The existing strip tests pass unchanged: `tests/test_graph_provider.py` (kebab-case filenames such as `use-toast.ts`, `1731900000000-CreateInitialTables.ts`, `EC-1.1: Empty workflow plan`, real module paths, real slugs) and `tests/test_incompleteness.py` (`my-repo.Execute`, `Users-alice-work-cobra.ExecuteC`).

## Not in this ticket

- `map --inject` creating AGENTS.md and `docs/deploy.md` are ticket 08.
- The MCP wire-byte bound belongs to the transport hardening ticket.

## Done when

- These tests fail on main and pass here:
  - `tests/test_cli_help.py`: reset help names both indexes
  - `tests/test_reset.py`: `--semantic-only` leaves the graph db and its `-wal`/`-shm` files in place and clears the semantic index; plain `reset` still removes both, as the existing tests pin
  - `tests/test_cli_commands.py`: a command whose `run` raises KeyboardInterrupt makes `main()` exit 130 and print `interrupted`, with no traceback
  - `tests/test_agent_prompt.py`: a doctor report with semantic `index-failed` (model download) produces no `pip install` line, and the prompt lists `code.status`
  - new `tests/test_render_names.py`: `tmp-claude-0-x-scratchpad-mixed.ünï cødé.日本.uni.unicaller` renders as `ünï cødé.日本.uni.unicaller`, and `EC-1.1: Empty workflow plan` is unchanged
- `.venv/bin/pytest tests/test_cli_help.py tests/test_cli_commands.py tests/test_reset.py tests/test_agent_prompt.py tests/test_render_names.py tests/test_graph_provider.py tests/test_incompleteness.py tests/test_model_dimension.py tests/test_uninstall.py tests/test_semantic_provider.py tests/test_onboarding.py tests/test_summary_integrity.py -q -o addopts=` passes, and all existing tests still pass. The files after the new `tests/test_render_names.py` are not on Touches and pass unchanged. In `tests/test_agent_prompt.py`, the existing tests keep their assertions: a row with no doctor `reason` keeps today's status-based step (`test_only_the_missing_engine_is_named_to_install`).

```check
$ .venv/bin/pytest tests/test_cli_help.py tests/test_cli_commands.py tests/test_reset.py tests/test_agent_prompt.py tests/test_render_names.py tests/test_graph_provider.py tests/test_incompleteness.py tests/test_model_dimension.py tests/test_uninstall.py tests/test_semantic_provider.py tests/test_onboarding.py tests/test_summary_integrity.py -q -o addopts=
prints passed
```

## Reference

- spec.md, the story "Small fixes", and Decisions ("Ctrl-C")
- redblue-fixplan.md § T10

## Answer

Work started at commit `8e2050284c293baec80b12d328faea3384bba224`. Test-only commit: `e18d9bf` (`test(trust-fixes): 12 failing test`).

- **Built**:
  - `src/codeintel/__main__.py`: reset description names both indexes; `--semantic-only` on the reset parser (plus one epilog example); `main()` wraps the command dispatch in `except KeyboardInterrupt`, prints `interrupted` to stderr and exits 130 (through `_native_safe_exit` for the native-runtime commands, as the normal exit path already does, `sys.exit` otherwise).
  - `src/codeintel/cli/reset.py`: reads `getattr(args, "semantic_only", False)` and passes it to both `run_reset` calls (preview and apply).
  - `src/codeintel/setup/reset.py`: `run_reset(..., semantic_only=False)` keyword; scoped mode skips `_reset_graph_project` (graph_found/graph_removed 0, detail adds "; the graph index was kept"), `--all` mode skips `_reset_graph_cache` (detail adds "; the graph indexes were kept"). No new count text.
  - `src/codeintel/engines/semantic/provider.py`: both corrupt-DB remediations are `codeintel reset --semantic-only X && codeintel index X`.
  - `src/codeintel/setup/agent_prompt.py`: steps driven by the doctor rows; `code.status` in the tool list.
  - `src/codeintel/engines/graph/render.py`: `_strip_project_prefix` strips by slug shape (`_SLUG_SHAPE`) and refuses only a space in the last segment.
  - Tests: `tests/test_cli_help.py`, `tests/test_cli_commands.py`, `tests/test_reset.py`, `tests/test_agent_prompt.py`, new `tests/test_render_names.py`.
- **Proof**:
  - Failing on main: at `e18d9bf` (tests only) the new tests failed: `test_reset_help_names_both_indexes_and_offers_semantic_only`, `test_reset_passes_semantic_only_through`, `test_reset_without_the_flag_still_clears_both`, the Ctrl-C test (KeyboardInterrupt escaped `main()` and aborted the pytest session), the two `--semantic-only` tests in test_reset.py (`TypeError: run_reset() got an unexpected keyword argument 'semantic_only'`), the three new agent_prompt tests, and `test_a_path_with_spaces_renders_without_the_project_id` (`'tmp-claude-0...uni.unicaller' == 'ünï cødé.日本.uni.unicaller'`). `test_plain_reset_still_removes_the_graph_index_with_its_siblings` and the EC-1.1 test pass on both sides (they pin unchanged behaviour).
  - The check block, run after the last edit: `.venv/bin/pytest tests/test_cli_help.py ... tests/test_summary_integrity.py -q -o addopts=` → `434 passed, 2 skipped, 1 xfailed` (both skips pre-existing: test_reset.py:706 root can delete in read-only dir; test_onboarding.py:52 model weights not cached). Files after test_render_names.py unchanged.
  - Neighbouring tests: test_architecture, test_corpus, test_docs_trust_claims, test_gateway_freshness, test_graph_failure_population, test_graph_real, test_mapper, test_mcp_answer_size, test_doctor (with commands.md's deselect) → `189 passed, 26 skipped`. `ruff check src tests bench` clean, `mypy` "no issues found in 88 source files", compileall exit 0.
  - "all existing tests still pass": whole Test command left to the conductor's gate.
  - Acceptance `test_tf_small_fixes.py` (backend on PATH, HF_HUB_OFFLINE=1): 7 passed, including reset help, `--semantic-only` keeps the graph, doctor's corrupt-DB fix, Ctrl-C under the index lock (exit 130, one line), and the path-with-spaces render. 1 failed: `test_prompt_does_not_reinstall_when_only_the_model_download_failed` fails at its precondition `graph status == ok`: the test indexes under a temp HOME, which the running long-lived codebase-memory-mcp daemon refuses (learnings (05)), so the graph is never indexed. It also needs `cli/index.py` to write the model-failure record (ticket 16 per map 10). Not this ticket's code; the prompt logic itself is pinned by the unit tests.
- **Decisions**:
  - The ticket's literal description "Clear the semantic and graph indexes (recover from a corrupt or stale DB)" breaks `test_no_help_line_exceeds_the_width_budget` (89 > 78 columns). Used "Clear the semantic and graph indexes (corrupt or stale DB)", which keeps both index names in the description line.
  - `--all --semantic-only` also keeps every graph file: the ticket's rule "`--semantic-only` must delete no graph file" applies to both modes.
  - Ctrl-C on a native-runtime command (`index`, `query`, ...) exits via `_native_safe_exit(130)` (flush + `os._exit`), like that command's normal exit, so native threads cannot hang shutdown after the interrupt.
  - Prompt: a semantic row with `reason` `model-load-failed` (or `index-failed`) gives a network/`FASTEMBED_CACHE_PATH` step (taken from doctor's remediation, before its "; then:") and no `pip install`. A row with `installed: True` is never told to `pip install` (doctor says the package is there); a row without `installed`/`reason` keeps today's status-based step, so `test_only_the_missing_engine_is_named_to_install` passes unchanged. An lsp row that is `warn`, installed, `runnable: None` is "unverified": no setup step on its account, and a sentence saying `doctor --deep` boots it (also in the healthy branch).
  - Render: the head must fullmatch `[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+` (the `graph_slug` shape), and the name is left alone if its LAST segment contains a space (a symbol never does; a directory can). `EC-1.1: Empty workflow plan` stays because its last segment is `1: Empty workflow plan`.
- **Shortcuts taken**: a doc heading under a real slug whose last segment has a space (`my-repo.docs.Getting started`) is still left unstripped; telling it apart from `EC-1.1: …` would need the project id itself, which `_strip_project_prefix` does not get. Worth upgrading if headings start showing up in graph rows.
- **For later tickets**: ticket 16 (index writes the model-failure record) is what lets the `codeintel prompt` acceptance test see `reason: model-load-failed` end to end; that test also needs a graph index under its temp HOME, which fails while the long-lived backend daemon runs.
