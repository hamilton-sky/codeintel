# Goal Index — Debounced fire-and-forget incremental reindex seam

> Goal: implement `maybe_reindex(project_root)` — a debounced, fire-and-forget call that
> incrementally reindexes both graph + semantic indexes in a background thread, triggered
> on every query and gated by the `reindex` config key.

## Plan Files

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| FEATURE_INDEX.md | planner | builder | Entry point: all touchpoints and phase map |
| USER_STORIES.md | planner | builder, reviewer | Acceptance criteria and user stories |
| IMPLEMENTATION_PLAN.md | planner | builder | Phase-by-phase builder prompts |
| HAPPY_FLOW.md | planner | builder, reviewer | Ideal runtime path end-to-end |
| EDGE_CASES.md | planner | builder, reviewer | Error paths, race conditions, config gates |
| PLAN_ARCHITECTURE.md | planner | builder, architect | Design decisions and phase mapping |
| FLOW_DIAGRAM.md | planner | builder | ASCII flow of the debounce + thread lifecycle |

## Codebase Touchpoints

| File | Conv | Change |
|------|------|--------|
| `src/codeintel/reindexer.py` | 1 | **CREATE** — `Reindexer` class with `maybe_reindex()`, debounce state, thread pool |
| `src/codeintel/gateway.py` | 1 | **MODIFY** — add `reindexer` param to `__init__`; call `maybe_reindex` in `query()` |
| `src/codeintel/server.py` | 1 | **MODIFY** — create module-level `Reindexer` singleton; pass to `_build_gateway()` |
| `tests/test_reindexer.py` | 2 | **CREATE** — unit tests for debounce, off-thread, config gate, never-raise |
| `tests/test_gateway.py` | 2 | **MODIFY** — add integration test: gateway calls reindexer on `query()` |

## Conversation Map

| Conv | Phases | Focus | Done when |
|------|--------|-------|-----------|
| 1 | 1–4 | Core reindexer + gateway + server wiring | `maybe_reindex` fires in background; gateway calls it; `reindex=off` disables |
| 2 | 5–6 | Tests | All new tests pass; `pytest tests/test_reindexer.py tests/test_gateway.py` green |

## Optional Plan Files

| File | Included |
|------|----------|
| HAPPY_FLOW.md | yes |
| EDGE_CASES.md | yes |
| PLAN_ARCHITECTURE.md | yes |
| FLOW_DIAGRAM.md | yes |
