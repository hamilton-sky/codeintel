# Feature Index — GraphProvider (F2)

Goal: GraphProvider wrapping the code-graph backend: impact/callers/callees/chain/pattern/overview.
Rigor: standard

---

## Plan Files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | planner | builder, reviewer | Entry point: touchpoints + conversation map |
| `USER_STORIES.md` | planner | builder, reviewer | Stories + acceptance criteria |
| `IMPLEMENTATION_PLAN.md` | planner | builder | Phase-by-phase build instructions |
| `HAPPY_FLOW.md` | planner | builder, reviewer | Ideal request-to-result flow |
| `EDGE_CASES.md` | planner | builder, reviewer | Failure modes + safe-null scenarios |
| `PLAN_ARCHITECTURE.md` | planner | builder, architect | Design decisions mapped to phases |
| `FLOW_DIAGRAM.md` | planner | builder | ASCII call-flow diagram |

---

## Codebase Touchpoints

| Source File | Conversation | Change |
|---|---|---|
| `src/codeintel/providers/graph.py` | Conv 1 | **CREATE** — GraphProvider class |
| `src/codeintel/providers/__init__.py` | Conv 1 | No change required; graph.py is imported directly |
| `src/codeintel/server.py` | Conv 2 | **MODIFY** — auto-include GraphProvider; forward project_root; update code.status |
| `src/codeintel/gateway.py` | Conv 2 | **MODIFY** — ensure engine routing passes project_root properly |
| `tests/test_graph_provider.py` | Conv 3 | **CREATE** — never-raise + backend-absent + timeout + op-dispatch tests |

---

## Conversation Map

| Conv | Phases | Files touched | Done when |
|---|---|---|---|
| Conv 1 | Phase 1–3 | `providers/graph.py` | `GraphProvider().build_result(...)` returns ok=True for every input whether backend is installed or not |
| Conv 2 | Phase 4–5 | `server.py`, `gateway.py` | `code.status` reports `graph` engine; `code.query` routes to GraphProvider with project_root; server starts cleanly |
| Conv 3 | Phase 6 | `tests/test_graph_provider.py` | `pytest tests/test_graph_provider.py -v` passes green |

---

## Optional Plan Files

| File | Included |
|---|---|
| `HAPPY_FLOW.md` | yes |
| `EDGE_CASES.md` | yes |
| `PLAN_ARCHITECTURE.md` | yes |
| `FLOW_DIAGRAM.md` | yes |
