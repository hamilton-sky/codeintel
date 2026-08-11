# 08-http-transport — Feature Index

> **Read this first.** Every agent working on this feature should load this file before any other plan file.
> It maps every file in this folder so you can fetch only what you need in one read.

---

## Plan files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | Planner | All agents | This file — single entry point for feature context |
| `USER_STORIES.md` | Planner | Tester, Reviewer | Acceptance criteria — the contract |
| `IMPLEMENTATION_PLAN.md` | Planner | Builder, Architect | Phase-by-phase design — the what and how; each phase becomes one board task |
| `HAPPY_FLOW.md` | Planner | Builder, Tester | Golden-path narrative for each phase |
| `EDGE_CASES.md` | Planner | Builder, Tester | Failure modes and risk scenarios |
| `PLAN_ARCHITECTURE.md` | Planner | Builder, Architect | Design decisions mapped to phases |
| `FLOW_DIAGRAM.md` | Planner | Builder | ASCII call-flow diagram |

### Optional plan files (present if signals fired)

| File | Present? | Purpose |
|---|---|---|
| `ARCHITECTURE_PROPOSAL.md` | no | Cross-layer design decisions (architect stage) |
| `EDGE_CASES.md` | yes | Failure modes and risk scenarios |
| `HAPPY_FLOW.md` | yes | Golden-path narrative |
| `FLOW_DIAGRAM.md` | yes | Multi-component interaction diagram |

---

## Codebase touchpoints

Files in the live repo that this feature reads or modifies.

| Codebase file | Conversation | What changes |
|---|---|---|
| `src/codeintel/http_server.py` | Conv 1 | CREATE — HTTP server module wrapping existing handlers |
| `src/codeintel/__main__.py` | Conv 1 | MODIFY — add `serve-http` subcommand |
| `tests/test_http_server.py` | Conv 1 | CREATE — HTTP endpoint tests |
| `src/codeintel/server.py` | Conv 1 | READ-ONLY — reuse `code_query_handler` and `code_status_handler` |
| `src/codeintel/provider.py` | Conv 1 | READ-ONLY — `Result` TypedDict shape reference |

> **Verify these paths exist before editing.** Glob each one. If a path is wrong, correct it before proceeding.

---

## Conversation map

| Conv | Title | Stories | Status | Key files touched |
|---|---|---|---|---|
| 1 | HTTP server + CLI + tests | S1, S2, S3 | TODO | `src/codeintel/http_server.py`, `src/codeintel/__main__.py`, `tests/test_http_server.py` |

---

## Feedback files (transient — deleted after resolution)

Live in `pathly/features/08-http-transport/feedback/`. A file existing = issue open.

| File | Written by | Resolved by |
|---|---|---|
| `REVIEW_FAILURES.md` | Reviewer | Builder |
| `TEST_FAILURES.md` | Tester | Builder |
| `IMPL_QUESTIONS.md` | Builder [REQ] | Planner |
| `DESIGN_QUESTIONS.md` | Builder [ARCH] | Architect |
| `HUMAN_QUESTIONS.md` | Any agent | User |
