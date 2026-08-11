# F1 — Skeleton + code.query contract (safe-off)

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

Boot the MCP server + code.query/code.status tools with a safe-null NoneProvider.

## Depends on

- (none — this is a root feature)

## Scope — what this feature builds

MCP server boots; `code.query` + `code.status` tools exist; the `CodeProvider` protocol + a `NoneProvider` that always returns a safe-null envelope. No real engine yet.

## Acceptance criteria

- `code.query` returns a well-formed safe-null envelope for ANY input.
- The server registers as an MCP tool a host (Claude/Codex) can call.
- The `{ok, op, target, result, engine, cached, reason?}` contract is documented.
- Never raises — a fault-injection test over every code path passes.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
