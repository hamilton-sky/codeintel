# User Stories — codeintel install/index/query/status CLI

## S1 — Config file loading

**As a** developer using codeintel in a project,  
**I want** a `.codeintel.toml` at the repo root to override `~/.codeintel/config.toml` defaults,  
**So that** per-project settings (backend, semantic on/off, reindex policy) take effect automatically.

**Acceptance criteria:**
- `load_config(".")` reads `.codeintel.toml` if present, otherwise reads `~/.codeintel/config.toml`, otherwise uses built-in defaults.
- Missing config files do not raise — returns defaults silently.
- A key present in project config overrides the same key in global config.
- All config values have documented defaults (backend=auto, semantic=on, reindex=on-demand).

---

## S2 — Index command

**As a** developer or agent,  
**I want** to run `codeintel index [project_root]` to build or refresh the semantic and graph indexes,  
**So that** I can guarantee freshness before a query burst.

**Acceptance criteria:**
- `codeintel index .` builds the semantic index for the current directory.
- `codeintel index /path/to/project` indexes that path.
- With no argument, defaults to the current working directory.
- Prints a summary: files walked, chunks embedded, time taken.
- On repeated calls with no file changes, prints "nothing to re-index" (incremental).
- Non-fatal errors (unreadable file) are logged but do not abort the command.
- Returns exit code 0 on success, non-zero only on a fatal configuration error.

---

## S3 — Query command

**As a** developer or script,  
**I want** to run `codeintel query --op <op> --target <target> [--engine <engine>] [--project-root <path>]` from the CLI,  
**So that** I can access code intelligence without an agent host.

**Acceptance criteria:**
- Required flags: `--op` and `--target`.
- Optional flags: `--engine` (default: auto), `--project-root` (default: cwd).
- Output is a human-readable block of the query result, or "no result" on safe-null.
- Never raises an uncaught exception; engine errors print a friendly message and exit 0.
- `codeintel query --op search --target "where is auth handled"` returns a ranked result when the semantic index is populated.

---

## S4 — Status command

**As a** developer or agent,  
**I want** to run `codeintel status` and see which engines are available and whether the index is fresh,  
**So that** I know whether to reindex before querying.

**Acceptance criteria:**
- Prints which engines (graph/lsp/semantic) are available on this machine.
- Prints whether the semantic index exists and its approximate age.
- Prints the embedding model name when semantic is available.
- Never raises; missing engines print as "unavailable".
- Exit code 0 always.

---

## S5 — Install: Claude

**As a** developer,  
**I want** to run `codeintel install --agent claude` to register the MCP server into Claude Code's settings,  
**So that** Claude can call `code.query` without any manual config editing.

**Acceptance criteria:**
- Writes `mcpServers.codeintel` into `~/.claude/settings.json` (creates file if absent).
- Entry: `{"command": "codeintel", "args": ["serve"]}`.
- Idempotent: running twice does not duplicate or corrupt the entry.
- Existing unrelated keys in the settings file are preserved.
- Prints `✓ claude: registered at ~/.claude/settings.json` on success.

---

## S6 — Install: Codex

**As a** developer,  
**I want** `codeintel install --agent codex` to register the MCP server into Codex CLI's config,  
**So that** Codex can call `code.query`.

**Acceptance criteria:**
- Writes `mcpServers.codeintel` into `~/.codex/config.json` (creates file if absent).
- Idempotent; existing keys preserved.
- Prints confirmation on success, or a skip message if already registered.

---

## S7 — Install: Gemini

**As a** developer,  
**I want** `codeintel install --agent gemini` to register the MCP server into Gemini Code's config,  
**So that** Gemini can call `code.query`.

**Acceptance criteria:**
- Writes `mcpServers.codeintel` into `~/.gemini/settings.json` (creates file if absent).
- Idempotent; existing keys preserved.

---

## S8 — Install: Zed

**As a** developer,  
**I want** `codeintel install --agent zed` to register the MCP server into Zed's context-servers config,  
**So that** Zed can call `code.query`.

**Acceptance criteria:**
- Writes into `~/.config/zed/settings.json` under `context_servers.codeintel`.
- Zed's format: `{"command": {"path": "codeintel", "args": ["serve"]}}`.
- Idempotent; existing keys preserved.

---

## S9 — Install: all agents

**As a** developer,  
**I want** `codeintel install --agent all` to register into every supported host in one command,  
**So that** I can set up once and use codeintel from any agent.

**Acceptance criteria:**
- Registers claude, codex, gemini, and zed in sequence.
- Prints one result line per agent (success, skipped-already-registered, or failed-with-reason).
- A failure on one agent does not abort registration of the others.
- Exit code 0 when at least one agent was registered; 1 only when all failed.
