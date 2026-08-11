# F6 — Freshness / reindex seam

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

Debounced fire-and-forget incremental reindex (graph + semantic), fired on-demand; reindex config.

## Depends on

- `04-unified-gateway`
- `05-semantic-engine`

## Scope — what this feature builds

A `maybe_reindex(project_root)` fired on-demand from queries, debounced (<= once/window), off-thread. Reindexes changed files only (both indexes). `reindex` config gates it.

## Acceptance criteria

- Editing a file + re-querying reflects the change within one debounce window.
- Reindex runs off-thread and never blocks a response.
- reindex=off disables it entirely.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
