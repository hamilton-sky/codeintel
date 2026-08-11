# Implementation Plan — codeintel install/index/query/status CLI

## Overview

3 conversations, 4 phases. Foundation first (config), then CLI commands (index/query/status), then self-registration (install). Each conversation leaves the codebase runnable.

Depends on: F4 (unified gateway), F5 (semantic engine), F6 (freshness/reindex) — all must be merged before starting Conv 1.

---

## Phase 1 — Config module

**Conversation:** 1  
**File:** `src/codeintel/config.py` (NEW)  
**Purpose:** Provide a config loading layer so all CLI subcommands can read `.codeintel.toml` (project-local) overriding `~/.codeintel/config.toml` (global) with documented defaults.  
**Depends on:** Python 3.11+ stdlib (`tomllib`, `pathlib`) only — no other codeintel modules.  
**Enables:** Phases 2, 3, 4 — every CLI subcommand reads config via `load_config`.

**Done when:** `python3 -c "from codeintel.config import load_config; c = load_config('.'); assert 'backend' in c"` exits 0.

**Builder prompt (Conv 1):**
```
Read FEATURE_INDEX.md in the goal folder first.

Create src/codeintel/config.py (new file, ~100 lines).

Implement load_config(project_root: str | None = None) -> dict:
- Try to read <project_root>/.codeintel.toml (or cwd if None) using tomllib.
- Try to read ~/.codeintel/config.toml as the global fallback.
- Merge: project values override global values, both override defaults.
- Never raise — missing files return defaults silently.

Defaults:
  backend = "auto"
  semantic = "on"
  reindex = "on-demand"
  window = 20
  stride = 10
  max_chunks = 500
  cosine_floor = 0.3
  model = "BAAI/bge-small-en-v1.5"

Return a plain dict. No dataclass (keeps it simple).

Do NOT touch any other file yet.

Verify: python3 -c "from codeintel.config import load_config; c = load_config('.'); assert 'backend' in c; print('ok', c)"

If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with git checkout on affected files and retry.
```

**Verify:** `python3 -c "from codeintel.config import load_config; c = load_config('.'); assert 'backend' in c; print(c)"`

---

## Phase 2 — CLI: index / query / status subcommands

**Conversation:** 2  
**File:** `src/codeintel/__main__.py` (EXTEND)  
**Purpose:** Add `index`, `query`, and `status` subcommands to the CLI, wired to the existing `Indexer`, `Gateway`, and `code_status_handler` machinery. Also let `server.py` optionally read config at startup.  
**Depends on:** Phase 1 (config.py); existing `Indexer` in `codeintel.indexer`, `Gateway` in `codeintel.gateway`, `code_status_handler` in `codeintel.server`, `SemanticDb` in `codeintel.semantic_db`.  
**Enables:** Phase 3–4 (install depends on a tested CLI baseline).

**Done when:** `codeintel status` prints engine availability; `codeintel index .` runs; `codeintel query --op search --target "index"` returns output (even safe-null).

**Builder prompt (Conv 2):**
```
Read FEATURE_INDEX.md in the goal folder first.
Conv 1 is complete: src/codeintel/config.py exists with load_config().

Extend src/codeintel/__main__.py. Keep the existing `serve` subcommand unchanged.

Add three subcommands:

1. `index [project_root]` (positional arg, default=cwd)
   - Load config via load_config(project_root).
   - Build SemanticDb at <project_root>/.codeintel/semantic.db, call db.init().
   - Run Indexer(db).index(project_root).
   - Also trigger Reindexer()._graph_reindex(project_root) if codebase-memory-mcp is on PATH.
   - Print: "Indexed <N> chunks in <project_root>" (or "Nothing new to index" if N==0).
   - On error: print the error message, exit 1.
   - Close the db in a finally block.

2. `query --op <op> --target <target> [--engine auto] [--project-root <cwd>]`
   - Build the gateway via server._build_gateway().
   - Call gateway.query(op=op, target=target, engine=engine, project_root=project_root).
   - If result["result"] is not None: print result["result"].
   - Else: print f"No result (reason: {result.get('reason','unknown')})".
   - Never raises uncaught; exit 0 always.

3. `status [project_root]` (positional, default=cwd)
   - Call server.code_status_handler({}).
   - Check if <project_root>/.codeintel/semantic.db exists; stat its mtime if so.
   - Print a human-readable block: engines available, index path+age, model.
   - Exit 0 always.

Do NOT add `install` yet — that is Conv 3.

Verify:
  codeintel status
  codeintel index .
  codeintel query --op search --target "index"

If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with git checkout on affected files and retry.
```

**Verify:** `codeintel status && codeintel index . && codeintel query --op search --target "embedding"`

---

## Phase 3 — Installer module

**Conversation:** 3  
**File:** `src/codeintel/installer.py` (NEW)  
**Purpose:** Encapsulate per-agent self-registration logic. Each agent has a distinct config file path and JSON shape; the installer does an idempotent JSON merge so existing keys are never overwritten.  
**Depends on:** stdlib only (`json`, `pathlib`, `shutil`). No other codeintel modules.  
**Enables:** Phase 4 (the `install` CLI subcommand calls this module).

**Done when:** `python3 -c "from codeintel.installer import Installer; r = Installer().register('claude'); print(r)"` prints a result dict with `ok=True` or `ok=False` and never raises.

**Builder prompt (Conv 3, part A):**
```
Read FEATURE_INDEX.md in the goal folder first.
Convs 1–2 are complete.

Create src/codeintel/installer.py (~180 lines). Implement class Installer with:

  register(agent: str) -> dict
    Returns {"agent": agent, "path": str, "ok": bool, "action": "registered"|"already"|"failed", "reason": str|None}
    Never raises.

  register_all() -> list[dict]
    Calls register() for each of ["claude", "codex", "gemini", "zed"], returns list.

Per-agent config targets and JSON shapes:

  claude:
    path: ~/.claude/settings.json
    merge key: mcpServers.codeintel
    value: {"command": "codeintel", "args": ["serve"]}

  codex:
    path: ~/.codex/config.json
    merge key: mcpServers.codeintel
    value: {"command": "codeintel", "args": ["serve"]}

  gemini:
    path: ~/.gemini/settings.json
    merge key: mcpServers.codeintel
    value: {"command": "codeintel", "args": ["serve"]}

  zed:
    path: ~/.config/zed/settings.json
    merge key: context_servers.codeintel
    value: {"command": {"path": "codeintel", "args": ["serve"]}}

Merge logic (idempotent):
  1. Load existing JSON if file exists (empty dict if not).
  2. Set the nested key (create intermediate dicts as needed).
  3. If the value already matches exactly, return action="already".
  4. Otherwise write back the merged JSON (indent=2) and return action="registered".
  5. Any exception → return action="failed", ok=False, reason=str(exc).
  6. Create parent dirs if they don't exist.

Do NOT touch __main__.py yet — that is Phase 4.

Verify:
  python3 -c "from codeintel.installer import Installer; r = Installer().register('claude'); print(r); assert r['ok']"

If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with git checkout on affected files and retry.
```

**Verify:** `python3 -c "from codeintel.installer import Installer; print(Installer().register('claude'))"`

---

## Phase 4 — Install subcommand

**Conversation:** 3  
**File:** `src/codeintel/__main__.py` (EXTEND)  
**Purpose:** Wire the `install` subcommand to `Installer`, completing the CLI surface.  
**Depends on:** Phase 3 (installer.py).  
**Enables:** The feature's top-level acceptance criterion — `codeintel install --agent all`.

**Done when:** `codeintel install --agent all` prints one result line per agent and exits 0.

**Builder prompt (Conv 3, part B):**
```
Read FEATURE_INDEX.md in the goal folder first.
Phase 3 is complete: src/codeintel/installer.py exists with Installer class.

Extend src/codeintel/__main__.py. Keep all existing subcommands (serve, index, query, status) unchanged.

Add one subcommand:

  install --agent <choice>
    choices: claude, codex, gemini, zed, all
    default: all

  Logic:
    If agent == "all": results = Installer().register_all()
    Else:              results = [Installer().register(agent)]

  Print per result:
    ✓ <agent>: registered at <path>
    ~ <agent>: already registered at <path>
    ✗ <agent>: failed — <reason>

  Exit code: 0 if any ok, else 1.

Do NOT touch other subcommands.

Verify:
  codeintel install --agent all

If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with git checkout on affected files and retry.
```

**Verify:** `codeintel install --agent all` exits 0 and writes `~/.claude/settings.json`
