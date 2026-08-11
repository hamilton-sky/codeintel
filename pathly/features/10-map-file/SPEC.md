# F10 — MD map-file mode (universal, zero-integration)

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

`codeintel map` generates a compressed, ranked `CODE_INTEL.md` from the graph index that ANY agent
reads for orientation — the universal, zero-integration layer that works even for hosts with no MCP.

## Depends on

- `02-graph-engine`
- `04-unified-gateway`

## Scope — what this feature builds

A `code.map` tool + `codeintel map <repo>` CLI that walks the graph index and writes a
size-bounded `CODE_INTEL.md`: repo/architecture overview, top modules, key symbols ranked by
fan-in (PageRank-style over the call graph, à la Aider's repo-map), entry points, and
"who-calls-what" highlights. Committable and refreshed on `index`. Optional `--inject` links or
appends it into the repo's existing agent-context file (`CLAUDE.md` / `AGENTS.md`). It is a STATIC
snapshot for orientation — it complements, never replaces, the live `code.query` tool.

## Acceptance criteria

- `codeintel map` writes a self-contained, human-readable `CODE_INTEL.md` with a ranked
  module/symbol overview, under a byte budget (documented; over-budget content is dropped, not
  silently truncated).
- Deterministic on an unchanged repo; updates when the graph changes.
- Consumable with NO tool installed — it's just markdown in the repo.
- `--inject` links/appends into `CLAUDE.md`/`AGENTS.md` idempotently (never duplicates its block).
- Never raises — a missing/empty graph index yields a minimal map with a note, not an error.

## Notes

- The map is generated from the **graph** index (breadth/ranking); semantic clustering of sections
  is a possible v2 enhancement, not required for v1.
- Honors the project-wide **never-raise / safe-null** contract and local-first (no network egress).
- Single-responsibility modules, ~<=400 lines/file.
