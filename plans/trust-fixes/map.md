# Map: trust-fixes

## Destination

The 34 red-team findings that map to blue-team fixes T1-T10 are fixed. Each one is pinned by a regression test built from its repro in `.feature-flow/state/redblue-findings.md`. An agent can act on `confidence: complete` and `safe_for_destructive: true`: known graph blind spots (aliased imports, sibling/interface dispatch, nested shadowing, `lsp_ts_local`) add a gap. Every graph answer, and the gateway cache, notices a stale or rebuilt index. `chain A->B` honours B. No write leaves the repo through a symlink. Every hint names a tool or command the agent really has.

**The bar.** In the repo venv, these all pass:

- `.venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read`
- `.venv/bin/ruff check src tests bench` (CI's own lint command; it replaces the brief's `ruff check .`, decided by the user, see below)
- `.venv/bin/mypy`
- `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check` (exit 0, "0 gating finding(s)")

Also, every regression test added for a finding fails on main eb0902f and passes on the branch. Ticket 13 runs all of it.

## How to work this

- See what is ready: `python3 scripts/flow-status.py trust-fixes`. Take the lowest numbered READY ticket.
- Claim: set `Status: claimed` in the ticket and save before starting.
- Resolve: write the result under `## Answer`, set `Status: resolved`, then add one line to Decisions so far below.
- See the graph: `python3 scripts/flow-status.py trust-fixes --mermaid` (coloured by status, drawn on demand).
- Commands live in `commands.md` (frozen). Lessons live in `learnings.md` (append only).
- Status lives only in the ticket files. This map never repeats it.
- Read the finding's Repro/Observed/Expected in `.feature-flow/state/redblue-findings.md` and the matching T-section of `.feature-flow/state/redblue-fixplan.md` before writing the regression test. Line numbers there are from 0.29.3, so search by function name.
- Run tools as `.venv/bin/<tool>`. PATH is not set for subagents.

## Decisions so far

- (plan) The gate's Test command deselects `tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read`. It is a known failure on main eb0902f that appears only when the suite runs as root: this container is root, CI is not. The coordinator measured the full suite at ~3m50s, with 2168 passed, 31 skipped and that 1 failed. Do not "fix" it in this feature.
- (plan) `codeintel c4 . --check` is run with `--out /tmp/codeintel-c4-check`, because without `--out` it writes `codeintel-c4/` into the repo. It reads the existing graph index. `tests/test_architecture.py`, in the Test command, enforces the same layer rule from the AST.
- (plan) Every CHANGELOG entry is written once in ticket 13, so `CHANGELOG.md` does not force every ticket to be built alone.
- (plan) Outside facts relied on:
  - `O_NOFOLLOW` fails with ELOOP when the last component is a symlink, and `O_CREAT|O_EXCL` fails on any symlink. docs: https://manpages.debian.org/trixie/manpages-dev/open.2.en.html
  - `%ct` is the committer timestamp. docs: https://git-scm.com/docs/pretty-formats
  - An uncaught KeyboardInterrupt re-raises SIGINT on POSIX. docs: https://github.com/python/cpython/issues/41078
  - The installed `mcp` 2.3.0 serializes a non-string tool result as `TextContent` text with `pydantic_core.to_json(result, fallback=str, indent=2)`. source: `.venv/lib/python3.13/site-packages/mcp/server/mcpserver/utilities/func_metadata.py` (`_convert_to_content`), installed package `mcp-2.3.0.dist-info`
  - The MCP spec sets no result size limit. docs: https://modelcontextprotocol.io/specification/2025-11-25/server/tools.md
  - Claude Code warns above 10,000 tokens of tool output. docs: https://code.claude.com/docs/en/mcp

- (decided, brief) The T6 symlink rule: `CODE_INTEL.md` is never written through a symlink; `AGENTS.md`/`CLAUDE.md`/`USING_CODEINTEL.md` are refused when they or a parent resolve outside the repo root or into its `.git` directory, and followed to a regular file elsewhere in the work tree. Recorded in spec.md Decisions; built in ticket 08.
- (decided, brief) `status-doctor-no-graph-age` (the 35th finding) stays out: it maps only to Improvement #1. If wanted later, it is one ticket: `cli/status.py` and `setup/doctor.py` print `graph_index_age` through `core.paths.graph_db_stamp`.
- (decided, brief) `plans/dogfood-fixes` is left untouched by this feature.
- (decided, brief) The lint bar is CI's `ruff check src tests bench`, replacing the brief's `ruff check .` in the bar, commands.md and ticket 13. Acceptance tests under `plans/` are not ruff-gated. Recorded in spec.md Decisions ("The lint bar").
- (decided, brief) The project has no build step. commands.md's Build, `python -m compileall -q src/codeintel`, is a stand-in syntax check that is in neither `pyproject.toml` nor CI.
- 01 — `changed` checks index age in every mode: `core.paths.graph_db_mtime/graph_db_stamp/graph_slug`, HEAD `%ct` counts, no slack; uncommitted mode refuses `not-a-git-repo` via `changed_range.in_work_tree`; tests get a fresh `graph_index_age` stub unless they request `real_index_age`.
- 01 — round 1 fixes: a failed `git status` gives `index-age-unknown`; uncommitted paths are scoped to the answered project's root; `graph_db_stamp` returns 0 on any OSError.
- 01 — round 2 fixes: HEAD's time is read with `git log -1 --no-show-signature --format=%ct` and an unreadable one (HEAD present) is `unknown`; uncommitted `changed` says "working tree clean" only when `git status` succeeded and listed nothing.
- 02 — callers/callees/impact/context get `stale-index`/`index-age-unknown` (section `index`) from `GraphProvider._stamp_staleness` via `changed_range.graph_index_age`; a missing file is dated by its nearest existing directory; rows in missing or newer files become `unstated`.
- 03 — gateway cache key is `generation:graph_db_stamp:semantic_db(+wal) mtime`; `maybe_reindex` returns bool, submits a git root only when HEAD+modified/untracked fingerprint moved (time debounce still applies; git failure falls back to it), returns False for a missing root; `reindexing` = pending-before or submitted; graph pass runs before semantic.

## Open questions

- `plans/dogfood-fixes` overlaps this plan, and its tickets are still open. For the user to act on after this feature, not part of it:
  - Its 08 (staleness on callers/impact) is superseded by ticket 02 here.
  - Its 04-06 ("Callers through" for TS structural interfaces) are the full version of the gap that ticket 06 here adds; they can build on 06's `dispatch-sibling-unbound` rows.
- Live fixtures run against codebase-memory-mcp 0.9.x, which is in this container. 0.10.x behaviour, such as hops that carry `strategy`, is covered by unit fakes only.
- 03 (round 2) — fingerprint lists `git ls-files -c -o --exclude-standard` and hashes each file's (path, mtime_ns, size), never `-m` (it runs repo clean filters); an unchanged check restarts the debounce window; semantic db path memoised per root on the config files' stats.
- 14 — c4 relations carry `import_files` (IMPORTS file pairs); `--check` matches `[[layers.allow]]` per file edge (allowed only if every edge is), stale = matched no edge; a cycle that vanishes without fully allowed relations is an allowed `import-cycle`.
03 — gateway cache key is generation:graph_db_stamp:semantic_db_mtime; maybe_reindex returns bool and submits only on a changed git fingerprint (stat of ls-files -c -o, bounded by the debounce); graph pass runs before semantic.
- 03 (round 5) — a lock-skipped or raising pass drops the root's fingerprint so it is retried; fingerprint skips listings over 50k files; per-root memo maps are 256-entry LRUs; a truncated c4 cycle with no allowed edge among listed members gets a generic ALLOWED reason, no index.
- 04 — `chain A->B` resolves both ends via `search_graph` (`target-not-found` / candidates), traces outbound depth 5 from A's qn, rebuilds the path backward from B over `query_graph` edges among traced nodes (no parent links in 0.9/0.10); hops label from the edge's own strategy, empty → `[unstated]` + one `hop-resolution-unstated` gap; census keys `GraphOps._chain_between`.
- 05 — `blindspots.py` (pure rules + budgeted `SourceReader` over qualifier_scan); `_known_blind_spots` runs in `_render_edge_answer` after the qualifier scan: alias in any row/target file → `aliased-import-unbound` (section `imports`), callee alias call with no row → same, nested namesake in caller → row name-matched + `target-ambiguous` (section `callers`), no "only symbol named"; `lsp_ts_local` → name-guess.
- 05 — live blind-spot test skips only when codebase-memory-mcp is absent; a present backend that does not answer fails it
- 06 — `DispatchCallers._through_siblings`: callers of `C.m` fetch each sibling `*.m` (from `_Dispatch.definers`, no new definer query) and list its `suffix_match`/`unique_name` receiver-call rows as name-matched under a count-free heading; `dispatch-sibling-unbound` (section `callers`) per sibling with rows, per TS interface whose file declares `m` (`blindspots.interface_declares`), and as "could not check" when a lookup or the read budget stops it.
- 07 — module-scope rows count as `IMPORT` (`_row_kind`): heading `(N direct calls, M files import it — call sites inside not resolved)` + `import-only-callers` (section `callers`); narrowed `callers` drop USAGE rows whose file lacks the qualifier and any import of the target's module (unreadable → name-matched); banner "N symbols named `X` have <unit> edges; M more … exist with none" from one `count(n)` query via `_lookup(surface=False)` using provider's per-thread `_answered_lookup`; no Settle it for unqualified targets.
- 08 — `core.containment.write_contained(root, name, content, follow_links=)` is the one repo-write path (temp `O_EXCL|O_NOFOLLOW` + `os.replace`); CODE_INTEL.md never through a link (`MapGenerator.last_refusal`, `map` exits 1, MCP `note`, index refresh skips); injector files refused outside root/into `.git` (`Injector.refusals`); `map --inject` passes `create=True`.
- 08 (round 2) — `write_contained` matches `.git` by `samestat` on every directory it walks and writes through `O_NOFOLLOW` directory descriptors (`dir_fd`), so a case-insensitive `.GIT` spelling or a parent swapped for a link is refused; refusals name `dangling`/`not-a-file`/`hard-link` apart from `outside-repo`.
- 09 — GET /code/status denied root -> 403 (fallback + `reason`); `code_query_handler` returns `root-not-found` (unavailable, redacted) for a non-directory root after `allows_root`; redact slashed needles get head guard `(?<![\w.~/-])` (mid-path home no longer redacted); MCP `_size` = `pydantic_core.to_json(indent=2)`, row cost at wire depth.
- 10 — model-load failure persisted to `codeintel_home()/semantic_failure.json` (atomic; trusted only <10 min, not future, same model + FASTEMBED_CACHE_PATH, weights still absent; O_NOFOLLOW read) by the provider inline/background passes only; search fails fast `index-failed`, probe/doctor `runnable False` + `reason: model-load-failed`; auto `search` falls back to graph `pattern` (`fallback: graph-name-search`, gap `name-only-fallback`, not cached) when `allows(role, "pattern")`. `cli/index.py` does not write the record yet (acceptance needs it).
- 10 — round 2: no code change; the index/reindexer/onboarding failure-record gap moved to ticket 16
- 10 — round 3: failure record: non-finite `at` rejected, written only for a model-load failure (`last_error` starts "could not load embedding model "); search fallback's `pattern` gate fails closed via `Gateway._allows_fallback_op`.
- 11 — backend stderr cleaned in `_stderr_tail` before the cap (`_agent_facing`: project list, quoted slugs, backend tool names to `code.query`); "project not found" gives `project-not-indexed`; not-in-graph gives one `search_graph` did-you-mean (halves OR-ed, up to 5, best effort) plus `codeintel index <root>` (or ask the user); `_dependency_kinds` keeps only `_EDGE_KINDS`; no-edges dead-code caveat for `callers` only; `symbol`/`search` on graph name their engine; `changes` hidden. ops.py:518 is left to ticket 15.
- 11 (round 2) — did-you-mean runs via `_guarded(surface=False)` on a quarter budget and is skipped under ancestor scope; backend text also withholds single-quoted and bare-after-"project" ids and maps `list_projects` to `code.status`.
