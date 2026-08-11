# codeintel — Feature Sequencing

The 10 features are named with `01`…`10` **build-order prefixes**, so the folder/board list sorts
in dependency order. The prefix matches the feature's F-number in `SPEC.md`, and the numeric order
`01 → 02 → … → 10` is a **valid topological order** — every feature's dependencies are lower-numbered.

## Canonical build order

```
01-mcp-skeleton        (root — the MCP server + code.query/code.status contract)
02-graph-engine        needs 01
03-lsp-engine          needs 01
04-unified-gateway     needs 02, 03
05-semantic-engine     needs 04
06-freshness-reindex   needs 04, 05
07-install-ux          needs 04
08-http-transport      needs 04
09-docs-tests          needs 02, 03, 05, 07   (ship gate)
10-map-file            needs 02, 04
```

## Waves (what can run in parallel)

If you parallelize instead of going one-at-a-time, these groups are safe to run together:

| Wave | Features | Depends on |
|---|---|---|
| **1** | `01-mcp-skeleton` | — |
| **2** | `02-graph-engine` · `03-lsp-engine` | 01 |
| **3** | `04-unified-gateway` | 02, 03 |
| **4** | `05-semantic-engine` · `07-install-ux` · `08-http-transport` · `10-map-file` | 04 (10 also 02) |
| **5** | `06-freshness-reindex` | 04, 05 |
| **6** | `09-docs-tests` | 02, 03, 05, 07 |

> The numeric order and the waves are two views of the same DAG: the number gives one valid
> serialization (build one at a time, in order); the waves give the maximum parallelism. `06` is
> numerically before `07`/`08` but is a later *wave* — that's fine, its deps (`04`,`05`) are already
> met by the time you reach it.

## Notes

- **Cross-feature order is NOT auto-enforced** by Pathly's executors — the DAG scheduler orders
  tasks *within* a goal, not features across the project. The driver (you, or a project-level loop)
  picks which feature to decompose/run when, following this order.
- **`01-mcp-skeleton` is non-negotiably first** — every other feature imports the `CodeProvider`
  protocol + the safe-null envelope it defines.
- Each feature is independently shippable behind the safe-null contract, so you can stop after any
  wave and still have a working (smaller) tool.
