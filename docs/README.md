# codeintel documentation

Start here. `codeintel` unifies three code-intelligence engines behind one safe tool; these docs
explain how the system is put together and how each piece behaves.

## Start here

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) | The layered design, the `CodeProvider` protocol, the safe-null contract, caching, freshness, transports. **Read this first.** |
| [query-flow.md](query-flow.md) | The request lifecycle — engine selection, fan-out & merge, caching, and why the gateway never throws. Mermaid + ASCII. |
| [map-file.md](map-file.md) | The static `CODE_INTEL.md` orientation layer (`codeintel map`) — for hosts with no MCP support. |

## Engine references

| Engine | Doc | Backs |
|---|---|---|
| Graph (breadth) | [graph.md](graph.md) | `codebase-memory-mcp` — impact, callers/callees, chains, patterns, overview |
| LSP (precision/freshness) | [lsp.md](lsp.md) | a language server via `uvx`/`serena` — symbols, references |
| Semantic (meaning) | [semantic.md](semantic.md) | in-house `fastembed` + `sqlite-vec` — NL search |

## Mental model in one line

> One tool (`code.query`) → one gateway → three interchangeable providers behind one protocol,
> each of which **never raises** — so a missing engine degrades the agent to grep, never crashes.

## Reading order by goal

- **"How does the whole thing fit together?"** → [architecture.md](architecture.md)
- **"What happens when I call it?"** → [query-flow.md](query-flow.md)
- **"Why did I get `result: null`?"** → the *Safe-null reasons* table in the relevant engine doc.
- **"I have no MCP host."** → [map-file.md](map-file.md)

See the top-level [README](../README.md) for install, CLI, config, and the agent HTTP snippet.
