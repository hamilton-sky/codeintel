# codeintel

A unified code-intelligence gateway — graph + LSP + semantic — that gives any coding agent a single safe API to search, trace, and understand any codebase.

[![CI](https://github.com/shammaihamilton/codeintel/actions/workflows/ci.yml/badge.svg)](https://github.com/shammaihamilton/codeintel/actions/workflows/ci.yml)

## Quickstart

```bash
git clone https://github.com/shammaihamilton/codeintel.git
cd codeintel
pip install -e .
```

Register with your AI agent(s):

```bash
codeintel install            # registers with Claude, Codex, Gemini, Zed
```

Index a project and run your first query:

```bash
codeintel index /path/to/your/project
codeintel query --op search --target "authentication middleware"
```

## How it works

A `Gateway` receives every query and dispatches it to one of three providers — graph (structural relationships), LSP (precise symbol resolution), or semantic (embedding-based search) — based on the operation type. Each provider is fully isolated: if it is unavailable or raises an exception, the gateway catches it and returns a safe-null envelope. The caller always gets a well-formed response with no exception to catch.

## Safe-null contract

Every `Gateway.query()` call returns a dict with exactly these keys:

```json
{"ok": true, "op": "search", "target": "auth", "result": null, "engine": "semantic", "cached": false}
```

`ok` is always `true`. `result` is `null` when no provider has an answer — never an exception, never a 500. An optional `reason` key explains null results (e.g. `"engine-unavailable"`, `"no-result"`). Callers must check `result is not None` before using the value.

## Engines

| Engine | Key ops | Install prereq |
|---|---|---|
| `graph` | `impact`, `callers`, `callees`, `chain`, `pattern`, `overview` | `codebase-memory-mcp` CLI on PATH — see [docs/graph.md](docs/graph.md) |
| `lsp` | `symbol`, `context` | Language server on PATH (e.g. `pyright`) — see [docs/lsp.md](docs/lsp.md) |
| `semantic` | `search` | `fastembed` + `sqlite-vec` (installed with the package) — see [docs/semantic.md](docs/semantic.md) |

Pass `--engine auto` (the default) and codeintel chooses the best engine per operation. Pass `--engine both` or `--engine all` to fan out to multiple engines and merge results.

## CLI reference

| Command | Purpose |
|---|---|
| `codeintel install [--agent claude\|codex\|gemini\|zed\|all]` | Register codeintel with AI agent(s) |
| `codeintel index [project_root]` | Index a project for semantic search |
| `codeintel serve` | Start the MCP server (stdio transport) |
| `codeintel serve-http [--host HOST] [--port 8766]` | Start the HTTP transport server |
| `codeintel query --op OP --target TARGET [--engine auto]` | Run a single query and print the result |
| `codeintel status [project_root]` | Show engine availability and index age |

## Config

Create `.codeintel.toml` at your project root to override defaults:

```toml
backend      = "auto"                   # auto | graph | lsp | semantic
semantic     = "on"                     # on | off
reindex      = "on-demand"              # on-demand | never
cosine_floor = 0.3                      # minimum similarity score for semantic hits
max_chunks   = 500                      # max chunks to embed per project
model        = "BAAI/bge-small-en-v1.5" # fastembed embedding model
```

## For agents

Start the HTTP server, then POST queries to `/code/query`:

```bash
codeintel serve-http &   # listens on 127.0.0.1:8766 by default
```

```python
import urllib.request, json

def code_query(op: str, target: str, engine: str = "auto") -> dict:
    body = json.dumps({"op": op, "target": target, "engine": engine}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8766/code/query",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

result = code_query("search", "authentication middleware")
if result["result"] is not None:
    print(result["result"])   # ranked semantic matches
```

The response is always JSON-safe. Check `result["result"] is not None` before use. Never catch an exception from the gateway — it never raises.

## Development

```bash
git clone https://github.com/shammaihamilton/codeintel.git
cd codeintel
pip install -e .[dev]
pytest tests/ -q            # full suite, ~1s
```
