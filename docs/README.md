# codeintel documentation

Start here. `codeintel` unifies three code-intelligence engines behind one safe tool; these docs
explain how the system is put together and how each piece behaves.

## Start here

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) | The layered design, the `CodeProvider` protocol, the safe-null contract, caching, freshness, transports. **Read this first.** |
| [install.md](install.md) | `codeintel install` — what each agent host actually reads, why the registered command is an absolute path, and the three levels of proof that it works. |
| [query-flow.md](query-flow.md) | The request lifecycle — engine selection, fan-out & merge, caching, and why the gateway never throws. Mermaid + ASCII. |
| [map-file.md](map-file.md) | The static `CODE_INTEL.md` orientation layer (`codeintel map`) — for hosts with no MCP support. |
| [graph-viewer.md](graph-viewer.md) | `codeintel graph` — the call graph as `{nodes,edges}` JSON, or a **self-contained interactive HTML viewer** for any repo (layouts, metrics, export). |
| [c4.md](c4.md) | `codeintel c4` — a **LikeC4 architecture model** (`.c4`) of the repo's files/directories and import graph: committable, diffable source rather than a rendered picture. |

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
- **"My agent doesn't see the tools."** → [install.md](install.md)
- **"I have no MCP host."** → [map-file.md](map-file.md)

See the top-level [README](../README.md) for install, CLI, config, and the agent HTTP snippet.
