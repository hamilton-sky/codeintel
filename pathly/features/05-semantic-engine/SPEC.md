# F5 — Semantic engine (in-house)

> Part of the **codeintel** project. Full context: `pathly/project/SPEC.md`.

## Goal

Local embeddings + sqlite-vec code_embeddings index + op=search KNN with a cosine floor.

## Depends on

- `04-unified-gateway`

## Scope — what this feature builds

The differentiator, built fresh + local (no API keys). A `code_embeddings` vec0 table, an incremental content-hash-keyed indexer (line-window v1), and op=search returning ranked path:line + snippet, floored by a cosine ceiling so weak matches return empty not noise.

## Acceptance criteria

- op=search on a natural-language query returns relevant path:line matches on a real repo.
- Re-indexing an unchanged repo re-embeds nothing (all content_hash match).
- Empty index → safe-null; weak matches floored to empty; a chunk cap drop is logged.

## Notes

- Honors the project-wide **never-raise / safe-null** contract: any internal failure returns a
  null result, never an error.
- Local-first: no network egress, no API keys.
- Single-responsibility modules, ~<=400 lines/file.
