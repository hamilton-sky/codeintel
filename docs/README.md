# codeintel documentation

`codeintel` unifies three code-intelligence engines behind one tool that never raises. These docs
explain how the system is put together, how each engine behaves, and — where it has been measured —
how well.

Every doc is listed here. Anything not in this index does not exist; anything historical says so in a
banner at the top rather than quietly reading as current.

## Start here

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) | The layered design, the `CodeProvider` protocol, the safe-null contract, caching, freshness, transports. **Read this first.** |
| [query-flow.md](query-flow.md) | The request lifecycle — engine selection, fan-out & merge, caching, and why the gateway never throws. |
| [install.md](install.md) | `codeintel install` — what each agent host actually reads, why the registered command is an absolute path, and the three levels of proof that it works. |

## Engine references

| Engine | Doc | Backs |
|---|---|---|
| Graph (breadth) | [graph.md](graph.md) | `codebase-memory-mcp` — impact, callers/callees, chains, patterns, overview |
| LSP (precision/freshness) | [lsp.md](lsp.md) | a language server via `uvx`/`serena` — symbols, references |
| Semantic (meaning) | [semantic.md](semantic.md) | in-house `fastembed` + `sqlite-vec` — NL search |

Two things in the graph reference are worth reading even if you skip the rest, because they changed
what an answer *means*:
[relationship kind and edge provenance](graph.md#relationship-kind-and-how-an-edge-was-resolved),
and the [safe-null reasons](graph.md#safe-null-reasons) table — in particular `no-edges`, which is
not the same claim as `not-in-graph`.

## Measurement

| Doc | What it measures |
|---|---|
| [../bench/README.md](../bench/README.md) | **Call-edge accuracy** — precision/recall of `callers`/`impact` against labelled ground truth, per question, across graph vs LSP vs LSP-plus-syntax. Includes what the oracle proves is NOT a caller — without which a fabricated caller costs an engine nothing — and what it refuses to judge. |
| [benchmarks.md](benchmarks.md) | **Semantic engine** throughput and index size at scale. A different measurement with a different method. |

## Outputs other than `code.query`

| Doc | What it covers |
|---|---|
| [map-file.md](map-file.md) | The static `CODE_INTEL.md` orientation layer (`codeintel map`) — for hosts with no MCP support. |
| [graph-viewer.md](graph-viewer.md) | `codeintel graph` — the call graph as `{nodes,edges}` JSON, or a self-contained interactive HTML viewer. |
| [c4.md](c4.md) | `codeintel c4` — a LikeC4 architecture model (`.c4`) of the repo's files/directories and import graph. |

## Operating it

| Doc | What it covers |
|---|---|
| [deploy.md](deploy.md) | Running the HTTP transport — auth, RBAC, rate limits, metrics, container notes. |
| [providers-bringup.md](providers-bringup.md) | Getting all three engines from "installed" to actually answering, and the failure mode behind each safe-null. |
| [branch-protection.md](branch-protection.md) | The committed rulesets for `main` and the `v*` release tags, which CI checks gate a merge, and which two deliberately don't. |

## Design records and history

These are kept for the reasoning, not as descriptions of the current code. Each opens with a status
banner saying what still holds.

| Doc | Status |
|---|---|
| [adr/0001-graph-capability-unlock.md](adr/0001-graph-capability-unlock.md) | Accepted, partly superseded. Two ops it describes have changed: `deadcode` has since been **withdrawn** — it always safe-nulls with `reason: "op-withdrawn"` after a labelled corpus measured its precision at 25% ([graph.md](graph.md#deadcode-is-retired)) — and `chain`'s hop risk labels became resolution evidence. |
| [refactor-graph-provider.md](refactor-graph-provider.md) | Done — the seams it proposed exist, and `wire_text.py` was later added at one of them. |
| [layers-design.md](layers-design.md) | Design only; not implemented. |
| [roadmap-semantic.md](roadmap-semantic.md) | Forward-looking plan for the semantic engine. |
| [eval-2026-08-17.md](eval-2026-08-17.md) | Historical — adversarial evaluation at 0.15.3. |
| [eval-2026-08-17-fix-architecture.md](eval-2026-08-17-fix-architecture.md) | Historical — the fix plan that followed it. |
| [eval-2026-08-23-status-and-market.md](eval-2026-08-23-status-and-market.md) | Historical — status and market position at 0.15.5. |

## Mental model in one line

> One tool (`code.query`) → one gateway → three interchangeable providers behind one protocol, each
> of which **never raises** — so a missing engine degrades the agent to grep, never crashes.

## Reading order by goal

- **"How does the whole thing fit together?"** → [architecture.md](architecture.md)
- **"What happens when I call it?"** → [query-flow.md](query-flow.md)
- **"Why did I get `result: null`?"** → the *Safe-null reasons* table in the relevant engine doc.
  Start by checking whether it is `no-edges` rather than `not-in-graph` — those license opposite
  conclusions.
- **"Can I trust this answer?"** → the `confidence` and `gaps` fields, then
  [graph.md](graph.md#relationship-kind-and-how-an-edge-was-resolved), then
  [../bench/README.md](../bench/README.md) for what has actually been measured.
- **"My agent doesn't see the tools."** → [install.md](install.md)
- **"Graph ops return nothing."** → [providers-bringup.md](providers-bringup.md), then check the
  backend version — both `0.9.x` and `0.10.x` are supported, a third dialect is not.
- **"I have no MCP host."** → [map-file.md](map-file.md)

See the top-level [README](../README.md) for install, CLI, config, and the agent HTTP snippet.
