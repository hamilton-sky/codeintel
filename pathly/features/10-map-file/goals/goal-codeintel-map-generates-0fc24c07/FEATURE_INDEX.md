# F10 — MD map-file mode — Feature Index

> **Read this first.** Every agent working on this feature should load this file before any other plan file.
> It maps every file in this folder so you can fetch only what you need in one read.

---

## Plan files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | Planner | All agents | This file — single entry point for feature context |
| `USER_STORIES.md` | Planner | Tester, Reviewer | Acceptance criteria — the contract |
| `IMPLEMENTATION_PLAN.md` | Planner | Builder, Architect | Phase-by-phase design — the what and how; each phase becomes one board task |
| `HAPPY_FLOW.md` | Planner | Builder, Tester | Golden-path narrative for how map generation and inject work |
| `EDGE_CASES.md` | Planner | Builder, Tester | Failure modes, safe-null paths, and edge scenarios |
| `PLAN_ARCHITECTURE.md` | Planner | Builder, Architect | Module design, interface contracts, key decisions |
| `FLOW_DIAGRAM.md` | Planner | Builder | ASCII flow showing mapper → injector → MCP/CLI wiring |

### Optional plan files (present if signals fired)

| File | Present? | Purpose |
|---|---|---|
| `ARCHITECTURE_PROPOSAL.md` | no | Cross-layer design (see PLAN_ARCHITECTURE.md) |
| `EDGE_CASES.md` | yes | Failure modes and risk scenarios |
| `HAPPY_FLOW.md` | yes | Golden-path narrative |
| `FLOW_DIAGRAM.md` | yes | Multi-component interaction diagram |

---

## Codebase touchpoints

Files in the live repo that this feature reads or modifies.

| Codebase file | Conversation | What changes |
|---|---|---|
| `src/codeintel/mapper.py` | Conv 1 | CREATE — MapGenerator class; graph-ranked CODE_INTEL.md generation |
| `src/codeintel/injector.py` | Conv 1 | CREATE — Injector class; idempotent inject into CLAUDE.md/AGENTS.md |
| `src/codeintel/__main__.py` | Conv 2 | MODIFY — add `map` subcommand (`project_root`, `--inject`, `--budget`) |
| `src/codeintel/server.py` | Conv 2 | MODIFY — add `code.map` MCP tool exposing MapGenerator |
| `tests/test_mapper.py` | Conv 2 | CREATE — unit + integration tests for mapper and injector |

> **Verify these paths exist before editing.** Glob each one. If a path is wrong, correct it before proceeding.

---

## Conversation map

| Conv | Title | Stories | Status | Key files touched |
|---|---|---|---|---|
| 1 | MapGenerator + Injector core | S1.1, S1.2, S1.3, S1.4 | TODO | `src/codeintel/mapper.py`, `src/codeintel/injector.py` |
| 2 | CLI + MCP wiring + tests | S2.1, S2.2, S2.3 | TODO | `src/codeintel/__main__.py`, `src/codeintel/server.py`, `tests/test_mapper.py` |

---

## Feedback files (transient — deleted after resolution)

Live in `pathly/features/10-map-file/feedback/`. A file existing = issue open.

| File | Written by | Resolved by |
|---|---|---|
| `REVIEW_FAILURES.md` | Reviewer | Builder |
| `TEST_FAILURES.md` | Tester | Builder |
| `IMPL_QUESTIONS.md` | Builder [REQ] | Planner |
| `DESIGN_QUESTIONS.md` | Builder [ARCH] | Architect |
| `HUMAN_QUESTIONS.md` | Any agent | User |
