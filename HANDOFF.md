# Handoff — finish the two wrapped engines (graph + LSP)

**For:** a fresh Claude Code session. **Branch:** `follow-ups`.

## Why this exists

codeintel's **semantic engine** and **safe-null / never-raise contract** are verified working
(dogfooded live on 2026-08-12). Its two **wrapped** engines are *not* verified against their real
backends and currently return no data end-to-end:

- **graph** wraps `codebase-memory-mcp` — written to an *assumed* contract that doesn't match reality.
- **lsp** wraps a language server via `uvx serena` — never driven end-to-end.

Every unit test mocks the backend boundary (`_run` / `_call_tool`), which is exactly what hid these
bugs. This handoff is to make both engines actually work, with tests that hit the **real** boundary.

See [ASSESSMENT.md](ASSESSMENT.md) for the full review and the fixes already applied.

---

## Paste-ready prompt

```text
You are working on `codeintel`, a local MCP code-intelligence server at
/Users/shammaihamilton/Documents/project/codeintel. Its semantic engine and safe-null
contract work; its two WRAPPED engines are NOT verified against their real backends and
must be made to actually work end-to-end. Work on the `follow-ups` branch. Keep all 93
existing tests green and the never-raise contract intact. Read ASSESSMENT.md first, then
src/codeintel/providers/{graph,lsp}.py, and PROBE the real backends before editing.

TASK A — Graph engine (src/codeintel/providers/graph.py) wraps `codebase-memory-mcp`.
It was written to an ASSUMED contract that doesn't match reality. Probe with
`codebase-memory-mcp cli <method> '<json>'` (raw-JSON args are deprecated-but-working) or
the codebase-memory-mcp MCP tools. Verified facts:
  1. list_projects → {"projects":[{name, root_path, ...}]} — ALREADY FIXED (_resolve_project
     accepts dict|list).
  2. query_graph → {"columns":[...], "rows":[...], "total":N, "hint":"..."}, NOT a bare list;
     rows are value-arrays aligned to columns. Every _op_* method does
     `if not isinstance(raw, list)` and discards real results. Rewrite _op_callers,
     _op_callees, _op_pattern, _op_chain, _op_overview to read resp["rows"] + map columns→values.
  3. The Cypher is misaligned with the real schema. Node labels are Function/Section/File
     (run get_graph_schema for the full schema incl. EDGE TYPES — truncated in my probe).
     `MATCH (caller)-[:CALLS]->(fn) WHERE fn.name="X"` returns 0 rows — the call edge
     type/direction is wrong. Find the real call edge and rewrite callers/callees/impact/chain.
     Test symbol: `safe_null_result` exists as a Function node in the already-indexed
     "codeintel" project.
  4. Verify + fix response shapes for trace_path (chain), search_code (pattern),
     get_architecture (overview) the same way.
  DoD-A: `codeintel query --op impact|callers|callees --target <sym> --engine graph` returns
  real data against the codeintel project.

TASK B — LSP engine (src/codeintel/providers/lsp.py) wraps a language server via `uvx serena`
(uvx is at /opt/homebrew/bin/uvx; serena is not a standalone binary). Untested end-to-end.
  1. Run `codeintel query --op symbol --target <sym> --engine lsp --project-root <repo>`.
     First call(s) should return reason:warming, then real definition + references. Confirm
     the warm-up→ready transition and that the session is cached per project_root (the gateway
     is now a singleton, so it should persist — verify under the real server too).
  2. Verify serena's REAL CLI/tool interface matches what the provider assumes: launch args
     `["uvx","serena","--project_root",<root>]` and MCP method names find_symbol /
     find_referencing_symbols / get_symbols_overview. Fix any that drifted (same class of bug
     as graph — assumed vs real contract).
  3. Boot failures must cool down (no per-request respawn) and never raise.
  DoD-B: `--op symbol --engine lsp` returns real definition/references after warm-up.

CONSTRAINTS (both tasks):
  - Branch `follow-ups`; all 93 tests stay green; never-raise holds (add fault injection where
    you touch a path).
  - Add tests that hit the REAL subprocess/backend boundary (or captured real-response
    fixtures) — do NOT just mock _run / _call_tool. Mocking the seam is exactly what hid these
    bugs; a mocked test here is worthless.
  - Commit each engine separately with a clear message. When done, run the full suite AND a
    live `codeintel query` against a real indexed repo, and report the actual outputs.
```

## Quick reference (verified probes, 2026-08-12)

- `codebase-memory-mcp cli list_projects '{}'` → `{"projects":[...]}` (rc=0, deprecation warning on stderr).
- `codebase-memory-mcp cli query_graph '{"project":"codeintel","query":"..."}'` → `{"columns":[...],"rows":[...],"total":N,"hint":"..."}`.
- `get_graph_schema` node labels: `Function` (132), `Section` (787), `File` (133), … properties incl. `name`, `qualified_name`, `file_path`.
- The `codeintel` project is already indexed in codebase-memory-mcp (via `index_repository`, 1475 nodes / 2809 edges).
- Smaller deferred items (optional stretch, see ASSESSMENT.md): migrate `GraphProvider._run` off deprecated raw-JSON CLI args to flags; make the map generator consume the `CodeProvider` protocol instead of `GraphProvider` internals.
