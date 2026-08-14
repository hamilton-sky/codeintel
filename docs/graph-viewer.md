# Graph viewer — `codeintel graph`

Turn the graph engine's structure into something you can **see** — for any indexed repo. The command
emits the call graph as machine-readable JSON, or wraps it in a **single self-contained interactive
HTML file** you can open in any browser, offline, with no server and no build step.

codeintel stays a **headless** code-intelligence brain: the CLI produces the *data* and the *picture*;
there is no running UI app. It follows a strict **data→renderer** split — the graph engine produces
deterministic structure (no hallucinated edges), and a template renders it — so the same JSON payload
can feed the built-in viewer, an external tool, or an agent.

![codeintel's own call graph in the viewer — nodes sized by complexity, colored by module, with the layout switcher, legend, and toolbar.](images/graph-codeintel.png)

## Usage

```bash
codeintel graph                      # {nodes, edges} JSON for the current repo → stdout
codeintel graph /path/to/repo        # …for any indexed repo
codeintel graph . --html             # write a self-contained interactive viewer (codeintel-graph.html)
codeintel graph . --html --out graph.html --limit 400
```

| Flag | Meaning |
|---|---|
| `project_root` | Repo to graph (default: cwd) |
| `--html` | Write the interactive HTML viewer instead of printing JSON |
| `--out FILE` | Output path for `--html` (default: `codeintel-graph.html`) |
| `--limit N` | Max call edges to include (default: 220) |

## The viewer

A force-directed call graph, rendered from the JSON payload. Everything is inline — open the file
anywhere, no network:

- **Four layouts** — **Force** (organic clusters) · **Radial** (rings by distance from the busiest hub)
  · **Layered** (top-down call flow) · **Modules** (grouped by directory).
- **Node size = cyclomatic complexity**; **color = directory** (so it generalizes to any repo layout).
- **Click a symbol** → its metrics (complexity, cognitive, fan-in / fan-out, lines) and the symbols it
  connects to; hover to highlight a neighborhood.
- **Search** to filter symbols; **drag** a node to pin, **drag** the background to pan, **scroll** to zoom.
- **Export** the current graph as **JSON / Markdown / SVG / PNG** (downloads work from a locally-opened
  file; inside a sandboxed preview use **Copy**).

## JSON payload (`--format`-style output)

The default (no `--html`) prints the machine-readable payload — the same shape any renderer, board, or
agent can consume:

```json
{
  "project": "codeintel",
  "engine": "graph",
  "op": "callgraph",
  "nodes": [
    { "id": "codeintel.src.codeintel.__main__.main", "label": "main", "file": "src/codeintel/__main__.py",
      "complexity": 33, "cognitive": 110, "in_degree": 1, "out_degree": 41, "lines": 220 }
  ],
  "edges": [
    { "from": "codeintel.src.codeintel.mapper._enforce_budget", "to": "codeintel.src.codeintel.mapper._render", "type": "CALLS" }
  ]
}
```

Nodes are the symbols that participate in an internal call edge, enriched with the same complexity
metrics the `hotspots` op surfaces; edges are `CALLS`/`USAGE` relations. Builtins and generated nodes
(`<python-builtins>`, …) are excluded.

## Requirements & safety

- Needs the **graph engine**: the `codebase-memory-mcp` backend installed and this repo indexed. Run
  `codeintel doctor` to check both. Without them the payload is **empty with a `reason`**
  (`engine-unavailable` / `project-not-indexed`) — the command still succeeds and writes a viewer that
  says so, never crashes (the same never-raise contract as `code.query`).
- The generated HTML is fully self-contained (data embedded, no external requests) — safe to open
  offline, share as a file, or archive.

## Where it lives

- `src/codeintel/grapher.py` — `build_graph_payload()` (data) and `render_html()` (renderer).
- `src/codeintel/viewer/graph_template.html` — the self-contained viewer template (ships with the
  package; `render_html` injects the payload into its `__DATA__` slot).
