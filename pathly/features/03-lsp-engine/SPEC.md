# F3 — LSP engine adapter

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

LspProvider wrapping the LSP-over-MCP bridge: always-fresh symbol/overview, async warm-up.

## Depends on

- `01-mcp-skeleton`

## Scope — what this feature builds

`LspProvider` runs one long-lived language-server session per project root with async warm-up; first call returns null (warming) then fresh data. op=symbol / op=overview.

## Acceptance criteria

- After warm-up, op=symbol returns fresh definition/references.
- Boot failure → cooldown, no per-request respawn; never blocks the prompt.
- Switching project_root tears down the old session.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
