# Goal: Boot MCP Server (F1 Skeleton) — Feature Index

> **Read this first.** Every agent working on this goal should load this file before any other plan file.
> It maps every file in this folder so you can fetch only what you need in one read.

Goal: Boot the MCP server + code.query/code.status with a safe-null NoneProvider (safe-off skeleton).
Feature: `01-mcp-skeleton` (F1) — part of the **codeintel** project.

---

## Plan files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | Planner | All agents | This file — single entry point for goal context |
| `USER_STORIES.md` | Planner | Tester, Reviewer | Acceptance criteria — the contract |
| `IMPLEMENTATION_PLAN.md` | Planner | Builder, Architect | Phase-by-phase design — each phase becomes one board task |
| `HAPPY_FLOW.md` | Planner | Builder, Reviewer | Golden-path narrative per phase |
| `EDGE_CASES.md` | Planner | Builder, Tester | Failure modes and safe-null edge scenarios |
| `PLAN_ARCHITECTURE.md` | Planner | Builder, Architect | Design decisions and phase-to-module mapping |
| `FLOW_DIAGRAM.md` | Planner | Builder | ASCII call-flow diagram for server + gateway |

### Optional plan files

| File | Present? | Purpose |
|---|---|---|
| `ARCHITECTURE_PROPOSAL.md` | no | Not produced (no prior architect stage) |
| `EDGE_CASES.md` | yes | Failure modes and safe-null edge scenarios |
| `HAPPY_FLOW.md` | yes | Golden-path narrative |
| `FLOW_DIAGRAM.md` | yes | Multi-component interaction diagram |

---

## Codebase touchpoints

Files this goal creates (all new — greenfield project).

| Codebase file | Conversation | What changes |
|---|---|---|
| `pyproject.toml` | Conv 1 | CREATE: package definition, deps (mcp SDK), entry point |
| `src/codeintel/__init__.py` | Conv 1 | CREATE: version string, public re-exports |
| `src/codeintel/providers/__init__.py` | Conv 1 | CREATE: providers sub-package init |
| `src/codeintel/provider.py` | Conv 1 | CREATE: CodeProvider Protocol + Result TypedDict + safe-null envelope type |
| `src/codeintel/providers/none.py` | Conv 1 | CREATE: NoneProvider — always returns safe-null Result |
| `src/codeintel/gateway.py` | Conv 2 | CREATE: Gateway — provider registry, routing, safe-null wrapping |
| `src/codeintel/server.py` | Conv 2 | CREATE: MCP server — registers code.query + code.status tools, runs stdio loop |
| `tests/__init__.py` | Conv 3 | CREATE: test package init |
| `tests/test_never_raise.py` | Conv 3 | CREATE: fault-injection test suite proving never-raise contract |

> **All paths are new files.** Verify the project root (`/path/to/codeintel/`) before running the builder — no existing code to merge with.

---

## Conversation map

| Conv | Title | Stories | Status | Key files touched |
|---|---|---|---|---|
| 1 | Skeleton + Protocol + NoneProvider | S1.1, S1.2, S1.3 | TODO | `pyproject.toml`, `provider.py`, `providers/none.py` |
| 2 | Gateway + MCP Server | S1.4, S1.5 | TODO | `gateway.py`, `server.py` |
| 3 | Fault-injection test suite | S1.6 | TODO | `tests/test_never_raise.py` |

---

## Feedback files (transient — deleted after resolution)

Live in `pathly/features/01-mcp-skeleton/feedback/`. A file existing = issue open.

| File | Written by | Resolved by |
|---|---|---|
| `REVIEW_FAILURES.md` | Reviewer | Builder |
| `TEST_FAILURES.md` | Tester | Builder |
| `IMPL_QUESTIONS.md` | Builder [REQ] | Planner |
| `DESIGN_QUESTIONS.md` | Builder [ARCH] | Architect |
| `HUMAN_QUESTIONS.md` | Any agent | User |
