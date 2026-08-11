# F9 — Docs + test suite · Happy Flow

The golden path through each phase — what "everything worked" looks like.

---

## Phase 1: Fix Failing Semantic Provider Test

1. Builder reads `tests/test_semantic_provider.py` and `src/codeintel/providers/semantic.py`.
2. Builder identifies root cause: `_FakeTextEmbedding.embed()` returns generators or mismatched vector dimensions causing `Searcher.search()` to return empty.
3. Builder fixes the monkeypatch or the indexer's vector packing — whichever is wrong — without changing the test's intent.
4. `pytest tests/test_semantic_provider.py -q` exits 0. All 7 tests pass.

---

## Phase 2: Expand Never-Raise Invariant Suite

1. Builder reads `tests/test_never_raise.py` (8 existing groups) and `src/codeintel/providers/graph.py`, `lsp.py`, `semantic.py`.
2. Builder appends Groups 9–13 without touching groups 1–8.
3. `pytest tests/test_never_raise.py -v` exits 0. All groups pass.
4. Every new test asserts `ok: true`, never `ok: false`.

---

## Phase 3: E2e Smoke Test

1. Builder creates `tests/test_e2e.py` with a `fixture_repo` that writes auth.py, parser.py, db.py to `tmp_path`.
2. Inline `_FakeTextEmbedding` is used; `_DEPS_OK` is monkeypatched to True.
3. `test_e2e_search_returns_ranked_result` indexes the fixture and finds "auth" in the result.
4. `test_e2e_gateway_lsp_never_raises` and `test_e2e_gateway_graph_never_raises` both assert `ok: true` with no backends installed.
5. `pytest tests/test_e2e.py -v` exits 0. All 4 tests pass.

---

## Phase 4: CI Config

1. Builder verifies `pytest tests/ -q` is green (all 65+ tests passing after Phases 1–3).
2. Builder creates `.github/workflows/ci.yml` with the exact YAML from the plan.
3. Builder adds `dev` optional dependencies to `pyproject.toml`.
4. Local `python -m pytest tests/ -q` exits 0 — CI config is ready.

---

## Phase 5: Agent-First README

1. Builder reads the current `README.md` (placeholder) and the project SPEC.
2. Builder writes a new README with the quickstart in the first 30 lines.
3. The README contains: one-sentence value prop, `pip install codeintel`, `codeintel install --agent claude`, `codeintel query --op search --target "..."`, safe-null contract paragraph, per-engine table with links to `docs/*.md`, CI badge.
4. Manual review confirms: install + first query are visible without scrolling.

---

## Phase 6: Per-Engine Docs

1. Builder reads `src/codeintel/providers/graph.py`, `lsp.py`, `semantic.py` one by one.
2. Builder creates `docs/` directory and writes three files, each ≤ 150 lines.
3. Each file accurately names ops, `reason` values, and install prereqs from the source.
4. `ls docs/` shows `graph.md`, `lsp.md`, `semantic.md`; `wc -l docs/*.md` is ≤ 150 each.
