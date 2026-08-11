# F9 — Docs + test suite · Implementation Plan

## Overview

The codeintel implementation is functionally complete (60/61 tests passing). This goal ships the remaining documentation and test coverage to satisfy the F9 acceptance criteria: a new user can install and query in <5 min from the README, and CI runs the safety + incremental + ranked-result suites green.

Three conversations: (1) fix the one failing test and strengthen the never-raise suite, (2) add e2e smoke + CI config, (3) write the agent-first README and per-engine docs.

## Layer Architecture

```
Feature goal deliverables
          │
          ├── Tests (Conv 1-2)
          │     ├── tests/test_semantic_provider.py  [fix]
          │     ├── tests/test_never_raise.py        [expand]
          │     ├── tests/test_e2e.py                [create]
          │     └── .github/workflows/ci.yml         [create]
          │
          └── Docs (Conv 3)
                ├── README.md                        [rewrite]
                └── docs/{graph,lsp,semantic}.md     [create]
```

## Prerequisites

- All of F1–F8 must be implemented (they are — the code exists in `src/codeintel/`).
- `python -m pytest tests/ -q` runs and produces output (it does — 60/61 passing).
- `fastembed` and `sqlite-vec` are installed in the dev environment.

## Phase 1: Fix Failing Semantic Provider Test

**File:** `tests/test_semantic_provider.py` — MODIFY: fix `test_search_returns_matches`
**Done when:** `python -m pytest tests/test_semantic_provider.py -q` exits 0 with no failures.
**Delivers stories:** S2.2, S2.3
**Depends on:** nothing (the test file already exists)
**Enables:** Phase 2 (clean baseline before expanding never-raise suite)

**Details:**

The test `test_search_returns_matches` is currently failing. Investigate `src/codeintel/providers/semantic.py` and `src/codeintel/searcher.py` to find why `SemanticProvider.build_result("search", "greet function", ...)` returns `result: None` after indexing a file containing `def greet()`. Likely causes: (a) `_FakeTextEmbedding.embed()` returns numpy arrays but the searcher or DB expects a different vector format, (b) the searcher's cosine floor is too high relative to the fake vectors, or (c) the `_DB_PATH` monkeypatch is not intercepting the right attribute.

Fix the test or fix the implementation — whichever is wrong. Do NOT touch any other test files in this phase. Do NOT touch `tests/test_never_raise.py` yet.

Recovery: if the fix requires changes in `src/codeintel/searcher.py` or `src/codeintel/providers/semantic.py`, ensure the other 60 tests still pass before declaring done.

**Verify:** `python -m pytest tests/test_semantic_provider.py -q`

---

## Phase 2: Expand Never-Raise Invariant Suite

**File:** `tests/test_never_raise.py` — MODIFY: add groups 9–12 (HTTP, graph, lsp, semantic paths)
**Done when:** `python -m pytest tests/test_never_raise.py -v` passes all tests and covers HTTP, GraphProvider, LspProvider, SemanticProvider, and full envelope shape assertions.
**Delivers stories:** S1.1, S1.2, S1.3
**Depends on:** Phase 1 (clean baseline)
**Enables:** Phase 3 (trustworthy invariant suite gates the e2e smoke)

**Details:**

The current `tests/test_never_raise.py` covers NoneProvider and Gateway. Add four new groups:

- **Group 9 — GraphProvider never-raise:** Call `GraphProvider().build_result(None, None, None, None, None)` and wrong-type args; assert `ok: true`. No backend needed — monkeypatch `shutil.which` to return None.
- **Group 10 — LspProvider never-raise:** Same pattern with `LspProvider`; monkeypatch `shutil.which` to return None.
- **Group 11 — SemanticProvider never-raise:** Call `SemanticProvider().build_result(None, None, None, None, None)` with `_DEPS_OK = False`; assert `ok: true`. Also inject a `RuntimeError` into `SemanticDb.init` and assert `ok: true`.
- **Group 12 — HTTP handler never-raise:** Import `_Handler` from `codeintel.http_server`; call the `_handle_query` helper (or equivalent) with a body that raises a `RuntimeError`; assert the response still has `ok: true`. If no testable helper exists, monkeypatch `Gateway.query` to raise and POST to a live test server.
- **Group 13 — Envelope shape:** For each provider and gateway, assert the returned dict always has ALL of: `ok`, `op`, `target`, `result`, `engine`, `cached`. Never assert `ok: false`.

Do NOT change the existing 8 test groups. Append only.

Do NOT touch `tests/test_semantic_provider.py`, `tests/test_reindexer.py`, or other test files.

**Verify:** `python -m pytest tests/test_never_raise.py -v`

---

## Phase 3: E2e Smoke Test

**File:** `tests/test_e2e.py` — CREATE: end-to-end smoke on fixture-based repo
**Done when:** `python -m pytest tests/test_e2e.py -v` passes all tests; the smoke indexes a fixture directory and asserts at least one `path:line` match is returned.
**Delivers stories:** S3.1, S3.2, S3.3, S3.4
**Depends on:** Phase 1 (semantic provider search must work correctly)
**Enables:** Phase 4 (CI workflow runs this file)

**Details:**

Create `tests/test_e2e.py` with a pytest fixture `fixture_repo(tmp_path)` that writes 3–5 Python source files covering different topics (e.g., an auth module, a parser module, a DB module). Use `_FakeTextEmbedding` (import from `test_semantic_provider`) OR re-define it inline — do NOT import from a test module if Python path resolution would make that fragile; inline is safer.

Write these tests:

1. **`test_e2e_search_returns_ranked_result`** — index the fixture repo, search for a domain term (e.g., "authenticate user"), assert `result` is not None and contains a filename match.
2. **`test_e2e_gateway_lsp_never_raises`** — call `Gateway().query(op="symbol", target="any", engine="lsp")` with no LSP binary (monkeypatch `shutil.which` → None); assert `ok: true` regardless.
3. **`test_e2e_gateway_graph_never_raises`** — same pattern for `engine="graph"`, assert `ok: true`.
4. **`test_e2e_full_pipeline_ok_envelope`** — call the full pipeline (`Gateway` with all default providers) with a nonsense target; assert the envelope has `ok`, `result`, `engine`, `cached` keys.

Patch `fastembed.TextEmbedding` with the fake in all semantic tests. Monkeypatch `shutil.which` where the real binary is needed. Use `monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)` to enable the semantic provider without a real model.

Do NOT add real network calls. Do NOT require a real installed LSP or graph backend.

**Verify:** `python -m pytest tests/test_e2e.py -v`

---

## Phase 4: CI Config

**File:** `.github/workflows/ci.yml` — CREATE: GitHub Actions CI pipeline
**File:** `pyproject.toml` — MODIFY: add `[project.optional-dependencies]` dev section if missing
**Done when:** The workflow YAML is syntactically valid, triggers on `push` + `pull_request`, installs the package, and runs `pytest`. Confirm by running `python -m pytest tests/ -q` locally to ensure the full suite is green before considering CI "ready".
**Delivers stories:** S4.1, S4.2, S4.3
**Depends on:** Phase 3 (all tests must be green locally)
**Enables:** Phase 5 (docs can reference the CI badge)

**Details:**

Create `.github/workflows/ci.yml`:
```yaml
name: CI
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: pip install -e ".[dev]"
      - name: Test
        run: pytest tests/ -q
```

Add a dev dependency group to `pyproject.toml` if it does not already have one:
```toml
[project.optional-dependencies]
dev = ["pytest>=8", "numpy>=1.24"]
```

Note: `fastembed` is already a runtime dependency, so it installs with the package. The CI test will use the `_FakeTextEmbedding` monkeypatch so the real model is NOT downloaded in CI.

Do NOT add `--no-header`, `--tb=short`, or any flags that hide failure information. Let pytest output be verbose by default.

If the full `pytest tests/ -q` run is still red after Phases 1–3, stop here and report. Do not commit a broken CI config.

**Verify:** `python -m pytest tests/ -q` (full suite, local)

---

## Phase 5: Agent-First README

**File:** `README.md` — REWRITE: agent-first framing, install + quickstart in first screenful
**Done when:** `README.md` passes the 5-min test: a reader can copy-paste the install command, run a query, and understand the safe-null contract without scrolling past the first two sections. Verify manually.
**Delivers stories:** S5.1, S5.2, S5.3, S5.4, S5.5
**Depends on:** Phase 4 (need the CI badge URL)
**Enables:** Phase 6 (per-engine docs link back to the README quickstart)

**Details:**

Rewrite `README.md` (currently a placeholder: "Status: spec'd, not yet built"). Structure:

```
# codeintel

[CI badge] [Python 3.11+]

One MCP server. Three engines. Any coding agent gets graph-breadth, LSP-precision,
and semantic search over any codebase — local-only, no API keys, never crashes an agent.

## Quickstart (< 5 min)
  pip install codeintel
  codeintel install --agent claude   # or codex, gemini, zed, all
  codeintel index .                  # build index for current repo
  codeintel query --op search --target "where is auth handled"

## How it works
[3-sentence arch summary: gateway → three providers → safe-null]

## Safe-null contract
[1 paragraph: every call returns {ok, result, engine, cached}; result is null on miss,
never an exception — agent falls back to grep, never crashes]

## Engines
| Engine | Best for | Install |
|--------|----------|---------|
| graph  | blast-radius, callers, call chains | (bundled via codebase-memory-mcp) |
| lsp    | fresh definition + references | uvx serena (auto-launched) |
| semantic | "where is X" natural-language search | bundled (fastembed) |

[Link each to docs/graph.md, docs/lsp.md, docs/semantic.md]

## CLI reference
  codeintel install | index | serve | query | status | map

## Config (.codeintel.toml)
[Key config keys only — backend, semantic, reindex]

## For agents
[The typical agent loop: status → index → search/impact/symbol → map]

## Development
  git clone ...
  pip install -e ".[dev]"
  pytest tests/
```

Do NOT add marketing language, badges beyond CI, or filler content. Keep total length ≤ 150 lines.
Do NOT delete the "codeintel (working name)" comment — replace it with the actual content.

**Verify:** Read the file; confirm first 30 lines contain install command and a working query.

---

## Phase 6: Per-Engine Docs

**File:** `docs/graph.md` — CREATE: GraphProvider reference
**File:** `docs/lsp.md` — CREATE: LspProvider reference
**File:** `docs/semantic.md` — CREATE: SemanticProvider reference
**Done when:** All three files exist, each ≤ 150 lines, and each accurately describes the ops, `reason` values, and install prereqs derivable from the source code in `src/codeintel/providers/`.
**Delivers stories:** S6.1, S6.2, S6.3, S6.4
**Depends on:** Phase 5 (README links to these)
**Enables:** F9 acceptance criteria fully met

**Details:**

Create a `docs/` directory if it does not exist. Write three files:

**`docs/graph.md`** — Source truth: `src/codeintel/providers/graph.py`
- Supported ops: `callers`, `callees`, `impact`, `chain`, `pattern`, `overview` (map to `query_graph` MCP methods)
- Install prereq: `codebase-memory-mcp` binary on PATH (detected via `shutil.which`)
- `reason` values: `engine-unavailable` (binary absent), `project-not-indexed` (list_projects returns empty), `unsupported-op`, `provider-error`
- Example: `codeintel query --op impact --target parse_result`

**`docs/lsp.md`** — Source truth: `src/codeintel/providers/lsp.py`
- Supported ops: `symbol`, `overview`
- State machine: IDLE → WARMING (thread boots) → READY (live) or FAILED (boot error) → cooldown → retry
- `reason` values: `engine-unavailable` (uvx absent), `warming` (session booting), `boot-failed` (in cooldown), `unsupported-op`
- First call may return `null` with `reason: warming`; subsequent calls return real data
- Install prereq: `uvx` on PATH (Serena launched automatically)
- Example: `codeintel query --op symbol --target Gateway`

**`docs/semantic.md`** — Source truth: `src/codeintel/providers/semantic.py`, `src/codeintel/indexer.py`, `src/codeintel/searcher.py`
- Supported ops: `search` only
- Indexing: content-hash-keyed; unchanged files skipped; walks `.py .ts .js .go .rs .java .c .cpp .h .md`; 20-line window, 10-line stride; cap = 500 chunks/file
- Embedding model: `BAAI/bge-small-en-v1.5` via fastembed; lazy-loaded on first index
- Search: KNN over sqlite-vec; weak matches floored (below cosine threshold → `reason: below-floor`)
- `reason` values: `op-not-supported`, `below-floor`, `empty-index`
- Install prereq: `fastembed` + `sqlite-vec` (already in package dependencies)
- Example: `codeintel query --op search --target "where is authentication handled"`

Do NOT invent behaviour not in the source. Read each provider file before writing its doc.

Do NOT touch source files, test files, or `README.md` in this phase.

**Verify:** `ls docs/` shows all three files; `wc -l docs/*.md` shows each ≤ 150 lines.

---

## Key Decisions

- **Fix-before-expand:** Phase 1 fixes the one failing test before Phase 2 expands the suite — prevents masking regressions introduced by expansion.
- **Inline fake embedder:** E2e tests define `_FakeTextEmbedding` inline rather than importing from another test module — avoids fragile cross-test-module imports.
- **CI uses monkeypatching:** Real fastembed model is NOT downloaded in CI; all semantic tests use the fake embedder — keeps CI fast and dependency-free.
- **Docs read source:** Each per-engine doc is written by reading the provider's source file, not from memory — ensures accuracy.
