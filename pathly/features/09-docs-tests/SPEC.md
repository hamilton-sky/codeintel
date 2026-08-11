# F9 — Docs + test suite

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

README (agent-first) + per-engine docs + never-raise invariant suite + indexer incrementality + e2e smoke.

## Depends on

- `02-graph-engine`
- `03-lsp-engine`
- `05-semantic-engine`
- `07-install-ux`

## Scope — what this feature builds

Ship it: an agent-first README, per-engine docs, and CI covering the never-raise invariant, indexer incrementality, role/tier gating, and a ranked-result e2e smoke on a real repo.

## Acceptance criteria

- A new user can install + run a query in <5 min from the README.
- CI runs the safety + incremental + ranked-result suites green.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
