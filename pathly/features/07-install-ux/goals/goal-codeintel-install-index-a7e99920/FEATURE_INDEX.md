# codeintel install/index/query/status CLI — Feature Index

> **Read this first.** Every agent working on this feature should load this file before any other plan file.
> It maps every file in this folder so you can fetch only what you need in one read.

---

## Plan files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | Planner | All agents | This file — single entry point for feature context |
| `USER_STORIES.md` | Planner | Tester, Reviewer | Acceptance criteria — the contract |
| `IMPLEMENTATION_PLAN.md` | Planner | Builder, Architect | Phase-by-phase design — the what and how; each phase becomes one board task |
| `HAPPY_FLOW.md` | Planner | Builder, Tester | Golden-path narrative end-to-end |
| `EDGE_CASES.md` | Planner | Builder, Reviewer, Tester | Failure modes and risk scenarios |
| `PLAN_ARCHITECTURE.md` | Planner | Builder, Architect | Plan-scoped architecture decisions and phase mapping |
| `FLOW_DIAGRAM.md` | Planner | Builder | ASCII flow for install + CLI subcommand dispatch |

### Optional plan files

| File | Present? | Purpose |
|---|---|---|
| `ARCHITECTURE_PROPOSAL.md` | no | — |
| `EDGE_CASES.md` | yes | Failure modes and risk scenarios |
| `HAPPY_FLOW.md` | yes | Golden-path narrative |
| `FLOW_DIAGRAM.md` | yes | Multi-component interaction diagram |

---

## Codebase touchpoints

Files in the live repo that this feature reads or modifies.

| Codebase file | Conversation | What changes |
|---|---|---|
| `src/codeintel/config.py` | Conv 1 | NEW — `ConfigLoader`; loads `.codeintel.toml` + `~/.codeintel/config.toml`, merges with defaults |
| `src/codeintel/__main__.py` | Conv 2 | EXTEND — add `index`, `query`, `status` subcommands wired to existing modules |
| `src/codeintel/server.py` | Conv 2 | MINOR — import `load_config` to read backend/semantic flags at startup |
| `src/codeintel/installer.py` | Conv 3 | NEW — per-agent self-registration logic (claude/codex/gemini/zed); idempotent JSON merge |
| `src/codeintel/__main__.py` | Conv 3 | EXTEND — add `install [--agent]` subcommand wiring to `Installer` |
| `pyproject.toml` | Conv 1 | VERIFY ONLY — `tomllib` is Python 3.11+ built-in, no new dep needed |

> **Verify these paths exist before editing.** Glob each one. If a path is wrong, correct it before proceeding.

---

## Conversation map

| Conv | Title | Phases | Status | Key files touched |
|---|---|---|---|---|
| 1 | Config module | Phase 1 | TODO | `src/codeintel/config.py` |
| 2 | CLI: index / query / status | Phase 2 | TODO | `src/codeintel/__main__.py`, `src/codeintel/server.py` |
| 3 | Install subcommand | Phases 3–4 | TODO | `src/codeintel/installer.py`, `src/codeintel/__main__.py` |

---

## Feedback files (transient — deleted after resolution)

Live in `pathly/features/07-install-ux/feedback/`. A file existing = issue open.

| File | Written by | Resolved by |
|---|---|---|
| `REVIEW_FAILURES.md` | Reviewer | Builder |
| `TEST_FAILURES.md` | Tester | Builder |
| `IMPL_QUESTIONS.md` | Builder [REQ] | Planner |
| `DESIGN_QUESTIONS.md` | Builder [ARCH] | Architect |
| `HUMAN_QUESTIONS.md` | Any agent | User |
