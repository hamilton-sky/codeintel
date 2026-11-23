# Fix reset help with --semantic-only, Ctrl-C exit 130, agent prompt steps, and names with spaces

Type: task
Status: open
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
