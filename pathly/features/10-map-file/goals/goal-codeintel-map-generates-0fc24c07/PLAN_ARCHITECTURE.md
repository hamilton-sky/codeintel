# F10 — MD map-file mode — Plan Architecture

## Problem Statement

Coding agents need an orientation snapshot they can read without any MCP connection or tool
installed. The existing `code.query` tool answers live queries but requires the server to be
running. Feature F10 adds a static, committed markdown file (`CODE_INTEL.md`) generated from
the graph index — covering ranked modules, key symbols, and entry points.

## Proposed Solution

Two new single-responsibility modules (`mapper.py`, `injector.py`) + thin wiring into the
existing CLI (`__main__.py`) and MCP server (`server.py`). No new dependencies: `mapper.py`
reuses `GraphProvider` (already wrapping `codebase-memory-mcp`); `injector.py` is pure stdlib.

## Layer Breakdown

```
CLI (__main__.py)
  "map" subcommand
     │  MapGenerator.generate() + .write()
     │  Injector.inject()  (if --inject)
     ▼
mapper.py  (MapGenerator)
     │  provider.build_result("overview", ...)
     │  provider._run("query_graph", {Cypher for ranked symbols})
     │  provider._run("query_graph", {Cypher for entry points})
     ▼
providers/graph.py  (GraphProvider)
     │  subprocess codebase-memory-mcp CLI
     ▼
codebase-memory-mcp (graph index on disk)

injector.py  (Injector)
     │  stdlib only: pathlib, re
     ▼
CLAUDE.md / AGENTS.md  (idempotent append/update)

server.py (code.map MCP tool)
     │  code_map_handler → MapGenerator + Injector
     ▼
MCP client (agent in session)
```

## Key Design Decisions

### Decision 1: Reuse `GraphProvider._run()` for custom Cypher
- **Options considered**: (A) add a `ranked_symbols` method to GraphProvider; (B) call `provider._run()` directly from MapGenerator; (C) add a new engine-level API
- **Chosen**: B — call `provider._run()` directly
- **Rationale**: `build_result()` only exposes coarse ops (overview, callers, etc.) and does not support the fan-in ranking Cypher. Adding a new public method to GraphProvider would widen its API for a single caller. `_run()` is already the internal dispatch primitive; both modules live in the same package, so this is acceptable coupling. If GraphProvider later grows a `ranked_symbols()` method, the call in mapper.py is the only site to update.

### Decision 2: Byte budget = explicit drop with notice, never silent truncation
- **Options considered**: (A) silent truncation at N bytes; (B) drop whole sections until under budget; (C) drop individual rows from the ranked table until under budget, then append a notice
- **Chosen**: C — drop rows, append notice
- **Rationale**: The header, architecture section, and entry points are high-value and small. The ranked symbol table is the most token-heavy and most compressible section. Dropping rows from the bottom of a sorted list is the least harmful truncation. The notice makes the drop visible to agents reading the file.

### Decision 3: Inject only — never create the context file
- **Options considered**: (A) create `CLAUDE.md` if it doesn't exist; (B) only modify existing files
- **Chosen**: B — only modify
- **Rationale**: Creating `CLAUDE.md` is opinionated. Many projects have neither file, have a custom name, or place the file in a subdirectory. Auto-creating it would surprise users. The scope note in the SPEC says "links or appends" — linking implies the file already exists.

### Decision 4: Map refresh after `codeintel index` — best-effort, non-blocking
- **Options considered**: (A) always refresh; (B) flag to enable refresh; (C) best-effort, silent on failure
- **Chosen**: C — best-effort, non-blocking
- **Rationale**: `index` is a heavy operation. If the graph is not yet built (common on first run), the map would be minimal. Failing the map silently is fine because the user can re-run `codeintel map` once the graph is ready. The index command must never fail due to a map generation error.

## Phase Mapping

### Phase 1 — MapGenerator
New module `src/codeintel/mapper.py`. The `MapGenerator` class owns all graph queries and
markdown rendering. The `write()` method owns the disk write. Both are independently testable
(mock the provider, pass a tmp_path for write).

### Phase 2 — Injector
New module `src/codeintel/injector.py`. Pure stdlib. Self-contained idempotent block management.
The marker convention (`<!-- codeintel-map-start/end -->`) is the only coupling with the outside
world — any future tool that reads CLAUDE.md can detect and skip this block.

### Phase 3 — CLI wiring
Only `__main__.py` is modified. One new subparser, one new elif branch. The index branch gets a
best-effort tail call to map generation.

### Phase 4 — MCP tool
Only `server.py` is modified. One new handler function, one new `mcp.add_tool()` call.
Existing tools (`code.query`, `code.status`) are untouched.

### Phase 5 — Tests
New file `tests/test_mapper.py`. Uses mocked GraphProvider (no binary required). Covers:
never-raise, budget enforcement, determinism, inject idempotency, inject missing file.

## Key Components

| Component | File | Role |
|---|---|---|
| `MapGenerator` | `src/codeintel/mapper.py` | Generates ranked markdown from graph queries |
| `Injector` | `src/codeintel/injector.py` | Idempotent block inject into CLAUDE.md/AGENTS.md |
| `code_map_handler` | `src/codeintel/server.py` | MCP tool wrapper |
| `map` subcommand | `src/codeintel/__main__.py` | CLI surface |

## Interface Design

```python
# mapper.py
class MapGenerator:
    def __init__(self, provider=None) -> None: ...  # None → minimal map
    def generate(self, project_root: str, budget_bytes: int = 32768) -> str: ...
    def write(self, project_root: str, content: str) -> str: ...  # returns path

# injector.py
class Injector:
    def inject(self, project_root: str) -> tuple[str | None, str]: ...
    # returns (context_file_path | None, action)
    # action: "appended" | "updated" | "no-context-file" | "error"
```

## Risks

- **Graph Cypher syntax differences**: `codebase-memory-mcp` may not support the exact Cypher used for fan-in ranking. Mitigation: Phase 1 must test against the live graph backend; if Cypher fails, fall back to the `overview` op output only.
- **GraphProvider `_run` is private**: coupling to an internal method. Mitigation: both modules are in the same package; document the dependency in a comment so a future refactor is aware.
- **Large repos exceed budget quickly**: the default 32768 byte budget may be too small for very large codebases. Mitigation: budget is configurable via `--budget`; the truncation notice tells agents the map is partial.
