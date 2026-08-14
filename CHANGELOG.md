# Changelog

All notable changes to codeintel are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.1] — 2026-08-14

### Added
- **Graph viewer: hover any node metric for a plain-English definition.** The detail-panel numbers
  (complexity / cognitive / callers-in / calls-out) now explain themselves — cyclomatic vs cognitive
  complexity, fan-in vs fan-out — so a decorator showing "complexity 0 / 39 callers" is no longer a
  mystery. Uses a reliable custom tooltip (native `title` silently skipped adjacent cells — it only
  re-fires after a fresh mouse "rest").

### Docs
- README now shows both "see your code" surfaces — the interactive call graph and the `CODE_INTEL.md`
  map — and explains what the map file is for (a static, committable orientation snapshot for agents
  or hosts that don't speak MCP, plus the `--inject` flow into `CLAUDE.md`/`AGENTS.md`).

## [0.11.0] — 2026-08-14

### Added
- **`codeintel graph` — see any codebase as an interactive call graph.** `codeintel graph <repo>`
  emits the graph engine's structure as `{nodes, edges}` **JSON** (machine-readable — the data→renderer
  contract); `codeintel graph <repo> --html` wraps it in a **single self-contained HTML viewer** (data
  embedded, zero external deps, opens offline in any browser). The viewer is a force-directed graph with
  four **layouts** (force / radial / layered / module-clustered), click-to-inspect symbol metrics
  (complexity, cognitive, fan-in/out), search, and **export** (JSON / Markdown / SVG / PNG). codeintel
  stays headless — the CLI produces the data *and* the picture; there is no server or UI framework.
  Nodes are sized by complexity and colored by directory, so it generalizes to any repo layout. The
  viewer template ships with the package (`src/codeintel/viewer/`).

### Removed
- Deleted stale workspace docs `ASSESSMENT.md` and `HANDOFF.md` — 2026-08-12 review/handoff artifacts
  describing a since-fixed state (per-request gateway rebuild, an unverified graph engine, 86/93 tests);
  they no longer reflect the code and nothing references them. (`docs/adr/0001-…` is kept — unlike those,
  it documents a current, shipped decision.)

## [0.10.0] — 2026-08-14

Two things that move codeintel from "works, with caveats" toward "just install it": a real
**one-command setup**, and a **published scale benchmark**.

### Added
- **`codeintel setup --all` — one-command setup.** Installs every automatable backend (uv for the LSP
  engine, the embedding model, a serena warm-up), indexes the repo, and prints a health report ending
  in a **Next:** list — exactly what's ready and the one remaining step. Idempotent: it skips what's
  already installed, so it's safe to re-run. Previously a user had to know to combine
  `--install-uv --install-deps --index --warm`; now it's one flag (the individual flags still work).
- **Scale benchmark** — [docs/benchmarks.md](docs/benchmarks.md): a full 1,449-file TS/React monorepo →
  **25,313 chunks indexed cold in ~8.3 min** (~51 chunks/sec, ~1.7 GB peak RSS, **60 MB** on-disk
  index); **warm `code.query` search p50 235 ms / p95 251 ms** (all queries returned relevant hits).
  Reproducible, with methodology and an extrapolation to the 100k-chunk ceiling.

### Changed
- **Graph is now genuinely optional, not a half-install.** It needs an external binary codeintel
  can't auto-install (`codebase-memory-mcp`), so `doctor`'s health model treats it as optional: a repo
  with **semantic + LSP** ready is *healthy* (and `setup --all` **exits 0**) even without the graph
  binary. `doctor` notes graph is "optional — an external backend; codeintel works without it," and its
  guidance is platform-aware (e.g. `Darwin/arm64`).
- `codeintel doctor`'s unhealthy tip and the README Quickstart now lead with `codeintel setup --all`.
- Docs refreshed to the current op set: README's op table + `docs/deploy.md`'s RBAC role example now
  list `changed`/`deadcode`/`hotspots`; `docs/architecture.md` documents the `changed` cache bypass.

### Fixed (from adversarial review, before release)
- **`--warm` on a fresh machine.** The warm step read the *pre-install* preflight, so it printed a
  self-contradictory "warm lsp: fail" directly under "install uv: ok" and never booted serena. It now
  re-probes (deep) *after* the install loop, so a just-installed `uv` is visible.
- **`setup --all` exit code.** It exited 1 (and rendered red) on the common no-graph machine because
  `healthy` required all three engines — graph is now optional (above), so a successful setup exits 0.
- **`_next_steps` hardened** to degrade to an empty list on a malformed doctor dict, matching every
  sibling helper's never-raise discipline.

### Tests
Idempotent-install skip, optional-graph health + render, warm-lsp re-probe (fresh not stale),
`_next_steps` never-raises on malformed input, and the diagnose-only → `--all` hint. Full suite:
**326 passed, 1 skipped**.

## [0.9.0] — 2026-08-14

Unlocks graph-engine capabilities the wrapped `codebase-memory-mcp` backend already computes but that
`code.query` never surfaced. The tool wrapped a rich backend and exposed only a search/trace subset;
this release adds the agent-facing ops that make the graph engine useful for *changing* code, not just
reading it. Designed via an ADR (`docs/adr/0001-graph-capability-unlock.md`).

### Added
- **`changed` — impact of your uncommitted edits.** The flagship pre-edit op: `code.query op=changed`
  runs the backend's `detect_changes` and reports which files changed and which symbols those changes
  ripple into — the thing a coding agent needs before an edit and can't get from grep or embeddings.
  Never cached (it reads the live git worktree, which the content-hash cache key can't see).
- **`hotspots` — complexity / fan-in risk.** Highest cyclomatic+cognitive-complexity, highest-fan-in
  symbols (client-sorted) — the "where is this codebase most dangerous to change" map.
- **`deadcode` — unreferenced non-test symbols.** In-degree-0 functions, tests and builtins filtered
  out, biggest first.
- **`chain` is now risk-labeled** — each call-chain hop carries a `[risk: …]` badge when the backend
  classifies it.
- **Agent discoverability**: the `code.query` tool description and the MCP server `instructions` now
  name the new ops and tell an agent to run `changed` before editing.

Each op maps to a backend method (`detect_changes`; `search_graph` with degree filters + complexity
metrics; `trace_path` with `risk_labels`), rendered to bounded markdown behind the same never-raise
safe-null contract. An empty scan (clean tree, no dead code) is a true answer (an informative string),
not a lookup miss (safe-null).

### Fixed (from adversarial review, before release)
- **Cache staleness on the fan-out path.** `engine=both`/`all` + `changed` could serve a stale diff —
  the cache bypass now covers the fan-out path, not just single-engine dispatch.
- **Fragile file-marker filter.** `changed` separated real symbols from bare file/module markers by a
  `"/"-in-label` heuristic, which leaked root-level markers (`main.py`) as fake symbols and would drop
  real symbols whose qualified names contain `/` (e.g. Go's `github.com/org/pkg.Func`). Now filtered
  structurally (drop when `label == file_path`), correct in both directions.
- **False "working tree clean".** A malformed (non-`detect_changes`) backend dict rendered a cheerful
  clean-tree message instead of degrading to safe-null; it now requires the real response shape.
- Dogfooding fix: the backend returns duplicate `changed_files` (staged+unstaged) — now deduped.

### Tests
+19 (318 passed, 1 skipped): the three new ops against captured real backend shapes; risk-label
rendering; cache bypass on **both** single-engine and fan-out paths; the marker filter in both
directions (root-level drop + slash-qualified-name keep); malformed-dict → safe-null; never-raise for
all three ops. A `/simplify` pass also collapsed ~40 lines of duplicated scan→render into one helper.

## [0.8.5] — 2026-08-14

More dogfooding fixes — from driving the *published* tool over real TS/React repos (brightsky-ai,
pathly-adapters).

### Fixed
- **Arrow-function components/hooks are now def-aligned.** The tree-sitter chunker only recognized
  `function`/`method` declarations, so `const Header = () => {…}` / `export const useThing = () => {…}`
  — how virtually all React components and hooks (and much modern TS/JS) are written — fell back to
  line windows. A `const`/`let` bound to an arrow or function expression is now its own chunk (a
  plain `const x = 5` still isn't).

### Added
- **`doctor` now reports whether def-aligned chunking is active.** tree-sitter's fallback to line
  windows was silent — a missing/broken `tree-sitter-language-pack` degraded chunking for every
  non-Python file with no signal (which is exactly how a stale environment indexed a whole TS repo
  as line windows). `codeintel doctor` now shows `def-aligned chunking: OFF …` with the fix when the
  grammar pack isn't importable.

### Tests
- Arrow-function def-alignment (and that a plain data const is NOT a chunk); the doctor tree-sitter
  advisory (shown only when off) + `run_doctor` reporting availability. Full suite: 299 passed.

## [0.8.4] — 2026-08-14

### Added
- **codeintel now advertises itself to agents**, so after `codeintel install` an agent reaches for
  it by default rather than falling back to grep/file-read. The MCP server sets the standard
  `instructions` field (prefer `code.query` for understanding code; how to read the never-raise
  safe-null envelope) and reports its `version`; the four tool descriptions were rewritten from
  throwaway one-liners into real "use this for callers/callees/impact/search/orientation" guidance.
  Standard MCP — works across clients (Claude, Codex, …) with no hooks written into a user's config.

### Tests
- `tests/test_mcp_server.py`: the server is constructed with non-empty `instructions` (mentioning
  `code.query`, grep, and the reason envelope) and rich per-tool descriptions. Full suite: 296 passed.

## [0.8.3] — 2026-08-14

Embedding-model / vector-dimension safety, done right — an architect-designed replacement for the
fix that was prepared for 0.8.2 and reverted (it caused cross-project data loss).

### Fixed
- **Changing the embedding `model` can no longer corrupt or wipe the semantic index.** The cache was
  a single shared `~/.codeintel/semantic.db` with a hardcoded `FLOAT[384]` vec0 table; a
  different-dimension model corrupted it (a DELETE-then-failed-INSERT dropped chunks), and a
  same-dimension different model silently mixed incompatible vectors. The cache is now **partitioned
  by model**: the default model keeps `semantic.db` (zero migration for existing users), any other
  model gets its own `semantic-<hash>.db`, and the vec0 table self-dimensions from the first real
  vector (no more hardcoded 384). Different-model repos are now **physically isolated** (separate
  files), so they can never corrupt or wipe each other — precisely the failure the reverted attempt
  had.

### Changed
- `SemanticProvider.build_result` and `probe` now resolve the same per-model file (fixing a latent
  divergence — build_result used a module `_DB_PATH`, probe used `default_db_path()`).
- `codeintel reset` sweeps every per-model cache file (scoped by project, or `--all`); changing a
  repo's `model` switches it to a fresh file, and the old one is reclaimed by `codeintel reset`.

### Hardened (from an adversarial review pass)
- The review confirmed the cross-project wipe is now physically impossible; two follow-ups: `code.status`
  now reports the repo's *configured* model (was hardcoded to the default), and the `index` CLI
  degrades with a message instead of a traceback if setup fails (e.g. an unresolvable home dir).

### Tests
- New `tests/test_model_dimension.py`: the cross-project-no-wipe regression (fails against the
  reverted global-wipe approach), self-dimensioning, dimension-mismatch skip (never wipes),
  `default_db_path` invariants, reset sweeping model files while sparing other projects, and
  probe/build_result resolving the same per-model file. Full suite: 295 passed.

## [0.8.2] — 2026-08-14

Two compatibility fixes, both found by dogfooding.

### Fixed
- **Codex registration wrote the wrong file/format.** `codeintel install --agent codex` wrote a
  Claude-style JSON `mcpServers` block to `~/.codex/config.json`, but Codex CLI reads MCP servers
  from `~/.codex/config.toml` as `[mcp_servers.<name>]` TOML — so codeintel was never actually
  registered with Codex. Now writes the correct TOML table, merging into (and preserving) an
  existing `config.toml`, idempotently. (The MCP server itself was always protocol-compatible; only
  the installer was wrong.)
- **Graph ops failed for a relative `--project-root`.** `GraphProvider._match_project` compared the
  raw path against the backend's absolute `root_path`, so `codeintel map .` — and any graph query
  with a relative path — resolved to "not indexed" from inside the repo. That was the actual root
  cause of the map emitting a stub (0.8.1 stopped the stub from clobbering a good map; this stops
  the stub). The path is normalized with `realpath` before matching.

### Tests
- New `tests/test_installer.py` (the installer had zero tests — how the Codex bug shipped): Codex
  TOML registration + idempotency + config preservation, the JSON agents, unknown-agent. Plus a
  graph relative-path resolution test.

_(An embedding-model-dimension safety fix was prepared for this release but reverted before publish:
adversarial review found it could wipe other projects' rows in the shared cache when repos use
different per-project `model` settings. It needs a redesign — tracked as a follow-up.)_

## [0.8.1] — 2026-08-13

### Fixed
- **`codeintel index` no longer guts a populated `CODE_INTEL.md`.** The best-effort map refresh
  after an index (and `codeintel map` itself) overwrote an existing populated map with a degraded
  stub whenever the graph backend was unavailable or hadn't indexed the repo yet — a common
  transient. `MapGenerator.write` now preserves an existing populated map when the new content is a
  stub (a real map still refreshes it, and a first-ever stub still writes). Found by dogfooding —
  this project's own release `index` runs had been silently stubbing its `CODE_INTEL.md`.
- `MapGenerator.write` now returns `(path, wrote)` and the `map` CLI / `code.map` MCP tool report
  the real outcome ("Kept existing …" + `wrote: false`) instead of claiming a write that a preserve
  skipped.

### Hardened (from an adversarial review pass)
- A sparse-but-real map (entry points only, or a budget-truncated render) is no longer misclassified
  as a stub — `## Entry Points` now counts as populated content, so a legitimate refresh isn't
  skipped and the "graph empty" warning isn't shown when the graph was actually queried.

### Tests
- `tests/test_mapper.py`: a stub does not overwrite a populated map, a stub still writes when no map
  exists, a populated map still replaces a stale stub, an entry-points-only render counts as
  populated, and `write` reports `wrote` honestly. Full suite: 278 passed.

## [0.8.0] — 2026-08-13

Syntax-aware chunking for **non-Python** languages via tree-sitter — TS/JS/Go/Rust/Java/C/C++
search hits now map to whole functions/methods/classes, the same as Python did in 0.6.0. The
roadmap's "Later" tree-sitter item, now that `ast` proved the value on Python.

### Added
- **Tree-sitter chunking** for TypeScript/TSX, JavaScript, Go, Rust, Java, C, and C++, behind the
  Phase-1 chunker interface: functions/methods → one chunk each; classes/impls/traits/interfaces/
  namespaces → a header chunk plus one chunk per member (never a whole-container chunk shadowing
  its members), through the same `_cover` gapless-coverage / oversized-split pipeline as Python.
  Node types a language config doesn't list simply fall into window-filled gaps, so an incomplete
  map degrades precision, never correctness.
- `tree-sitter-language-pack` is now a dependency (≈2 MB; one package covers all the grammars).

### Changed
- Under `chunk_strategy = "syntax"` (the default), non-`.py` code files are now def-aligned instead
  of line-windowed. `.md` and unmapped extensions still window; `chunk_strategy = "lines"` still
  forces windowing everywhere.
- The indexer now walks the common TS/JS and C/C++ variant extensions too — `.tsx` `.jsx` `.mjs`
  `.cjs` `.cc` `.cxx` `.hpp` `.hh` — so a TS/React or C++ repo is fully indexed (previously these
  were silently skipped entirely).

### Hardened
- **Never a hard requirement.** If `tree-sitter-language-pack` (or a grammar) isn't installed, a
  parser errors, or a file yields no definitions, the file falls back to line windowing per file —
  the never-raise contract holds, so one bad file or a missing dependency never aborts the pass.
  Parsers are cached per Indexer instance (loaded once per language, no cross-thread sharing).
- tree-sitter is error-tolerant, so a syntactically-broken source still produces useful def-aligned
  spans (a partial tree) rather than degrading to windows.

### Tests
- New `tests/test_treesitter.py` (15 cases): def-aligned spans for TS/Go/Rust/Java/C++ (methods
  carved out of classes/impls/interfaces), gapless + collision-free coverage, error-tolerant
  parsing, the parser cache, a both-ways extension/language-map guard, and every fallback (missing
  parser, parser exception, `lines` strategy, unmapped ext). The stale `.ts-always-windows` chunking
  test was updated to the new behavior. Full suite: 274 passed.

### Hardened (from an adversarial review pass)
- Fixed a coverage hole the review caught: `.tsx`/`.jsx`/`.cc`/… were mapped to a grammar but not
  in the walker's indexed-extension set, so those files were silently indexed by nothing. The set
  now includes them, with a test asserting the map and the walked set agree both ways.
- Added the `method_signature` node type so TS/TSX **interfaces** decompose into per-method chunks
  (previously only `abstract_method_signature`, for abstract classes, was recognized).

## [0.7.0] — 2026-08-12

Hybrid reranking for the semantic engine — search results are re-ordered by fusing cosine
similarity with a lexical/symbol score, so an exact symbol match is no longer buried under a
merely-semantically-near chunk. Phase 2 of `docs/roadmap-semantic.md`.

### Added
- **Hybrid rerank** (`rerank = "on"`, the new default). `Searcher.search` retrieves a cosine
  candidate set (`rerank_candidates`, default 30), re-reads each candidate's chunk text (bounded —
  ≤ 40 lines, capped at the next stored chunk so it never bleeds into an unrelated def), scores a
  lexical token overlap (camelCase/snake_case-aware) plus a `def`/`class` **symbol boost**, fuses
  the semantic and lexical ranks with Reciprocal Rank Fusion (`1/(60+rank_sem) + 1/(60+rank_lex)`)
  plus the boost, and returns the top-k. A query with no lexical overlap returns the cosine order
  unchanged.
- **`rerank` + `rerank_candidates` config keys**, validated in `config.py`. `rerank = "off"`
  restores the exact pre-0.7 pure-cosine path.

### Changed
- The `cosine_floor` gate stays on the **semantic** candidate set, so reranking only re-orders
  chunks pure cosine already judged good enough — quality can't regress below the pre-0.7 path.
  Result `score` stays the cosine similarity (interpretable); only the ordering reflects the fusion.

### Hardened (from two adversarial review passes)
- Lexical/boost text is bounded at the **next stored chunk start** (via the Phase-1
  `(project_root, file_path)` index), so a small chunk can't be credited with a neighbouring def's
  symbol, and an overlapping window chunk isn't truncated below its own span.
- `Searcher.search` coerces `k` / `rerank_candidates` (bad type / zero / negative → default) and
  caps the candidate set (`_RERANK_CANDIDATES_CAP`) so a misconfigured `rerank_candidates` can't
  turn one query into thousands of reads — while always honoring `k`. `_read_chunk` uses
  `itertools.islice` so a huge file isn't fully read for a 40-line window. Falsy `rerank` spellings
  (incl. `False`) disable; the symbol boost is Unicode-aware and case-insensitive, matching the
  lexical score.
- Never-raise throughout: a missing/edited file scores that candidate 0 (not a crash), and any
  rerank fault falls back to the cosine order.

### Fixed
- **Embeddings now update when a chunk's content changes.** `Indexer._embed_and_write` used
  `INSERT OR REPLACE` on the `code_embeddings` **vec0** virtual table, which `sqlite-vec` does not
  honor — it raised `UNIQUE constraint failed` on an existing `chunk_id` instead of replacing, so a
  chunk whose content changed but whose start line (`chunk_id`) stayed put silently kept its
  **stale** vector. Syntax chunking (0.6.0) exposed this on every function-body edit (a def's
  `chunk_id` is its def line, which doesn't move). Now uses DELETE-then-INSERT, the supported vec0
  upsert. Found by dogfooding: re-indexing a real repo threw 185 of these errors → now 0.
- **`doctor` now says *how* to fix a missing backend.** The `fix:` lines carried vague "install X"
  text; they now carry runnable commands (`codeintel setup --install-uv`, `brew install uv`, …),
  and a footer points at `codeintel setup` for the pip-installable backends.

### Tests
- New `tests/test_rerank.py` (14 cases): exact-symbol-over-semantic ordering, `rerank="off"`
  cosine parity, no-lexical-signal order preservation, the no-bleed fix, bounded + capped reads,
  large-`k`-not-shrunk, bad-param and falsy-`rerank` degradation, and lexical / symbol-boost units.
  Plus a regression test that a changed chunk re-embeds at a stable `chunk_id` (the vec0 upsert).
  Config validation and the config-threading integration test extended. Full suite: 259 passed.

## [0.6.0] — 2026-08-12

Syntax-aware chunking for the semantic engine — Python files are now embedded on real definition
boundaries instead of fixed line windows, so a search hit maps to a whole function/method/class
rather than an arbitrary span. Phase 1 of `docs/roadmap-semantic.md`.

### Added
- **Syntax-aware chunking** (`chunk_strategy = "syntax"`, the new default). `.py` files are parsed
  with the stdlib `ast` and chunked on definition boundaries: each top-level `def` / `async def` is
  one chunk (decorators included); each `class` becomes a header chunk (bases + class docstring)
  plus one chunk per method / nested def; module-level and inter-method runs are line-windowed so
  coverage stays complete. A def longer than `2 × window` is window-split so no chunk overflows the
  embedder. `chunk_start` stays 0-based and `chunk_id` is unchanged, so the DB schema and
  `Searcher._read_snippet` are untouched.
- **`chunk_strategy` config key** (`"syntax" | "lines"`, default `"syntax"`), validated in
  `config.py`. `"lines"` forces the pre-0.6 fixed-window behaviour — a runtime escape hatch.
- **Per-file orphan reconciliation.** After re-chunking a file, rows for `chunk_id`s it no longer
  produces (a moved/deleted function, or a strategy switch) are dropped from both `chunk_hashes` and
  `code_embeddings`, scoped by `(project_root, file_path)`. This also fixes a latent pre-0.6 bug:
  deleting a function never dropped its chunk.

### Changed
- Non-`.py` files, and any `.py` that fails to parse (`SyntaxError`, a NUL byte, …), fall back to
  line windowing **per file** — the never-raise contract holds per file, so one malformed file
  never aborts the index pass.
- Replaced the single-column `idx_chunk_project` index with a composite
  `idx_chunk_project_file(project_root, file_path)`, so the new per-file reconcile (and the existing
  deleted-file cleanup) is an index seek rather than an O(files²) project-wide scan. Backward
  compatible: the regenerable cache builds the new index on next open.

### Hardened (from an adversarial review pass)
- `Indexer` now coerces its numeric knobs (`window` / `stride` / `max_chunks` / `max_total_chunks`)
  and case-normalizes `chunk_strategy` in its constructor, so a direct caller that bypasses config
  can't set a `stride=0` that raises inside `range()`, a `window=0` that silently drops every
  region, or a `chunk_strategy="LINES"` that silently swaps to the default.
- Corrected the "non-overlapping" claim in the chunker docs/docstring: def-aligned chunks are whole
  distinct units, but window-filled runs and oversized-def splits reuse the overlapping
  `window`/`stride` exactly as the legacy windower does (always strictly less overlap than `lines`
  mode). Coverage is complete and chunk starts are collision-free.

### Tests
- New `tests/test_chunking.py`: def-aligned / gapless / collision-free spans, decorator inclusion,
  oversized-def splitting (both the default overlapping-window path and the `window == stride`
  non-overlapping case), syntax-error and NUL-byte fallback, non-Python windowing,
  `chunk_strategy="lines"` parity with the legacy windower (verified byte-identical against the
  0.5.0 source and pinned to the legacy formula), constructor guards, and orphan reconciliation
  (function removed, cross-file scoping, strategy switch). Existing
  semantic / e2e / hardening / integration suites stay green.

## [0.5.0] — 2026-08-12

Role-based access control — the HTTP transport can now serve multiple callers with different
privileges, activating the previously-dormant `TieringPolicy`.

### Added
- **RBAC** — an optional `auth.toml` (`~/.codeintel/auth.toml` or `$CODEINTEL_AUTH_CONFIG`) maps
  bearer tokens to roles and roles to the ops they may run (`["*"]` = all). The role is derived
  **server-side from the token**, so a client cannot escalate by sending `"role": "admin"` in the
  request body; a disallowed op returns **HTTP 403** (`op-not-allowed-for-role`). Tokens are
  compared as sha256 (a `sha256:<hex>` entry keeps plaintext out of the config file).
- **`codeintel gen-token`** — print a secure random bearer token.
- Docs: an RBAC + SSO-via-auth-proxy section in `docs/deploy.md` (codeintel owns authorization; an
  OIDC proxy such as oauth2-proxy owns SSO).

### Changed
- With RBAC configured, a non-loopback bind counts as authenticated (no separate `--token` needed);
  the fail-closed guard accepts either a shared token or an RBAC config.
- The MCP (stdio) transport is unaffected — the local agent runs unrestricted.

### Hardened (from a security review pass)
- `/code/doctor` is now RBAC-gated behind a `doctor` scope — previously any authenticated token,
  regardless of role, got full diagnostics (including a deep LSP boot on an arbitrary path).
- A role whose `ops` is not a list (the `reader = "search"` missing-brackets typo) now **fails
  closed** (deny-all) with a warning, instead of silently granting full access.
- A malformed or token-less `auth.toml` is now logged loudly (was silent), and the `sha256:` token
  prefix is matched case-insensitively.
- The RBAC policy check runs **before** the background reindex, so a denied role can't trigger
  reindex work on an attacker-chosen path.

### Tests
- New `tests/test_rbac.py`: config loading (`sha256:` entries, malformed files, non-list-ops
  fail-closed), policy construction, and HTTP enforcement — allow / deny-403 (query **and**
  doctor), missing + invalid token → 401, the no-escalation guard (a reader claiming `role=admin`
  is still a reader), and denied-op-does-no-reindex. (+16 tests → 227 total.)

## [0.4.0] — 2026-08-12

Enterprise operability — the HTTP transport ships the endpoints, signals, and packaging a platform
team needs to run codeintel as a shared, observable service.

### Added
- **Health & readiness probes** — `GET /healthz` (liveness) and `GET /readyz` (readiness), both
  unauthenticated by convention, for load balancers and Kubernetes probes.
- **Prometheus `/metrics`** — dependency-free exposition (`codeintel_requests_total{method,path,
  status}`, `codeintel_request_duration_seconds`, `codeintel_requests_in_flight`,
  `codeintel_build_info{version}`). Path labels are restricted to known routes, so an attacker
  can't explode label cardinality. Auth-gated when a token is configured.
- **Structured logging** — `CODEINTEL_LOG_FORMAT=json` (one JSON object per line),
  `CODEINTEL_LOG_LEVEL`, and `CODEINTEL_HTTP_ACCESS_LOG=1` for per-request access lines.
- **Graceful shutdown** — the HTTP server handles `SIGTERM`/`SIGINT`, draining in-flight requests
  and exiting `0` (systemd- and Kubernetes-friendly).
- **Container image** — a multi-stage, non-root `Dockerfile` with a `/healthz` HEALTHCHECK, plus
  `.dockerignore`.
- **Ops & governance docs** — `docs/deploy.md` (systemd, Docker/Compose, Kubernetes with probes,
  reverse-proxy TLS, Prometheus scrape config, security checklist), `SECURITY.md`, `CONTRIBUTING.md`.

### Hardened (from a security review pass)
- **Fail closed**: `serve-http` on a non-loopback host now *refuses to start* without a token
  unless `CODEINTEL_ALLOW_NO_AUTH=1` is set — no more accidental unauthenticated exposure (and this
  is the container's default posture, so `docker run` with no token stops with a clear message).
- **Graceful shutdown actually drains now**: worker threads are daemons, so `server_close()` never
  joined them; shutdown now waits (bounded, 15s) for in-flight requests to finish before exiting,
  so a rolling restart doesn't cut a live response. Verified end-to-end.
- **Overload visibility**: concurrency-cap refusals are counted as `codeintel_requests_rejected_total`.
- `/metrics` rendering is wrapped so a render error can never leave a client with no response.

### Tests
- `tests/test_enterprise.py` covers the probes, `/metrics` + auth gating, the metrics registry
  (bounded cardinality, in-flight + rejected counters), the JSON log formatter, and the fail-closed
  non-loopback bind. (+11 tests → 211 total.)

## [0.3.0] — 2026-08-12

Reliability pass for unattended/production use: safe config, cheaper warm queries, optional auth
on the network transport, and diagnosable failures.

### Added
- **Optional bearer-token auth for `serve-http`** — `--token TOKEN` (or `CODEINTEL_HTTP_TOKEN`)
  requires `Authorization: Bearer <token>` on every request (constant-time, bytes-safe compare),
  making `--allow-remote` actually deployable. No token → auth disabled (the loopback default).
- **Config validation** — a malformed `.codeintel.toml` (wrong type, out-of-range `cosine_floor`,
  unknown enum) now falls back to that key's default with a warning instead of breaking every
  query that loads it.
- **`CODEINTEL_DEBUG=1`** — logs the full traceback of any error the never-throw contract
  swallows, so an unexpected `null` is diagnosable without weakening the contract.
- **`max_total_chunks`** config — safety ceiling on chunks embedded in one index pass, so a huge
  monorepo can't drive unbounded memory on its first index.
- Per-request socket timeout on the HTTP transport (slow-client / slowloris guard).

### Changed
- **Semantic search skips the inline full-index on a warm repo** — it only walks+hashes the whole
  tree on a COLD repo (or when the background reindexer is disabled via `CODEINTEL_REINDEX=off`),
  relying on the debounced background reindexer otherwise. Large latency win on repeat queries.
- **`code.status` / `codeintel status <repo>` is project-scoped** — `indexed` reflects whether
  THAT repo has indexed chunks, not merely "a semantic db exists somewhere on this machine".
- **`overview` auto-routing also falls back to LSP** when the repo isn't in the graph (previously
  only when the graph backend was entirely unavailable).

### Hardened (from an adversarial review pass)
- Config coercion no longer crashes on TOML `inf`/`nan` (`int(float('inf'))` → `OverflowError`);
  the CLI `index` path could previously abort with an uncaught traceback.
- `reindex = "never"` now actually disables the **background** reindexer (not just the inline
  path), and `max_total_chunks` is honored by **all** index entry points (`index`, `setup`, the
  background pass), not only inline search.
- The HTTP transport bounds concurrent worker threads (fast `503` past the cap) so a slow-client
  burst can't exhaust threads/FDs, and routes stalled-client socket errors through the same quiet
  `log_swallowed` path instead of a stderr traceback.
- The graph provider caches a *failed* project lookup only briefly (short TTL), so a repo indexed
  into the graph after a first miss is picked up without restarting a long-lived server.

### Tests
- New suites for config validation (incl. inf/nan), HTTP auth (incl. a non-ASCII-token crash
  regression + the concurrency cap), the cold/warm indexing decision, the chunk ceiling, the
  overview fallback, `reindex="never"`, and the graph negative-lookup TTL (+29 tests → 200 total).

## [0.2.2] — 2026-08-12

Production-hardening pass — bounded memory, concurrent request handling, and a maintained CI.

### Added
- **Concurrent HTTP transport** — the server now handles requests on threads, so one slow query
  (an LSP session warming, a first-time index) no longer blocks every other agent. Shared gateway
  state is lock-guarded and the semantic engine is thread-confined with WAL, so this is safe.

### Changed
- **Bounded query cache** — `ContentHashCache` is now an LRU capped at 1024 entries so the
  long-lived server holds steady memory instead of growing an unbounded dict; freshness/hash
  invalidation is unchanged.
- **Thread-safe graph project resolution** — the graph provider's project-name cache is now
  lock-guarded, and `list_projects` runs outside the lock so a slow backend can't serialize
  concurrent requests.

### CI
- Bumped `actions/checkout`, `actions/setup-python`, and `actions/upload-artifact` off the
  deprecated Node 20 runtime.

### Documentation
- README: added a **"What makes it good"** section (local-first & private, never-throws, one tool
  not three, graceful degradation, fast+bounded caching, concurrency-safe, self-diagnosing).

## [0.2.1] — 2026-08-12

### Fixed
- **Semantic DB concurrency** — the background reindexer (a daemon thread) and the inline index
  a query runs open two connections to the one cache file. With SQLite's default zero busy
  timeout, the loser of that write race hit an immediate `database is locked` and silently
  dropped its work. The DB layer now sets `busy_timeout` and `journal_mode=WAL`, so writers wait
  instead of failing and a search can read while a reindex writes.

### Changed
- **Atomic writes to user-owned files** — `codeintel install` (agent config such as
  `~/.claude/settings.json`) and `codeintel map --inject` (`CLAUDE.md` / `AGENTS.md`) now write
  via a temp file + atomic rename, so an interrupted write can never truncate a file the tool
  does not own. The install merge already preserved unrelated keys; this protects the write too.

### Documentation
- Rewrote the README lead to explain what codeintel does for a coding agent and what it can ask —
  a per-operation table plus a real request/response example — and documented the four MCP tools.
- Fixed drift: `max_chunks` is documented as **per file** (matching the code and semantic docs),
  and the test-suite runtime note is realistic.

## [0.2.0] — 2026-08-12

First public release. Distributed on PyPI as **`codecortex`** (the import package and CLI
remain `codeintel`). Verified end-to-end on real Python **and** TypeScript repositories.

### Added
- **`codeintel doctor`** — preflight health check (CLI + `code.doctor` MCP tool + `POST /code/doctor`).
  Reports, per engine, installed / runnable / repo-indexed with a one-line fix for each gap;
  `--deep` boot-checks serena; `--json` for scripting; exits non-zero when unhealthy.
- **`codeintel setup`** — onboarding: checks backends, prints exact install steps, opt-in
  `--install-uv` / `--install-deps`, `--index`, `--warm`, ending with a health report.
- **`codeintel reset`** — clear a corrupt/stale semantic index (this repo, or `--all`); safe on a
  corrupt DB; confirms unless `--yes`.
- **`code.doctor` MCP tool** and **`POST /code/doctor`** HTTP endpoint.
- A dependency-free terminal output system: color (respects `NO_COLOR` / `--no-color` / non-TTY),
  ASCII fallback (`--ascii`), consistent across `doctor` / `status` / `query` / `setup`.
- Actionable `hint`s on the two "repo not indexed" safe-null reasons.
- Extensive real-boundary tests (live subprocess/DB, captured real backend responses).

### Fixed
- **graph engine** now reads the real `query_graph` `{columns, rows}` shape and traverses the
  real call edges (`CALLS` + `USAGE`); `callers`/`callees`/`impact`/`chain`/`pattern`/`overview`
  return real data (previously silently empty). Project resolution prefers an exact root match.
- **LSP engine** now launches serena correctly
  (`uvx --from git+https://github.com/oraios/serena serena start-mcp-server …`; the old
  `uvx serena` never worked) and uses serena's real tool contract (two-step reference lookup).
- **`code.map`** ranked-symbols / entry-points now populate from the real backend shape.
- **HTTP hardening**: non-loopback bind refused unless `--allow-remote` (loopback detected via
  `ipaddress`, not a spoofable string prefix); 1 MiB request-body cap → HTTP 413.
- **Reindex no longer hangs the CLI**: background reindex runs on daemon threads, so a first
  query on a large repo returns immediately instead of freezing until the repo-wide index finishes.
- Graph backend calls migrated off the **deprecated raw-JSON CLI args** to piped stdin (with a
  one-release fallback) and a shared timeout deadline.
- `impact` no longer emits a redundant double header.

### Changed
- `op=context` is now implemented as a real fan-out across all engines (graph→impact, lsp→symbol,
  semantic→search).
- Version is single-sourced from `codeintel.__version__`.

### Notes
- The graph engine requires the external `codebase-memory-mcp` binary; the LSP engine fetches
  serena via `uvx` on first use. `pip install codecortex` gives you the semantic engine
  out of the box — run `codeintel doctor` to see what else is available. The unrelated `codeintel`
  package on PyPI is a different project; install `codecortex`, and avoid installing both.

[0.2.0]: https://github.com/hamilton-sky/codeintel/releases/tag/v0.2.0
