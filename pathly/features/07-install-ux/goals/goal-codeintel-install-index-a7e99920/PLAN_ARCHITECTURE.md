# Plan Architecture — codeintel install/index/query/status CLI

## Design decisions for F7

### Decision 1 — Config uses `tomllib` (Python 3.11+ built-in)

Python 3.11 ships `tomllib` in stdlib. The project already requires `>=3.11` (`pyproject.toml`). Using `tomllib` means zero new dependencies for config loading.

The config module is intentionally permissive: it merges and returns raw values without type coercion. Callers (gateway, CLI) apply their own validation. This keeps the config module under ~100 lines and single-responsibility.

### Decision 2 — Installer writes JSON directly (no library)

Each agent config file is JSON. The installer uses `json.load` / `json.dump` with a dict-merge approach. No dedicated library (e.g. `deepmerge`) is needed — the nesting is at most 2 levels deep.

Idempotency is achieved by comparing the existing nested value to the target value before writing. If they match, no write occurs and `action="already"` is returned.

### Decision 3 — Installer is a separate module (`installer.py`)

Self-registration logic is isolated from the CLI dispatch (`__main__.py`) and the query engine. This lets tests exercise registration without spawning a subprocess, and keeps `__main__.py` focused on subcommand wiring.

### Decision 4 — `index` CLI triggers semantic index only; graph reindex is best-effort

The `codeintel index` command builds the semantic index directly (via `Indexer`). Graph reindex is triggered via `Reindexer._graph_reindex()` as a best-effort shell call to `codebase-memory-mcp` if it is on PATH. If it isn't, that step is silently skipped — consistent with the never-raise contract.

### Decision 5 — Never-raise contract applies to CLI handlers

All subcommand handlers catch exceptions and print human-readable messages. `sys.exit(1)` is used only for configuration-level failures (e.g., DB creation fails). Query and status always exit 0 — a safe-null result is a valid answer, not an error.

---

## Phase Mapping

### Phase 1 — Config module (`src/codeintel/config.py`)

Standalone new file. No dependency on any other codeintel module at import time (only stdlib). This is the foundation layer — all later phases import `load_config`.

The config dict is the single authoritative source of runtime settings. Callers read keys they need and ignore the rest.

### Phase 2 — CLI core subcommands (`src/codeintel/__main__.py`)

Extends `__main__.py` to add `index`, `query`, `status`. The main architectural constraint: **imports are deferred inside each handler function** (using `from codeintel.xxx import yyy` inside the `if args.command == "..."` block). This keeps CLI startup fast and avoids import-time errors from optional dependencies (e.g. `fastembed`).

`server._build_gateway()` is reused for the `query` subcommand — no new gateway construction logic is needed.

### Phase 3 — Installer module (`src/codeintel/installer.py`)

New standalone module. No imports from other codeintel modules. The `AGENT_CONFIGS` mapping table drives all per-agent logic — adding a new agent is one entry in that dict.

Zed uses a different JSON key path (`context_servers` vs `mcpServers`) and a different value shape. The `_get_entry_for_agent` helper encapsulates this difference.

### Phase 4 — Install subcommand (`src/codeintel/__main__.py`)

Adds the `install` subcommand. Imports `Installer` inside the handler. Exit code logic: `sys.exit(0)` if `any(r["ok"] for r in results)`, else `sys.exit(1)`.
