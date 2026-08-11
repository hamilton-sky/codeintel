# F2 — Graph engine adapter

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

GraphProvider wrapping the code-graph backend: impact/callers/callees/chain/pattern/overview.

## Depends on

- `01-mcp-skeleton`

## Scope — what this feature builds

`GraphProvider` shells out to / bridges the code-graph backend (tree-sitter whole-repo index). Auto-detects if the backend is installed; null + reason='engine-unavailable' if not.

## Acceptance criteria

- On an indexed repo, op=impact returns real caller/callee data.
- Missing backend → safe-null with a `reason`, never a crash.
- Every query is deadline-bounded; a wedged subprocess can't block a response.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
