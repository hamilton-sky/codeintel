# F7 — Install & UX

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

codeintel install/index/serve/query/status CLI + self-register into agent hosts + .codeintel.toml config.

## Depends on

- `04-unified-gateway`

## Scope — what this feature builds

`codeintel install [--agent claude|codex|gemini|zed|all]` self-registers the MCP server (idempotent). `index/serve/query/status` subcommands. `.codeintel.toml` (project) over `~/.codeintel/config.toml` (global). No external DB dependency; per-machine index cache.

## Acceptance criteria

- `codeintel install --agent all` makes the tool callable from each host.
- `codeintel index` builds both the graph + semantic indexes.
- The config file overrides defaults; install is idempotent.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
