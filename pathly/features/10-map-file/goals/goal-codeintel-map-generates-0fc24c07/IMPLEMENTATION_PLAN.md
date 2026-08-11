# F10 — MD map-file mode — Implementation Plan

## Overview

Feature F10 adds `codeintel map` — a CLI command and MCP tool that generates a ranked,
size-bounded `CODE_INTEL.md` from the graph index. The map is a static orientation snapshot
(architecture overview + top modules + key symbols by fan-in + entry points), consumable with
no tool installed. An optional `--inject` flag appends a reference block into `CLAUDE.md` or
`AGENTS.md` idempotently. This feature builds on the existing GraphProvider (F2) and requires
no new dependencies.

## Layer Architecture

```
CLI (__main__.py)  ──► MapGenerator (mapper.py) ──► GraphProvider (providers/graph.py)
MCP (server.py)   ──┘         │                          │
                              │                     codebase-memory-mcp CLI
                              ▼
                       Injector (injector.py) ──► CLAUDE.md / AGENTS.md
                              │
                              ▼
                       CODE_INTEL.md  (written to project root)
```

---

## Conversation 1 — MapGenerator + Injector core

### Phase 1 — MapGenerator (`src/codeintel/mapper.py`)

**File:** `src/codeintel/mapper.py` — CREATE
**Done when:** `MapGenerator(graph_provider).generate(project_root, budget_bytes)` returns a valid
markdown string in all cases (graph available, graph empty, graph absent); no exception ever
escapes.

**Delivers stories:** S1.1, S1.4

**Depends on:** `src/codeintel/providers/graph.py` (GraphProvider must exist and be importable)

**Enables:** Phase 2 (Injector uses the generated content path), Phase 3 (CLI wraps this)

**Details:**

Create `MapGenerator` class. Constructor accepts an optional `GraphProvider` instance (default:
auto-instantiate from `providers.graph`). Never raises on init.

`generate(project_root: str, budget_bytes: int = 32768) -> str`:
- If graph not available: return `_minimal_map(project_root, note="graph engine not available — install codebase-memory-mcp and run `codeintel index`")`
- Query 1 — Architecture overview: call `provider.build_result("overview", "", [], 5000, project_root)`. Extract result text.
- Query 2 — Ranked symbols (fan-in): run Cypher via `provider._run("query_graph", {"project": project, "query": <cypher>}, 8000)` where cypher is:
  ```
  MATCH (fn) WHERE fn.type IN ['function', 'method', 'class']
  OPTIONAL MATCH (caller)-[:CALLS]->(fn)
  WITH fn, count(caller) as in_degree
  ORDER BY in_degree DESC LIMIT 30
  RETURN fn.name, fn.file_path, in_degree
  ```
- Query 3 — Entry points: run Cypher for nodes with 0 in-degree and at least 1 out-edge (LIMIT 10)
- Build markdown sections: header, architecture, top modules (grouped by file), ranked symbols table, entry points, footer note
- Enforce byte budget: measure rendered length; if over budget, drop ranked-symbols rows from the bottom until under; append `> ⚠ Content truncated to fit {budget_bytes} byte budget.` at the end of the dropped section
- Return final string

`_minimal_map(project_root, note) -> str`: returns a short `CODE_INTEL.md` with the repo name (basename of project_root), the note, and instructions.

`write(project_root, content) -> str`: writes content to `{project_root}/CODE_INTEL.md` and returns the path. Never raises.

Do NOT touch `__main__.py`, `server.py`, or any other file in this phase.

**Verify:** `pytest tests/ -x -q` must pass (no test for mapper yet — verify at least existing tests still pass)

---

### Phase 2 — Injector (`src/codeintel/injector.py`)

**File:** `src/codeintel/injector.py` — CREATE
**Done when:** `Injector().inject(project_root)` idempotently adds/updates a reference block in
`CLAUDE.md` or `AGENTS.md` without duplicating; returns `(path, action)` or `(None, "no-context-file")`.

**Delivers stories:** S1.3

**Depends on:** Phase 1 (CODE_INTEL.md must exist at the path being linked)

**Enables:** Phase 3 (CLI `--inject` flag calls this), Phase 4 (MCP `inject: true` calls this)

**Details:**

Create `Injector` class. No constructor args.

Constants:
```python
_START_MARKER = "<!-- codeintel-map-start -->"
_END_MARKER   = "<!-- codeintel-map-end -->"
_CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md"]
```

`inject(project_root: str) -> tuple[str | None, str]`:
- Find the first of `_CONTEXT_FILES` that exists in `project_root`; if none found, return `(None, "no-context-file")`
- Read the file content
- If both `_START_MARKER` and `_END_MARKER` are present: replace the block between them with the new block content (in-place update, no duplication)
- If markers are absent: append the block at the end of the file (with a blank line separator)
- Write updated content back
- Return `(path_of_context_file, "updated" | "appended")`

Block content (between the markers):
```
## codeintel orientation map

See [CODE_INTEL.md](CODE_INTEL.md) for a ranked overview of this codebase's modules,
key symbols (by call frequency), and entry points. Refresh with: `codeintel map --inject`.
```

Never raises. Any IO error is caught, logged, and returns `(None, "error")`.

Do NOT touch `__main__.py`, `server.py`, or any other file in this phase.

**Verify:** `pytest tests/ -x -q` must pass

---

## Conversation 2 — CLI + MCP wiring + tests

### Phase 3 — CLI `map` subcommand (`src/codeintel/__main__.py`)

**File:** `src/codeintel/__main__.py` — MODIFY
**Done when:** `codeintel map` CLI subcommand generates `CODE_INTEL.md` in the given project
root (or cwd) and prints the output path + size; `--inject` triggers Injector; `--budget N`
overrides the default byte budget.

**Delivers stories:** S2.1

**Depends on:** Phase 1 (MapGenerator), Phase 2 (Injector)

**Enables:** Phase 4 (index auto-refresh uses the same call path)

**Details:**

In `__main__.py`, add a `map` subparser after the existing `install` subparser:
```python
map_parser = subparsers.add_parser("map", help="Generate CODE_INTEL.md orientation file")
map_parser.add_argument("project_root", nargs="?", default=None,
    help="Project root directory (default: cwd)")
map_parser.add_argument("--inject", action="store_true",
    help="Link CODE_INTEL.md into CLAUDE.md or AGENTS.md")
map_parser.add_argument("--budget", type=int, default=32768,
    help="Max byte budget for CODE_INTEL.md (default: 32768)")
```

In the `elif args.command == "map":` branch:
```python
from codeintel.mapper import MapGenerator
from codeintel.providers.graph import GraphProvider

project_root = args.project_root or os.getcwd()
try:
    gp = GraphProvider()
    gen = MapGenerator(gp if gp.available else None)
except Exception:
    gen = MapGenerator(None)

content = gen.generate(project_root, budget_bytes=args.budget)
path = gen.write(project_root, content)
size = len(content.encode())
print(f"Wrote {path} ({size} bytes)")

if args.inject:
    from codeintel.injector import Injector
    ctx_path, action = Injector().inject(project_root)
    if ctx_path:
        print(f"Inject: {action} block in {ctx_path}")
    else:
        print("Inject: no CLAUDE.md or AGENTS.md found — skipped")
```

Also update the `elif args.command == "index":` branch to call `gen.generate()` + `gen.write()`
as a best-effort step AFTER semantic indexing, inside a broad try/except (never block the index
command on a map failure).

Do NOT change any other subcommand handlers.

**Verify:** `python -m codeintel map --help` prints usage; `pytest tests/ -x -q`

---

### Phase 4 — MCP `code.map` tool (`src/codeintel/server.py`)

**File:** `src/codeintel/server.py` — MODIFY
**Done when:** `code.map` MCP tool is registered and returns `{ok, path, size_bytes}` when called
with any combination of `project_root`, `budget`, and `inject` args; never raises.

**Delivers stories:** S2.2

**Depends on:** Phase 1 (MapGenerator), Phase 2 (Injector)

**Enables:** agents can call `code.map` directly in an MCP session

**Details:**

Add a `code_map_handler(args: dict) -> dict` function to `server.py`:
```python
def code_map_handler(args: dict) -> dict:
    try:
        from codeintel.mapper import MapGenerator
        from codeintel.providers.graph import GraphProvider

        project_root = str(args.get("project_root", "") or "")
        budget = int(args.get("budget", 32768) or 32768)
        inject = bool(args.get("inject", False))

        try:
            gp = GraphProvider()
            gen = MapGenerator(gp if gp.available else None)
        except Exception:
            gen = MapGenerator(None)

        content = gen.generate(project_root, budget_bytes=budget)
        path = gen.write(project_root, content)
        size = len(content.encode())

        inject_result = None
        if inject:
            from codeintel.injector import Injector
            ctx_path, action = Injector().inject(project_root)
            inject_result = {"path": ctx_path, "action": action}

        return {"ok": True, "path": path, "size_bytes": size, "inject": inject_result}
    except Exception:
        return {"ok": True, "path": None, "size_bytes": 0, "note": "map-error"}
```

In `run()`, register the tool:
```python
async def _code_map(
    project_root: str = "",
    budget: int = 32768,
    inject: bool = False,
) -> dict:
    return code_map_handler({"project_root": project_root, "budget": budget, "inject": inject})

mcp.add_tool(_code_map, name="code.map",
    description="Generate or refresh CODE_INTEL.md orientation file for the project")
```

Do NOT change `code_query_handler`, `code_status_handler`, or `run()`'s existing tool registrations.

**Verify:** `pytest tests/ -x -q`

---

### Phase 5 — Tests (`tests/test_mapper.py`)

**File:** `tests/test_mapper.py` — CREATE
**Done when:** `pytest tests/test_mapper.py -v` passes all tests with no failures.

**Delivers stories:** S2.3

**Depends on:** Phase 1 (MapGenerator), Phase 2 (Injector)

**Enables:** CI verification of never-raise contract and idempotency

**Details:**

Write `tests/test_mapper.py` with the following tests. Use `unittest.mock` or `pytest-mock`
(available via pytest standard install) to mock GraphProvider calls. Do not require
`codebase-memory-mcp` to be installed for tests to pass.

Tests to include:
1. `test_generate_with_empty_graph` — MapGenerator with provider returning empty results generates a minimal map string (non-empty, no exception)
2. `test_generate_with_no_provider` — MapGenerator(None) generates a minimal map with the "not available" note
3. `test_generate_byte_budget_enforced` — with a mocked provider returning many symbols, passing `budget_bytes=200` produces output ≤ 200 bytes and contains the truncation notice
4. `test_generate_deterministic` — calling generate twice with same mock data returns identical strings
5. `test_write_creates_file` — `gen.write(tmp_path, "# content")` creates `CODE_INTEL.md` in `tmp_path`
6. `test_inject_appends_block` — `Injector().inject(tmp_path)` with a `CLAUDE.md` present appends the start/end markers; action == "appended"
7. `test_inject_is_idempotent` — calling inject twice does not duplicate the block
8. `test_inject_no_context_file` — inject in a dir with no CLAUDE.md or AGENTS.md returns `(None, "no-context-file")` and no exception
9. `test_inject_updates_existing_block` — inject with existing markers updates the content in-place

**Verify:** `pytest tests/test_mapper.py -v`

---

## Prerequisites

- F2 (GraphProvider) must be importable: `from codeintel.providers.graph import GraphProvider` — it is (`src/codeintel/providers/graph.py` exists)
- `codebase-memory-mcp` binary: runtime dep only (auto-detected); tests mock it
- No new Python package dependencies — GraphProvider already shells out to the CLI binary

## Key Decisions

- **No new dependencies:** MapGenerator uses GraphProvider's existing `_run()` and `build_result()` methods; it does NOT add new Python packages
- **Byte budget = explicit drop, never silent truncation:** dropped rows get a `> ⚠ Content truncated` footer — this is a tested invariant, not a convention
- **Inject only updates existing files:** creating `CLAUDE.md` or `AGENTS.md` is not in scope — too opinionated; the user controls which context file exists
- **GraphProvider internal access (`_run`):** the mapper needs to run custom Cypher (fan-in ranking) that `build_result()` doesn't expose; it calls `provider._run("query_graph", ...)` directly; this is acceptable since both modules are internal to the same package
- **Refresh on index:** hooked into the existing `index` CLI branch as a best-effort try/except — never blocks or fails the index command
- **MCP tool name:** `code.map` (consistent with `code.query` / `code.status` naming convention)
