# F4 — Unified gateway

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

Engine selector (graph|lsp|semantic|both|all|auto), fan-out+merge, content-hash cache, auto-detect, optional role tiering.

## Depends on

- `02-graph-engine`
- `03-lsp-engine`

## Scope — what this feature builds

One entry point routes ops across engines. `both`=graph+lsp, `all`=+semantic, `auto`=best-available-for-op. Content-hash cache; auto-backend detection; role/op tiering OFF by default (a harness can enable it).

## Acceptance criteria

- One endpoint serves all engines; engine=both merges graph+lsp.
- An unchanged file is served from cache; an edit busts it.
- The tiering toggle works and defaults to permissive (all ops, all callers).

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
