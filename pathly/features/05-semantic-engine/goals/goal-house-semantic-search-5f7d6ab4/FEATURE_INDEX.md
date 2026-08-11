# FEATURE INDEX — In-House Semantic Search (Goal: house-semantic-search)

Feature: `05-semantic-engine`  
Goal: In-house semantic search — local embeddings + sqlite-vec `code_embeddings` index + op=search KNN with a cosine floor.  
Rigor: standard  
Plan created: 2026-08-11

---

## Plan Files

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `FEATURE_INDEX.md` | planner | all agents | Entry point: codebase touchpoints + conversation map |
| `USER_STORIES.md` | planner | builder, tester | Acceptance criteria + user stories |
| `IMPLEMENTATION_PLAN.md` | planner | builder | Phase-by-phase build sequence |
| `HAPPY_FLOW.md` | planner | builder, tester | Ideal query → result journey |
| `EDGE_CASES.md` | planner | builder, tester | Failure modes + boundary conditions |
| `PLAN_ARCHITECTURE.md` | planner | builder, architect | Design decisions + module map |
| `FLOW_DIAGRAM.md` | planner | builder | ASCII flow diagram of the semantic pipeline |

---

## Codebase Touchpoints

| File | Conv / Phase | Change |
|---|---|---|
| `pyproject.toml` | Conv 1 / Phase 1 | Add `sqlite-vec` and `fastembed` to `dependencies` |
| `src/codeintel/semantic_db.py` | Conv 1 / Phase 1 | **CREATE** — DB init, `code_embeddings` vec0 table, `chunk_hashes` metadata table |
| `src/codeintel/indexer.py` | Conv 1 / Phase 2 | **CREATE** — content-hash-keyed line-window indexer |
| `src/codeintel/searcher.py` | Conv 2 / Phase 3 | **CREATE** — KNN search with cosine floor, returns ranked path:line + snippet |
| `src/codeintel/providers/semantic.py` | Conv 2 / Phase 4 | **REPLACE** — real SemanticProvider (replaces always-unavailable placeholder) |
| `tests/test_semantic_provider.py` | Conv 2 / Phase 5 | **CREATE** — end-to-end tests for op=search, dedup, safe-null, cosine floor |

---

## Conversation Map

| Conv | Phases | Description | Depends on |
|---|---|---|---|
| 1 | 1–2 | Foundation: DB schema + indexer | Feature 04-unified-gateway (already done) |
| 2 | 3–5 | Integration: searcher + real provider + tests | Conv 1 complete |

---

## Optional Plan Files

| File | Included |
|---|---|
| `HAPPY_FLOW.md` | yes |
| `EDGE_CASES.md` | yes |
| `PLAN_ARCHITECTURE.md` | yes |
| `FLOW_DIAGRAM.md` | yes |
