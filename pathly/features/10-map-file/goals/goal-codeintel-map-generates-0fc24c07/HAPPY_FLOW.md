# F10 — MD map-file mode — Happy Flow

## Overview

A developer runs `codeintel map --inject` in a freshly cloned repo that already has a graph
index. Within a second, `CODE_INTEL.md` appears in the project root with a ranked symbol
overview, and a reference block is appended to `CLAUDE.md`. Their coding agent will load this
file automatically on the next session.

---

## Phase 1 — MapGenerator (`src/codeintel/mapper.py`)

### Step-by-Step

#### Step 1: User invokes `codeintel map`
- **User does**: runs `codeintel map /path/to/repo`
- **System does**: CLI instantiates `GraphProvider` (detects `codebase-memory-mcp` on PATH), creates `MapGenerator(graph_provider)`
- **State after**: MapGenerator ready, graph provider confirmed available

#### Step 2: Architecture overview queried
- **User does**: (nothing — automatic)
- **System does**: `MapGenerator.generate()` calls `provider.build_result("overview", "", [], 5000, project_root)`, receives a text block describing repo structure
- **State after**: architecture section content ready

#### Step 3: Ranked symbols queried
- **User does**: (nothing — automatic)
- **System does**: Runs Cypher `MATCH (fn) ... WITH fn, count(caller) as in_degree ORDER BY in_degree DESC LIMIT 30 RETURN fn.name, fn.file_path, in_degree`; receives list of top 30 symbols sorted by caller count
- **State after**: ranked symbol table ready

#### Step 4: Entry points queried
- **User does**: (nothing — automatic)
- **System does**: Runs Cypher for functions with 0 in-degree but at least 1 out-edge; receives up to 10 entry point functions
- **State after**: entry points section ready

#### Step 5: Byte budget enforced
- **User does**: (nothing — automatic)
- **System does**: Renders full markdown; measures byte length; if over default budget (32768 bytes), removes ranked-symbol rows from the bottom until under budget; appends truncation notice
- **State after**: final markdown string is within budget

#### Step 6: CODE_INTEL.md written
- **User does**: (nothing — automatic)
- **System does**: `gen.write(project_root, content)` writes `CODE_INTEL.md` to project root; CLI prints `Wrote /path/CODE_INTEL.md (N bytes)`
- **State after**: `CODE_INTEL.md` exists in the project root, committed-ready

---

## Phase 2 — Injector (`src/codeintel/injector.py`)

### Step-by-Step

#### Step 1: User passes `--inject`
- **User does**: ran `codeintel map --inject` (or CLI branch adds inject call after generate)
- **System does**: CLI calls `Injector().inject(project_root)` after `gen.write()`
- **State after**: Injector instantiated

#### Step 2: Context file located
- **User does**: (nothing — automatic)
- **System does**: Injector checks for `CLAUDE.md` in project root; finds it
- **State after**: target file identified

#### Step 3: Block appended (first run)
- **User does**: (nothing — automatic)
- **System does**: No markers found → appends `<!-- codeintel-map-start -->` … `<!-- codeintel-map-end -->` block with link and description
- **State after**: `CLAUDE.md` ends with the reference block; CLI prints `Inject: appended block in /path/CLAUDE.md`

#### Step 4: Second run is idempotent
- **User does**: runs `codeintel map --inject` again later
- **System does**: Injector finds existing markers → replaces block content in-place, no duplication
- **State after**: `CLAUDE.md` has exactly one codeintel block; CLI prints `Inject: updated block in /path/CLAUDE.md`

---

## Phase 3 — CLI `map` subcommand

### Step-by-Step

#### Step 1: Auto-refresh after index
- **User does**: runs `codeintel index`
- **System does**: semantic + graph re-indexing completes; then best-effort `gen.generate()` + `gen.write()` refreshes `CODE_INTEL.md`
- **State after**: `CODE_INTEL.md` is up-to-date with the latest graph

---

## Phase 4 — MCP `code.map` tool

### Step-by-Step

#### Step 1: Agent calls `code.map` via MCP
- **User does**: agent sends `{tool: "code.map", project_root: "/repo", inject: true}` over MCP
- **System does**: `code_map_handler` generates map, writes file, optionally injects; returns `{ok: true, path: "...", size_bytes: N}`
- **State after**: agent receives orientation file path and can read it on the next turn

---

## End State

After the happy flow completes:
- `CODE_INTEL.md` exists in the project root with a ranked, size-bounded overview
- `CLAUDE.md` contains exactly one `<!-- codeintel-map-start/end -->` block
- `codeintel index` will refresh the map automatically going forward
- Any agent (MCP or not) can read `CODE_INTEL.md` for instant orientation

## Success Indicators
- [ ] `CODE_INTEL.md` is written within 2 seconds on a repo with a live graph index
- [ ] Running `codeintel map --inject` twice leaves `CLAUDE.md` with exactly one reference block
- [ ] `codeintel index` finishes with a map refresh note in its output
- [ ] `code.map` MCP tool returns `{ok: true}` even when graph is unavailable
