# Happy Flow — codeintel install/index/query/status CLI

The ideal end-to-end journey for a developer setting up codeintel on a new machine.

---

## Phase 1 — Config reads cleanly

Developer has no `.codeintel.toml` yet. They import `load_config(".")`:

```
backend  = "auto"
semantic = "on"
reindex  = "on-demand"
window   = 20
stride   = 10
max_chunks = 500
cosine_floor = 0.3
model    = "BAAI/bge-small-en-v1.5"
```

All defaults are returned. No file is created. No error is raised.

Later the developer adds `.codeintel.toml` with `backend = "semantic"`. Next call returns `backend = "semantic"` and all other defaults unchanged.

---

## Phase 2 — First indexing session

Developer runs `codeintel index .` in a Python project root:

```
$ codeintel index .
Indexed 412 chunks in /home/alice/myproject  (3.1 s)
```

Running again immediately:
```
$ codeintel index .
Nothing new to index.
```

After editing one file and running again:
```
$ codeintel index .
Indexed 8 chunks in /home/alice/myproject  (0.2 s)
```

Developer runs `codeintel status`:
```
$ codeintel status
Engines:
  graph    : unavailable (codebase-memory-mcp not found)
  lsp      : unavailable (serena not found)
  semantic : available
Index:
  path  : /home/alice/myproject/.codeintel/semantic.db
  age   : 0 min
  model : BAAI/bge-small-en-v1.5
```

Developer runs a query:
```
$ codeintel query --op search --target "where is authentication handled"
## Code matches

src/auth/middleware.py:12
  def authenticate_request(request): ...

src/auth/tokens.py:5
  class TokenValidator: ...
```

---

## Phase 3 — Self-registration into all agent hosts

Developer runs `codeintel install --agent all`:

```
$ codeintel install --agent all
✓ claude  : registered at ~/.claude/settings.json
✓ codex   : registered at ~/.codex/config.json
✓ gemini  : registered at ~/.gemini/settings.json
✓ zed     : registered at ~/.config/zed/settings.json
```

Running again (idempotent):
```
$ codeintel install --agent all
~ claude  : already registered at ~/.claude/settings.json
~ codex   : already registered at ~/.codex/config.json
~ gemini  : already registered at ~/.gemini/settings.json
~ zed     : already registered at ~/.config/zed/settings.json
```

Claude Code now shows `codeintel` as an available MCP tool. The developer opens a file, asks Claude "who calls `parse_result`?" and Claude calls `code.query` with `op=callers, target=parse_result` — returning a real answer from the running MCP server.
