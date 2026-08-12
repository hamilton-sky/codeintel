# Changelog

All notable changes to codeintel are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-08-12

First public release. Distributed on PyPI as **`codeintel-mcp`** (the import package and CLI
remain `codeintel`). Verified end-to-end on real Python **and** TypeScript repositories.

### Added
- **`codeintel doctor`** — preflight health check (CLI + `code.doctor` MCP tool + `POST /code/doctor`).
  Reports, per engine, installed / runnable / repo-indexed with a one-line fix for each gap;
  `--deep` boot-checks serena; `--json` for scripting; exits non-zero when unhealthy.
- **`codeintel setup`** — onboarding: checks backends, prints exact install steps, opt-in
  `--install-uv` / `--install-deps`, `--index`, `--warm`, ending with a health report.
- **`codeintel reset`** — clear a corrupt/stale semantic index (this repo, or `--all`); safe on a
  corrupt DB; confirms unless `--yes`.
- **`code.doctor` MCP tool** and **`POST /code/doctor`** HTTP endpoint.
- A dependency-free terminal output system: color (respects `NO_COLOR` / `--no-color` / non-TTY),
  ASCII fallback (`--ascii`), consistent across `doctor` / `status` / `query` / `setup`.
- Actionable `hint`s on the two "repo not indexed" safe-null reasons.
- Extensive real-boundary tests (live subprocess/DB, captured real backend responses).

### Fixed
- **graph engine** now reads the real `query_graph` `{columns, rows}` shape and traverses the
  real call edges (`CALLS` + `USAGE`); `callers`/`callees`/`impact`/`chain`/`pattern`/`overview`
  return real data (previously silently empty). Project resolution prefers an exact root match.
- **LSP engine** now launches serena correctly
  (`uvx --from git+https://github.com/oraios/serena serena start-mcp-server …`; the old
  `uvx serena` never worked) and uses serena's real tool contract (two-step reference lookup).
- **`code.map`** ranked-symbols / entry-points now populate from the real backend shape.
- **HTTP hardening**: non-loopback bind refused unless `--allow-remote` (loopback detected via
  `ipaddress`, not a spoofable string prefix); 1 MiB request-body cap → HTTP 413.
- **Reindex no longer hangs the CLI**: background reindex runs on daemon threads, so a first
  query on a large repo returns immediately instead of freezing until the repo-wide index finishes.
- Graph backend calls migrated off the **deprecated raw-JSON CLI args** to piped stdin (with a
  one-release fallback) and a shared timeout deadline.
- `impact` no longer emits a redundant double header.

### Changed
- `op=context` is now implemented as a real fan-out across all engines (graph→impact, lsp→symbol,
  semantic→search).
- Version is single-sourced from `codeintel.__version__`.

### Notes
- The graph engine requires the external `codebase-memory-mcp` binary; the LSP engine fetches
  serena via `uvx` on first use. `pip install codeintel-mcp` gives you the semantic engine
  out of the box — run `codeintel doctor` to see what else is available. The unrelated `codeintel`
  package on PyPI is a different project; install `codeintel-mcp`, and avoid installing both.

[0.2.0]: https://github.com/hamilton-sky/codeintel/releases/tag/v0.2.0
