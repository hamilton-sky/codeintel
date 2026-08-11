# F10 — MD map-file mode — User Stories

## Context

Coding agents orient to a new codebase by reading files — but no single file gives them a ranked
overview of the most-connected modules and symbols. `codeintel map` fills this gap by generating
`CODE_INTEL.md`: a ranked, size-bounded snapshot that any agent or human reads for instant
orientation — no MCP, no tool installed, just a markdown file in the repo. It complements the live
`code.query` tool; it does not replace it.

---

## Stories

### Story 1.1: Generate CODE_INTEL.md with ranked symbol overview
**As a** coding agent, **I want** `codeintel map` to write a `CODE_INTEL.md` in my project root,
**so that** I can orient to the codebase structure without calling any live tool.

**Acceptance Criteria:**
- [ ] `codeintel map` (CLI) writes `CODE_INTEL.md` to the project root
- [ ] File contains: architecture overview, top modules by file, key symbols ranked by fan-in (in-degree), entry points, and "who-calls-what" highlights
- [ ] File is under a documented byte budget; content that would exceed the budget is dropped with an explicit notice (never silently truncated)
- [ ] File is deterministic: same graph → same output; no timestamps or random ordering
- [ ] Never raises — a missing or empty graph index yields a minimal `CODE_INTEL.md` with a note, not an exception

**Delivered by:** Phase 1 → Conversation 1

---

### Story 1.2: Refresh map on index
**As a** coding agent, **I want** `CODE_INTEL.md` to be refreshed automatically when the graph is
re-indexed, **so that** the map stays current after I edit the codebase.

**Acceptance Criteria:**
- [ ] `codeintel index` (CLI) re-runs map generation after indexing completes
- [ ] `code.map` MCP tool can be called explicitly to refresh on demand
- [ ] Re-generation is idempotent (calling twice with no changes produces the same file)

**Delivered by:** Phase 3, 4 → Conversation 2

---

### Story 1.3: Optional --inject links CODE_INTEL.md into agent context file
**As a** developer, **I want** `codeintel map --inject` to link `CODE_INTEL.md` into `CLAUDE.md`
or `AGENTS.md`, **so that** my AI assistant automatically loads the orientation file at the start
of every session.

**Acceptance Criteria:**
- [ ] `--inject` finds `CLAUDE.md` (preferred) then `AGENTS.md` in the project root
- [ ] Appends a clearly-bounded block (delimited by `<!-- codeintel-map-start -->` / `<!-- codeintel-map-end -->`) containing a link to `CODE_INTEL.md` and a one-line description
- [ ] Block is never duplicated: a second `--inject` call updates the block in-place
- [ ] If neither file exists, `--inject` logs a message and exits cleanly — it does NOT create a new file
- [ ] `code.map` MCP tool accepts `inject: true` parameter with the same contract

**Delivered by:** Phase 2 → Conversation 1

---

### Story 1.4: Minimal map when graph is unavailable
**As a** coding agent, **I want** `codeintel map` to produce a useful minimum output even when the
graph engine is not installed or the index is empty, **so that** the command never fails.

**Acceptance Criteria:**
- [ ] When `codebase-memory-mcp` binary is absent: writes a `CODE_INTEL.md` that states "graph engine not available — install and index to generate a ranked map"
- [ ] When graph is installed but the project is not indexed: writes a `CODE_INTEL.md` that states "project not yet indexed — run `codeintel index` first"
- [ ] When graph returns empty results: writes a minimal map with the architecture note and a "no symbols found" section
- [ ] In all cases the exit code is 0 and no exception is raised

**Delivered by:** Phase 1 → Conversation 1

---

### Story 2.1: CLI `map` subcommand
**As a** developer, **I want** a `codeintel map [project_root] [--inject] [--budget N]` CLI
subcommand, **so that** I can generate and refresh `CODE_INTEL.md` from the terminal.

**Acceptance Criteria:**
- [ ] `codeintel map` defaults project root to cwd
- [ ] `--inject` triggers the injector (Story 1.3)
- [ ] `--budget N` overrides the default byte budget (default: 32768 bytes)
- [ ] Prints a one-line summary of what was written (path + size)

**Delivered by:** Phase 3 → Conversation 2

---

### Story 2.2: MCP `code.map` tool
**As a** coding agent using MCP, **I want** a `code.map` tool that generates/refreshes
`CODE_INTEL.md` on demand, **so that** I can trigger orientation-file generation without leaving
my agent session.

**Acceptance Criteria:**
- [ ] `code.map` tool accepts `{project_root?, budget?, inject?}` — all optional
- [ ] Returns `{ok, path, size_bytes, note?}` — never raises
- [ ] `inject: true` triggers idempotent inject (Story 1.3)

**Delivered by:** Phase 4 → Conversation 2

---

### Story 2.3: Tests for mapper and injector
**As a** developer, **I want** automated tests covering the mapper and injector, **so that** the
never-raise contract and idempotency guarantees are verified by CI.

**Acceptance Criteria:**
- [ ] Test: map generates valid markdown from a mocked graph
- [ ] Test: byte budget is enforced — over-budget content dropped with explicit note
- [ ] Test: empty/unavailable graph → minimal map returned, no exception
- [ ] Test: inject is idempotent — second call does not duplicate the block
- [ ] Test: inject when no CLAUDE.md/AGENTS.md exists → exits cleanly
- [ ] All tests pass with `pytest tests/test_mapper.py`

**Delivered by:** Phase 5 → Conversation 2
