# F8 — HTTP transport parity (optional)

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

POST /code/query mirroring the MCP contract for harness callers; never 500s on an engine miss.

## Depends on

- `04-unified-gateway`

## Scope — what this feature builds

An optional HTTP port exposing the SAME safe-null contract as the MCP tool, for harnesses (like Pathly) that prefer HTTP. Only a malformed REQUEST gets a 4xx.

## Acceptance criteria

- The HTTP envelope is byte-identical in shape to the MCP result.
- An engine miss returns ok:true + result:null, never a 500.
- A malformed request returns a clean 4xx.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
