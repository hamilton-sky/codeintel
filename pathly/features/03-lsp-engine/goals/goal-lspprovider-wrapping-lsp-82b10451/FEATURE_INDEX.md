# FEATURE_INDEX — LspProvider (F3 LSP Engine Adapter)

Goal: `LspProvider` wrapping the LSP-over-MCP bridge — always-fresh symbol/overview with async warm-up.

---

## Plan Files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | planner | all | Entry point — plan map, codebase touchpoints, conversation map |
| `USER_STORIES.md` | planner | builder | Stories + acceptance criteria per op |
| `IMPLEMENTATION_PLAN.md` | planner | builder | Phase-by-phase build spec; source of the board task DAG |
| `HAPPY_FLOW.md` | planner | builder | Ideal warm-up and query journey |
| `EDGE_CASES.md` | planner | builder | Failure modes and invariant guards |
| `PLAN_ARCHITECTURE.md` | planner | builder/architect | Design decisions scoped to this feature; phase mapping |
| `FLOW_DIAGRAM.md` | planner | builder | ASCII state machine and call flow |

---

## Codebase Touchpoints

All paths are relative to the project root `/Users/shammaihamilton/Documents/project/codeintel`.

| File | Status | Conversation | What changes |
|---|---|---|---|
| `src/codeintel/providers/lsp.py` | CREATE | Conv 1 | New LspProvider: session state machine, async warm-up thread, `build_result` dispatch |
| `src/codeintel/server.py` | MODIFY | Conv 2 | Wire LspProvider into `_build_providers()` and `code_status_handler()` |
| `tests/test_lsp_provider.py` | CREATE | Conv 2 | Test suite: never-raise invariant, state machine, op dispatch |

---

## Conversation Map

| Conv | Phases | Key file(s) | Done when |
|---|---|---|---|
| 1 | 1 | `src/codeintel/providers/lsp.py` | LspProvider passes never-raise and state-machine tests in isolation |
| 2 | 2–3 | `src/codeintel/server.py`, `tests/test_lsp_provider.py` | `pytest tests/test_lsp_provider.py` green; status handler reports lsp |

---

## Optional Plan Files

| File | Included |
|---|---|
| `HAPPY_FLOW.md` | yes |
| `EDGE_CASES.md` | yes |
| `PLAN_ARCHITECTURE.md` | yes |
| `FLOW_DIAGRAM.md` | yes |
