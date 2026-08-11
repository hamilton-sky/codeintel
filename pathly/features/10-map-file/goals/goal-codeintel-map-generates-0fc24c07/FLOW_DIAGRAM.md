# F10 — MD map-file mode — Flow Diagram

## Happy Path: `codeintel map --inject`

```
User: codeintel map /repo --inject
              │
              ▼
        __main__.py  (map subcommand)
              │
              ├─ GraphProvider() ─────► codebase-memory-mcp on PATH?
              │                             │
              │                    yes ─────┘   no ──► provider = None
              │
              ▼
        MapGenerator(provider)
              │
              ├─ provider available? ──► no ──► _minimal_map("not available")
              │                                       │
              │         yes                           ▼
              │           │                      CODE_INTEL.md (minimal)
              │           ▼
              │   build_result("overview", ...)
              │           │
              │           ├─ result: None ──► skip section
              │           │
              │           └─ result: text ──► architecture_section
              │
              ├─ _run("query_graph", cypher=ranked_by_in_degree)
              │           │
              │           ├─ result: [] ──► skip section
              │           │
              │           └─ result: rows ──► ranked_symbols_table
              │
              ├─ _run("query_graph", cypher=entry_points)
              │           │
              │           └─ result: rows ──► entry_points_section
              │
              ├─ render markdown (join sections)
              │
              ├─ len(content) > budget_bytes?
              │           │
              │     yes ──┤  drop bottom rows from ranked table
              │           │  append "> ⚠ Content truncated..."
              │           │
              │     no ───┘
              │
              ▼
        gen.write(project_root, content)
              │
              └─ write CODE_INTEL.md ──► print "Wrote ... (N bytes)"
              │
              ▼  (if --inject)
        Injector().inject(project_root)
              │
              ├─ CLAUDE.md exists? ──► yes ──► target = CLAUDE.md
              │                        no
              │                         └─ AGENTS.md exists? ──► yes ──► target = AGENTS.md
              │                                                    no ──► return (None, "no-context-file")
              │
              ├─ both markers in content?
              │           │
              │     yes ──┤  replace block in-place ──► action = "updated"
              │           │
              │     no ───┤  append block ──► action = "appended"
              │
              └─ write target file
              │
              └─ print "Inject: {action} block in {target}"
```

## Fallback: Graph Unavailable

```
codeintel map /repo
              │
              ▼
        GraphProvider  ──► binary not found
              │
              ▼
        MapGenerator(provider=None)
              │
              ▼
        _minimal_map(note="graph engine not available")
              │
              ▼
        CODE_INTEL.md  (minimal — states note, how to fix)
              │
              └─ exit code 0
```

## MCP Path: `code.map` tool

```
MCP agent: {tool: "code.map", project_root: "/repo", inject: true}
              │
              ▼
        code_map_handler(args)  (server.py)
              │
              ├─ GraphProvider() + MapGenerator
              ├─ gen.generate(project_root, budget)
              ├─ gen.write(project_root, content)
              │
              ├─ inject=true? ──► Injector().inject(project_root)
              │
              └─ return {ok: true, path: "...", size_bytes: N}
              │
              ──► any Exception ──► {ok: true, path: null, note: "map-error"}
```

## Component Legend

| Component | Role |
|---|---|
| `__main__.py (map)` | CLI entry; parses args, calls MapGenerator + Injector |
| `MapGenerator` | Graph queries + markdown rendering + byte budget enforcement |
| `GraphProvider` | Shells out to `codebase-memory-mcp`; returns safe-null on any failure |
| `Injector` | Reads/writes CLAUDE.md or AGENTS.md; idempotent marker management |
| `code_map_handler` | MCP tool wrapper; catches all exceptions |
| `CODE_INTEL.md` | The artifact — static markdown file committed to the repo |
