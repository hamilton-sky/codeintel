# F9 — Docs + test suite · Goal: README (agent-first) · Feature Index

> **Read this first.** Every agent working on this goal should load this file before any other plan file.
> It maps every plan file and every codebase touchpoint so you fetch only what you need.

---

## Plan files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | Planner | All agents | This file — single entry point for goal context |
| `USER_STORIES.md` | Planner | Tester, Reviewer | Acceptance criteria — the contract |
| `IMPLEMENTATION_PLAN.md` | Planner | Builder, Architect | Phase-by-phase design — each phase = one board task |
| `HAPPY_FLOW.md` | Planner | Builder | Golden-path narrative |
| `EDGE_CASES.md` | Planner | Builder, Tester | Failure modes and risk scenarios |
| `PLAN_ARCHITECTURE.md` | Planner | Builder, Architect | Design decisions scoped to this goal |
| `FLOW_DIAGRAM.md` | Planner | Builder | ASCII flow showing test + doc layers |

### Optional plan files

| File | Present? | Purpose |
|---|---|---|
| `ARCHITECTURE_PROPOSAL.md` | no | Cross-layer design (not needed — goal is docs/tests only) |
| `EDGE_CASES.md` | yes | Test gaps and doc edge cases |
| `HAPPY_FLOW.md` | yes | Normal pass for each phase |
| `FLOW_DIAGRAM.md` | yes | Test/doc layer diagram |

---

## Codebase touchpoints

Files in the live repo that this goal reads or modifies.

| Codebase file | Conv | What changes |
|---|---|---|
| `tests/test_semantic_provider.py` | 1 | Fix `test_search_returns_matches` (currently failing) |
| `tests/test_never_raise.py` | 1 | Expand to cover HTTP server, all providers, full envelope shape |
| `tests/test_e2e.py` | 2 | CREATE — e2e smoke on fixture-based repo |
| `.github/workflows/ci.yml` | 2 | CREATE — CI pipeline running pytest |
| `pyproject.toml` | 2 | Add pytest config section if missing |
| `README.md` | 3 | Full rewrite — agent-first framing, install + quickstart |
| `docs/graph.md` | 3 | CREATE — GraphProvider reference |
| `docs/lsp.md` | 3 | CREATE — LspProvider reference |
| `docs/semantic.md` | 3 | CREATE — SemanticProvider reference |

> **Verify these paths exist before editing.** Glob each one before touching it.

---

## Conversation map

| Conv | Title | Phases | Status | Key files |
|---|---|---|---|---|
| 1 | Fix & Strengthen Test Suite | 1–2 | TODO | `tests/test_semantic_provider.py`, `tests/test_never_raise.py` |
| 2 | E2e Smoke + CI | 3–4 | TODO | `tests/test_e2e.py`, `.github/workflows/ci.yml` |
| 3 | Docs | 5–6 | TODO | `README.md`, `docs/*.md` |

---

## Feedback files (transient — deleted after resolution)

Live in `pathly/features/09-docs-tests/feedback/`.

| File | Written by | Resolved by |
|---|---|---|
| `REVIEW_FAILURES.md` | Reviewer | Builder |
| `TEST_FAILURES.md` | Tester | Builder |
| `IMPL_QUESTIONS.md` | Builder [REQ] | Planner |
| `HUMAN_QUESTIONS.md` | Any agent | User |
