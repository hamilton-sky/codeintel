# Feature Index — F4: Unified Gateway (Engine Selector)

## Plan Files

| File | Written By | Read By | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | planner | all agents | Entry point: all plan files + codebase touchpoints |
| `USER_STORIES.md` | planner | builder, tester | Stories and acceptance criteria |
| `IMPLEMENTATION_PLAN.md` | planner | builder | Phased build instructions (authoritative builder prompt) |
| `HAPPY_FLOW.md` | planner | builder, reviewer | Ideal call flow end-to-end |
| `EDGE_CASES.md` | planner | builder, reviewer | Failure modes + never-raise invariant cases |
| `PLAN_ARCHITECTURE.md` | planner | builder, architect | Design decisions scoped to this feature + phase map |
| `FLOW_DIAGRAM.md` | planner | builder | ASCII diagram of engine-selection routing |

## Codebase Touchpoints

| File | Conv | What Changes |
|---|---|---|
| `src/codeintel/gateway.py` | 1, 2 | Rewrite: honor engine param, add engine router, fan-out merge |
| `src/codeintel/server.py` | 1, 3 | Pass engine+role through; register SemanticProvider |
| `src/codeintel/providers/semantic.py` | 1 | New: SemanticProvider placeholder (always engine-unavailable) |
| `src/codeintel/cache.py` | 2 | New: ContentHashCache (content-hash-keyed result cache) |
| `src/codeintel/policy.py` | 3 | New: TieringPolicy (off-by-default role/op restriction) |
| `tests/test_gateway.py` | 3 | New: gateway routing, fan-out, cache, tiering tests |

## Conversation Map

| Conv | Phases | Scope | Depends On |
|---|---|---|---|
| 1 | 1–3 | Engine router + SemanticProvider placeholder + server wiring | F3 complete |
| 2 | 4–6 | Fan-out merge (both/all) + content-hash cache | Conv 1 |
| 3 | 7–9 | Role/op tiering policy + server role param + gateway tests | Conv 2 |

## Optional Plan Files

| File | Included |
|---|---|
| `HAPPY_FLOW.md` | yes |
| `EDGE_CASES.md` | yes |
| `PLAN_ARCHITECTURE.md` | yes |
| `FLOW_DIAGRAM.md` | yes |
