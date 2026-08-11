# F10 — MD map-file mode — Edge Cases

---

## Phase 1 — MapGenerator (`src/codeintel/mapper.py`)

### EC-1.1: `codebase-memory-mcp` binary not on PATH
- **Trigger**: User runs `codeintel map` on a machine without the graph backend installed
- **Current behavior**: GraphProvider detects absence (`shutil.which` returns None), `available = False`
- **Expected behavior**: MapGenerator receives `None` or unavailable provider → returns minimal map string with note "graph engine not available — install codebase-memory-mcp and run `codeintel index`"; exit code 0, `CODE_INTEL.md` written
- **Handled in**: Phase 1 — `if not provider or not provider.available: return _minimal_map(...)`

### EC-1.2: Graph binary present but project not indexed
- **Trigger**: `codebase-memory-mcp` is installed but user hasn't run `codeintel index` yet
- **Current behavior**: `_resolve_project()` returns `None`; `build_result()` returns safe-null with `reason="project-not-indexed"`
- **Expected behavior**: MapGenerator treats null result as empty graph → writes minimal map with note "project not yet indexed — run `codeintel index` first"
- **Handled in**: Phase 1 — handle null/empty results from all three graph queries

### EC-1.3: Graph query times out
- **Trigger**: Graph index is large / slow, query exceeds timeout
- **Current behavior**: `subprocess.run` raises `TimeoutExpired`; `GraphProvider._run` catches it, returns `None`
- **Expected behavior**: MapGenerator treats null result as empty for that section; other sections may still render; no exception
- **Handled in**: Phase 1 — each query result is optional; missing sections are omitted from output

### EC-1.4: Graph returns unexpected / malformed JSON
- **Trigger**: `codebase-memory-mcp` emits non-JSON or malformed response
- **Current behavior**: `GraphProvider._run` catches `json.JSONDecodeError`, returns `None`
- **Expected behavior**: MapGenerator treats null as empty; no exception
- **Handled in**: Phase 1 — same null-guard as EC-1.2

### EC-1.5: Output exactly at byte budget
- **Trigger**: Generated content is exactly `budget_bytes` bytes
- **Expected behavior**: No truncation occurs; no truncation notice appended
- **Handled in**: Phase 1 — budget check is `len(content.encode()) > budget_bytes` (strictly greater)

### EC-1.6: Budget so small nothing fits
- **Trigger**: `--budget 10` — even the header exceeds the budget
- **Expected behavior**: Output is the minimal map header only (no truncation of the header itself); truncation notice appended; file is written; no exception
- **Handled in**: Phase 1 — enforce budget by dropping ranked rows; the header/footer are never dropped (they are the minimum viable output)

### EC-1.7: Project root is empty directory
- **Trigger**: User maps an empty directory
- **Expected behavior**: Graph finds no files → returns empty results → minimal map with "no symbols found" note; `CODE_INTEL.md` written
- **Handled in**: Phase 1

### EC-1.8: `CODE_INTEL.md` is not writable (permissions error)
- **Trigger**: User runs map in a read-only directory
- **Expected behavior**: `gen.write()` catches the `PermissionError`, logs it, returns the intended path; CLI prints the error inline; exit code 0 (never raises)
- **Handled in**: Phase 1 — `write()` wraps the open/write in a broad try/except

---

## Phase 2 — Injector (`src/codeintel/injector.py`)

### EC-2.1: Neither CLAUDE.md nor AGENTS.md exists
- **Trigger**: User runs `codeintel map --inject` in a project with no context file
- **Expected behavior**: Injector returns `(None, "no-context-file")`; CLI prints "no CLAUDE.md or AGENTS.md found — skipped"; no file created; exit code 0
- **Handled in**: Phase 2 — first check is existence of context files before any write

### EC-2.2: Both CLAUDE.md and AGENTS.md exist
- **Trigger**: Project has both files
- **Expected behavior**: Injector prefers `CLAUDE.md` (checked first in `_CONTEXT_FILES` list); `AGENTS.md` is untouched
- **Handled in**: Phase 2 — `_CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md"]`; loop stops at first match

### EC-2.3: Inject block has only start marker (corrupted state)
- **Trigger**: A previous crash left `<!-- codeintel-map-start -->` but no end marker
- **Expected behavior**: Injector detects that only one marker is present → falls back to append behavior (appends a fresh block at the end); does not corrupt the file
- **Handled in**: Phase 2 — check `_START_MARKER in content and _END_MARKER in content` before attempting replacement

### EC-2.4: Context file is not UTF-8 decodable
- **Trigger**: CLAUDE.md contains binary or non-UTF-8 content
- **Expected behavior**: Injector catches the `UnicodeDecodeError`, logs it, returns `(None, "error")`; file is untouched
- **Handled in**: Phase 2 — open with `encoding="utf-8", errors="strict"` inside try/except

### EC-2.5: Context file is read-only
- **Trigger**: CLAUDE.md exists but has write permissions denied
- **Expected behavior**: Injector catches `PermissionError`, returns `(None, "error")`; no exception escapes
- **Handled in**: Phase 2 — write wrapped in try/except

---

## Phase 3 — CLI `map` subcommand

### EC-3.1: `codeintel index` fails during map refresh
- **Trigger**: Graph query errors during the post-index map refresh
- **Expected behavior**: `index` command prints a warning ("map refresh skipped: <reason>") but exits 0; the index itself is not rolled back
- **Handled in**: Phase 3 — map refresh inside broad try/except in `index` branch

### EC-3.2: `--budget` is zero or negative
- **Trigger**: User passes `--budget 0`
- **Expected behavior**: CLI uses the default (32768) — or raises argparse error for negative values; no crash in MapGenerator
- **Handled in**: Phase 3 — add `map_parser.add_argument("--budget", type=int, default=32768)` with a `min` check, or clamp in MapGenerator

---

## Phase 4 — MCP `code.map` tool

### EC-4.1: `project_root` missing in MCP call
- **Trigger**: Agent calls `code.map` with no `project_root`
- **Expected behavior**: Handler defaults to `""` → MapGenerator gets empty root → returns minimal map; response is `{ok: true, path: null, ...}` if write fails (no cwd in server context)
- **Handled in**: Phase 4 — `project_root = str(args.get("project_root", "") or "")`

### EC-4.2: Exception in handler
- **Trigger**: Any unexpected exception in `code_map_handler`
- **Expected behavior**: Outer `except Exception` catches it; returns `{ok: True, path: None, size_bytes: 0, note: "map-error"}`; never raises to MCP runtime
- **Handled in**: Phase 4 — broad try/except wrapping the entire handler body

---

## Known Limitations

- Map is a static snapshot; it reflects graph state at generation time. A very active repo may have stale rankings until the next `codeintel index` run.
- Entry-point detection depends on the graph backend's CALLS edges. Repos not yet indexed will show no entry points.
- The `--inject` flag requires an existing `CLAUDE.md` or `AGENTS.md`. It does not create them.
- Ranked symbol ordering is by in-degree (number of callers), not PageRank. Full PageRank is a v2 enhancement.
