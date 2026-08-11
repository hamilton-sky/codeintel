# F9 — Docs + test suite · Plan Architecture

Design decisions for the docs + test suite goal, mapped to implementation phases.

---

## Key Decisions

**D1 — Fix-before-expand (Phases 1→2):** The one failing test must be fixed before expanding the never-raise suite. Adding new test groups on a broken baseline creates ambiguity about which failure is real. Fixing first establishes a clean 61/61 baseline, then expansion is additive.

**D2 — Inline fake embedder in e2e (Phase 3):** The `_FakeTextEmbedding` class is re-defined inline in `tests/test_e2e.py` rather than imported from `tests/test_semantic_provider.py`. Importing across test modules creates fragile cross-module dependencies that break on pytest path resolution changes. Inline duplication is deliberate here.

**D3 — No real model in CI (Phase 4):** fastembed downloads model weights (>100MB) on first use. All semantic tests in CI must monkeypatch `fastembed.TextEmbedding` with the fake. This means CI tests the indexing/search pipeline logic, not the model itself — acceptable for CI; real model can be tested locally.

**D4 — Docs read source, not memory (Phase 6):** Each per-engine doc is written by reading the provider's source file, not from spec or memory. This prevents the common doc-vs-code drift where `reason` values, op names, or constants differ between docs and implementation.

**D5 — README uses real install commands (Phase 5):** The README must show a working install command, not a placeholder. If the package is not yet on PyPI, use `pip install -e .`. Do NOT write `pip install codeintel` if it would 404.

---

## Phase Mapping

### Phase 1: Fix Failing Test
- No architectural change. Fix is confined to `tests/test_semantic_provider.py` (test fix) or `src/codeintel/` (impl fix).
- If the fix is in `src/`: it is a bug fix to an existing module, not a new feature. The module's interface does not change.

### Phase 2: Expand Never-Raise Suite
- Append-only: 5 new test groups added to `tests/test_never_raise.py`.
- No changes to source code. If a provider raises during test, that is the bug to fix — do NOT work around it with try/except in the test.

### Phase 3: E2e Smoke Test
- New file: `tests/test_e2e.py`. No changes to source.
- The e2e test drives the full public interface (`Gateway`, `SemanticProvider`, provider chain) — it is an integration test, not a unit test. It is slower than unit tests and may be tagged with `@pytest.mark.e2e` if desired (not required for Phase 3).

### Phase 4: CI Config
- New file: `.github/workflows/ci.yml`. Modify `pyproject.toml` (add `dev` optional group).
- CI runs `pytest tests/ -q` — the whole suite. No selective skipping.

### Phases 5–6: Docs
- New/modified files: `README.md`, `docs/graph.md`, `docs/lsp.md`, `docs/semantic.md`.
- No changes to source or tests.
- The `docs/` directory is a new top-level directory. This is intentional: keeping engine-specific docs in `docs/` separate from the project root makes them easy to link from the README without cluttering the root.
