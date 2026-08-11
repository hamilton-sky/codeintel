# F9 — Docs + test suite · Edge Cases

Risk scenarios and failure modes per phase.

---

## Phase 1: Fix Failing Semantic Provider Test

**EC1.1 — Vector format mismatch:** `_FakeTextEmbedding.embed()` returns `np.full(384, 0.1, ...)` but the searcher packs the query vector differently. Fix: align the query embedding path in `Searcher.search()` with how the indexer packs chunks.

**EC1.2 — `_DB_PATH` monkeypatch race:** The monkeypatch may not intercept the right attribute if `SemanticProvider` reads `_DB_PATH` at import time rather than call time. Fix: verify the attribute is read lazily; if not, restructure `SemanticProvider.__init__` to accept `db_path` as a constructor arg for testability.

**EC1.3 — Fix breaks other tests:** Changing vector packing in `indexer.py` or `searcher.py` could break `test_unchanged_repo_skips_embed`. Run the full `tests/test_semantic_provider.py` suite after any change and stop if other tests regress.

**EC1.4 — Cosine floor too strict:** The cosine similarity between two identical 0.1-filled vectors is 1.0, so the floor should not be the issue. If it is, the floor threshold value is a bug — lower it or parameterise it, and document the change.

---

## Phase 2: Expand Never-Raise Invariant Suite

**EC2.1 — Import side effects:** `GraphProvider()` or `LspProvider()` constructor may trigger real subprocess calls. Monkeypatch `shutil.which` to return None for both before instantiation.

**EC2.2 — HTTP handler has no standalone testable helper:** If `_Handler` has no callable method without a live server, spin up a `CodeIntelHTTPServer` in a thread (same pattern as `test_http_server.py`) rather than calling the handler directly.

**EC2.3 — Envelope shape missing keys:** Some providers may omit `op` or `cached` from their result. The invariant test should check for presence of all keys, not their values. Use `assert "op" in r`, not `assert r["op"] == something`.

---

## Phase 3: E2e Smoke Test

**EC3.1 — Fixture file content matters:** The fixture must contain terms that appear in the search query. Use distinct domain terms per file (auth.py: `authenticate`, `token`; parser.py: `parse`, `grammar`; db.py: `connect`, `query`). Search for a term that appears in exactly one file so the match is unambiguous.

**EC3.2 — `fastembed` model download in CI:** If `_DEPS_OK` monkeypatching fails to intercept the real model load, CI will try to download the model. Guard: always monkeypatch `_DEPS_OK = True` AND patch `fastembed.TextEmbedding` with the fake in every semantic test. Do NOT rely on one without the other.

**EC3.3 — Concurrent tests share `tmp_path`:** Each pytest test function gets a unique `tmp_path`. Do NOT share the fixture directory across test functions via a session-scoped fixture — each test gets its own clean state.

**EC3.4 — Gateway constructor signature changed:** If `Gateway()` constructor was updated in F4, check `src/codeintel/gateway.py` before calling `Gateway()` with no args in the e2e test.

---

## Phase 4: CI Config

**EC4.1 — `fastembed` model download blocks CI:** `fastembed` downloads model weights on first use. CI tests must use the fake embedder — all semantic tests must monkeypatch `fastembed.TextEmbedding`. Run the full suite locally with the model absent to confirm.

**EC4.2 — `sqlite-vec` extension missing on CI runner:** `sqlite-vec` requires a compiled `.so`/`.dll` that may not be available as a pip wheel for all Python/platform combos. If the CI run fails on `import sqlite_vec`, pin to a known-good version or add a platform matrix skip.

**EC4.3 — `dev` dependency group conflicts:** Adding `pytest>=8` to `[project.optional-dependencies].dev` must not conflict with the existing `mcp>=1.0` or `fastembed>=0.3`. Verify with `pip install -e ".[dev]"` locally before committing.

---

## Phase 5: Agent-First README

**EC5.1 — Install command not yet valid:** `pip install codeintel` will fail if the package is not yet on PyPI. Replace with `pip install -e .` (development install) or `uvx codeintel` (if uvx-published), whichever is actually usable. Do NOT use a placeholder install command.

**EC5.2 — CI badge URL:** The badge URL must match the actual GitHub repo path and workflow name. Use the correct path or omit the badge if the repo URL is not yet confirmed.

**EC5.3 — Safe-null section must be accurate:** Do NOT paraphrase the contract from memory. Read `src/codeintel/gateway.py` to confirm the actual envelope shape (`ok`, `result`, `engine`, `cached`, `reason?`) before writing.

**EC5.4 — "Working name" caveat:** The product name is still "codeintel (working name)". The README should acknowledge this briefly or use the name without the caveat if it has been locked by this point.

---

## Phase 6: Per-Engine Docs

**EC6.1 — Ops list may be incomplete:** The graph provider maps ops to `query_graph` methods — read `src/codeintel/providers/graph.py` carefully for every `if op ==` branch. Missing an op is a doc bug.

**EC6.2 — `reason` values drift:** The `reason` strings in docs must match the exact strings in source code. Copy them verbatim (e.g., `"engine-unavailable"`, not `"engine_unavailable"`).

**EC6.3 — LSP state machine is complex:** The `_State` enum and session lifecycle in `src/codeintel/providers/lsp.py` must be read completely before writing `docs/lsp.md`. Do NOT invent state transitions not in the source.

**EC6.4 — Semantic doc must match indexer constants:** `window=20`, `stride=10`, `max_chunks=500`, extensions list, skip dirs — read `src/codeintel/indexer.py` for the actual values before writing `docs/semantic.md`.
