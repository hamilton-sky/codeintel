# Changelog

All notable changes to codeintel are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
