# F9 — Docs + test suite · User Stories

## Story S1: Never-raise invariant is tested and green

**As an** agent host running codeintel,
**I want** the never-raise contract verified across every provider and the HTTP layer,
**So that** no engine failure can crash an agent's prompt.

### Acceptance criteria
- S1.1: `tests/test_never_raise.py` covers NoneProvider, GraphProvider, LspProvider, SemanticProvider, Gateway, and HTTP handler — all with fault-injected exceptions and wrong-type args.
- S1.2: Every test in `tests/test_never_raise.py` passes with `pytest tests/test_never_raise.py`.
- S1.3: Every envelope returned by a fault-injected call has `ok: true` and `result: null` (never raises, never returns `ok: false`).

---

## Story S2: Indexer incrementality is tested and green

**As a** developer running repeated queries on an unchanged repo,
**I want** the indexer to skip unchanged files,
**So that** re-indexing is fast and the semantic database is not corrupted by duplicate rows.

### Acceptance criteria
- S2.1: `test_unchanged_repo_skips_embed` (in `tests/test_semantic_provider.py`) passes: first index returns > 0, second returns 0.
- S2.2: `test_search_returns_matches` passes: after indexing a file with `def greet()`, a search for "greet function" returns a result containing "sample.py".
- S2.3: The full `tests/test_semantic_provider.py` suite passes (currently 1 failing).

---

## Story S3: E2e smoke test runs on a real-ish repo fixture

**As a** CI system,
**I want** an end-to-end smoke test that indexes a fixture repo and asserts ranked results are returned,
**So that** the full gateway → provider → indexer → searcher pipeline is verified on every CI run.

### Acceptance criteria
- S3.1: `tests/test_e2e.py` exists and creates a fixture directory with several Python source files.
- S3.2: The smoke test calls `SemanticProvider.build_result("search", ...)` on the fixture repo and asserts `result` is not None and contains a `path:line` match.
- S3.3: The smoke test calls `gateway.query(op="symbol", engine="lsp")` and asserts `ok: true` (even if `result: null` because LSP may not be installed in CI — never-raise check).
- S3.4: All tests in `tests/test_e2e.py` pass with `pytest tests/test_e2e.py`.

---

## Story S4: CI pipeline runs the full suite on every push

**As a** contributor pushing to the repo,
**I want** a GitHub Actions workflow that runs `pytest` on every push and pull request,
**So that** regressions are caught automatically.

### Acceptance criteria
- S4.1: `.github/workflows/ci.yml` exists and runs `pip install -e ".[dev]"` + `pytest` on Python 3.11.
- S4.2: The workflow triggers on `push` and `pull_request` to any branch.
- S4.3: A failing test causes the workflow to exit non-zero (standard pytest behaviour — no special config needed).

---

## Story S5: Agent-first README lets a new user install and query in <5 min

**As a** new user (human or agent),
**I want** a README that shows me install + first query in the first screenful,
**So that** I can be productive in under 5 minutes without reading source code.

### Acceptance criteria
- S5.1: `README.md` opens with a one-sentence value prop (no preamble, no table of contents first).
- S5.2: The first major section is a quickstart with copy-paste install + a working query (no placeholders).
- S5.3: The README links to per-engine docs (`docs/graph.md`, `docs/lsp.md`, `docs/semantic.md`).
- S5.4: The README explains the never-raise / safe-null contract in one short paragraph.
- S5.5: A CI badge is included linking to the GitHub Actions workflow.

---

## Story S6: Per-engine docs give a concise reference for each engine

**As an** agent or developer calling `code.query`,
**I want** a short reference page per engine explaining ops, example calls, and what `null` means,
**So that** I can choose the right engine without reading source code.

### Acceptance criteria
- S6.1: `docs/graph.md` exists and covers: supported ops, example CLI call, what `engine-unavailable` means, install prerequisites.
- S6.2: `docs/lsp.md` exists and covers: supported ops, warm-up behaviour, `warming`/`boot-failed` reasons, install prerequisites.
- S6.3: `docs/semantic.md` exists and covers: supported ops, how indexing works, `below-floor` reason, install prerequisites and model.
- S6.4: Each doc is ≤ 150 lines and uses only information derivable from the source code.
