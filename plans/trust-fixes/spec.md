# trust-fixes — Spec

## Problem

A red-team/blue-team review of codeintel 0.29.3 verified 35 findings (`.feature-flow/state/redblue-findings.md`; fix design in `.feature-flow/state/redblue-fixplan.md`, fixes T1-T10). An agent can't act on what codeintel tells it, for four main reasons:

- **Wrong "safe" answers.** In TypeScript, 4 of the 8 answers stamped `confidence: complete` and `safe_for_destructive: true` were wrong. The graph can't see calls through an aliased import, an interface or duck-typed receiver, or a nested function with the same name, and nothing in the answer says so. Python has the same shapes.
- **Stale answers that look current.** Only `changed <ref>` checks the age of the graph index. `changed` with no target, `callers`, `callees` and `impact` describe deleted code as verified. The age check also misses commits made after the index and has a 2 second blind window. The MCP server keeps serving a cached answer after an external `codeintel index`.
- **A false contract.** `chain A->B` ignores B. Hop labels are missing on backend 0.9.x.
- **Unsafe writes and misleading hints.** `map` and `index` write through a symlinked `CODE_INTEL.md`, so a hostile repo can overwrite a file outside itself. Hints name tools the agent does not have (`index_repository`, `search_graph`, `trace_path`). The HTTP transport leaks other projects' names and reports a denial as "nothing installed". Several smaller CLI and help bugs mislead users too.

## Goal and the bar

When this is done:

- An agent can act on `confidence: complete` / `safe_for_destructive: true`. When codeintel cannot see something, a named gap says so.
- Every graph answer says when the index is stale.
- Agent-facing text names only tools and commands the agent really has.

The bar: in the repo venv, these all pass:

- `.venv/bin/pytest tests/ -q` (the one known root-only failure is deselected, see commands.md)
- `.venv/bin/ruff check src tests bench` (this replaces the brief's `ruff check .`; see Decisions, "The lint bar")
- `.venv/bin/mypy`
- `.venv/bin/codeintel c4 . --check` (run with `--out /tmp/codeintel-c4-check`, see commands.md)

Also, for each finding a ticket claims to fix, its regression test fails on main eb0902f and passes on the branch. The builder's failing-test-first commit proves that per ticket, and ticket 13 rechecks it against eb0902f.

## Stories

### An agent deciding whether a change is safe

**As a** coding agent, **I want** `safe_for_destructive: true` only when the graph really saw every caller, **so that** I don't delete or rename code that is still in use.

- [ ] `callers helper` where `helper` is imported as `h` somewhere (`helper as h`, `{ helper as h }`, `export { helper as X }`) is `confidence: partial`, `safe_for_destructive: false`, and has an `aliased-import-unbound` gap.
- [ ] `callees entry`, where `entry`'s body calls an import alias that has no callee row, is partial and has an `aliased-import-unbound` gap.
- [ ] `callers Square.area`, where a same-named sibling method (`Circle.area`) or an interface/Protocol member `area` has a name-matched receiver call (`total_area`'s `s.area()`), lists that call as a name-matched row, is partial, and has a `dispatch-sibling-unbound` gap.
- [ ] `callers inner`, where another function defines a nested `inner` before its call, does not list that function as a resolved caller. The answer has a `target-ambiguous` gap and never says "the only symbol named `inner`".
- [ ] `GraphProvider.build_result("callers", "inner", [], 0, root)` (or `codeintel query --op callers --target inner --json --engine graph`), where the backend's only caller row carries the strategy `lsp_ts_local` (the TS `outer`/nested `inner` repro of `ts-safe-for-destructive-wrong-half`): that row has `verified: false`, the envelope's `evidence` counts it under `possible` and not `verified`, and `safe_for_destructive` is false.

### An agent working right after an edit

**As a** coding agent that just edited files, **I want** every graph answer to say when the index is older than the code, **so that** I never read a stale answer as current.

- [ ] `changed` with no target, after an edit with no reindex, has a `stale-index` gap (or `index-age-unknown`) and is at best `partial`. An untracked source file with 0 indexed symbols counts as stale.
- [ ] `changed` with no target in a non-git directory returns `outcome: unavailable` with `reason: not-a-git-repo`, never "working tree clean".
- [ ] `changed <ref>` is stale when HEAD's commit time (`git log -1 --format=%ct`) is newer than the graph db, even when no file in the diff is. An edit 0.1 s after the index is stale, because the 2 s slack is gone.
- [ ] `callers`, `callees` and `impact` have a `stale-index` gap when the target's file or any row's file is missing or newer than the index. Rows in those files are `unstated`, and the answer is never `safe_for_destructive`.
- [ ] Over MCP with `CODEINTEL_REINDEX=off`, after `codeintel index` runs in another process, the next `callers` call is not served from cache (`cached` is false) and includes the new caller.
- [ ] `reindexing: true` appears only when a pass was already running before the query, or this query found a changed tree. An unchanged tree gives no flag.

### An agent tracing a path

- [ ] `chain "A -> B"` returns `## Path A → B` with the hops in order when B is reachable within 5 hops.
- [ ] When B is not reachable it says "No call path from A to B within N hops (this is not proof none exists)" and has a `no-path-found` gap (partial).
- [ ] When B does not exist the answer names B as not found (`reason: target-not-found`).
- [ ] A hop with no `strategy` prints `[unstated]` and adds a `hop-resolution-unstated` gap (partial).

### An agent reading caller counts

- [ ] A caller row at module scope ("module scope of x.py") counts as an import, not a direct call. The heading reads like "4 direct calls, 10 files import it (call sites inside not resolved)", and the answer has an `import-only-callers` gap.
- [ ] Under a narrowed target (`Gateway.query`), a USAGE row is dropped when its file neither mentions the qualifier nor imports the target's module. When that can't be judged, the row is name-matched and is not counted as direct.
- [ ] The ambiguity banner reads "N symbols named `run` have caller edges; M more named `run` exist with none". It never prints a "Settle it …" qualifier for an unqualified target.

### A user cloning an untrusted repo

**As a** user, **I want** `codeintel map`, `index`, `code.map` and `map --inject` never to write outside the repo, **so that** a hostile repo can't overwrite my files.

- [ ] When `CODE_INTEL.md` is a symlink, wherever it points (outside the repo, inside it, or nowhere), `codeintel map`, `codeintel index` and `code.map` write nothing through it. The file it points at is unchanged, and the command reports the refusal (`map` exits 1 and names the path).
- [ ] When `AGENTS.md`, `CLAUDE.md` or `USING_CODEINTEL.md` (or a parent directory of it) is a symlink that resolves outside the repo root, or into the repo's `.git` directory (`.git/config`, `.git/hooks/…`), `map --inject` and `code.map` with `inject` write nothing through it, the target is unchanged, and the refusal is reported.
- [ ] An `AGENTS.md`/`CLAUDE.md` symlink that resolves to a regular file elsewhere inside the work tree (a shared rules file) is still written through, as before.
- [ ] `codeintel map --inject` in a repo with no AGENTS.md or CLAUDE.md creates AGENTS.md. The advice `codeintel install` prints therefore works.

### An operator running the HTTP server with RBAC

- [ ] A `backend-error` hint never contains other projects' ids. A "project not found" error from the backend is reported as `project-not-indexed`.
- [ ] `GET /code/status?project_root=<root outside the role>` returns 403 with `reason: root-not-allowed-for-role`.
- [ ] `code.query` with a `project_root` that does not exist returns `reason: root-not-found` before any dispatch or indexing.
- [ ] Home redaction leaves `/srv/root/app`, `/proc/self/root/x` and `scratchpad/root/app` alone (HOME=/root), and still turns `/root/x` into `~/x`.

### A user on a host where the embedding model cannot download

- [ ] After an index pass records a model-load failure, the next `search` (in any process) returns `index-failed` with the cause within 1 s. It never says "first-time indexing started".
- [ ] `doctor` reports semantic `runnable: false` with "last index could not load model: <cause>". `status` names that cause instead of "the next index downloads ~50 MB".
- [ ] When semantic is unavailable, `search` falls back to the graph name search, with `fallback: graph-name-search` and a gap saying the result is name-only.
- [ ] `Reindexer._do_reindex(root)` (the pass `Reindexer.maybe_reindex` submits) runs `_graph_reindex(root)` before `_semantic_reindex(root)`, and still runs the graph pass when the semantic pass would block or raise. A test replaces both methods with recorders and reads the call order.

### An agent following hints

- [ ] No hint names `index_repository`, `search_graph` or `trace_path`. The not-in-graph hint names `codeintel index <root>` (or "ask the user to run it") and lists up to 5 close names, e.g. `compact_for_mcpp` → `compact_for_mcp`.
- [ ] `--engine graph --op symbol` gives a hint saying `symbol` is answered by engine=lsp and to use op=context or op=pattern when LSP is down. It never says "did you mean symbol" and never lists `changes`.
- [ ] The no-edges hint never prints a non-edge kind such as "1 1" or the structural kinds `DEFINES`/`CONTAINS`. For callees and impact it talks about outgoing calls and does not mention dead code.

### Small fixes

- [ ] An MCP `code.query` answer's text block is at most 40,000 bytes as sent on the wire.
- [ ] `codeintel prompt` bases its steps on doctor's `reason`: no `pip install` when the package is installed and only the model download failed. It lists `code.status`.
- [ ] Rows from a repo path with spaces (`ünï cødé/日本/uni.py`) render as `ünï cødé.日本.uni.unicaller`.
- [ ] `reset --help` says it clears the semantic and graph indexes. `reset --semantic-only` keeps the graph, and doctor's corrupt-DB fix uses it.
- [ ] Ctrl-C during any command prints one line, `interrupted`, and exits 130, with no traceback.
- [ ] `docs/deploy.md` says the snippet symlink hole is closed via `open_contained`.

## Scope

In:

- T1-T10 from `redblue-fixplan.md`, covering 34 of the 35 findings.
- A regression test per finding.
- `tests/fixtures/blindspots/` (Python + TS).
- README and docs claims that the fixes make false.
- CHANGELOG entries under [Unreleased].

Not in scope:

- `status-doctor-no-graph-age`. It maps to Improvement #1, the `freshness` block, which the brief puts out of scope. It is listed under Open questions in map.md.
- Improvements 1-8 (`trust` verdict, compact mode, file:line call sites, graph `symbol` fallback, CI gate). Left for a later feature.
- Changes to codebase-memory-mcp.
- A real semantic model download. Mocks only.
- Version bump, tag or release.
- The "Callers through" section for TypeScript structural interfaces. That is `plans/dogfood-fixes` tickets 04-06. This plan only adds the gap and the name-matched sibling rows.

## Happy path

1. The agent edits `core.py` and asks `code.query callers helper` without reindexing. The answer is partial with a `stale-index` gap, and rows in `core.py` are unstated.
2. The agent runs `codeintel index .` and asks again. The cache key has moved, so the answer is fresh, `cached: false` and complete if nothing else is missing.
3. The agent asks `chain "entry -> Cache.save"` and gets the ordered path, or "no call path" with a gap.

## Edge cases

| Trigger | Expected behaviour | Handled in ticket |
|---|---|---|
| Uncommitted `changed` in a non-git dir | `not-a-git-repo`, unavailable | 01 |
| Commit made after the index, outside the diff | `stale-index` | 01 |
| Edit 0.1 s after the index | `stale-index` (no slack) | 01 |
| A row's file was deleted or renamed after the index | row `unstated`, `stale-index` gap | 02 |
| External `codeintel index` while `serve` runs with REINDEX=off | next answer is a cache miss | 03 |
| Index deleted with `reset` while the server runs | missing db changes the key: cache miss | 03 |
| Query on an unchanged tree (MCP) | no `reindexing` flag | 03 |
| `chain A` with no `->` | unchanged neighbourhood answer, hops labelled or `[unstated]` | 04 |
| B ambiguous in `chain A->B` | the backend's candidates are listed, no path claimed | 04 |
| Backend 0.10.x hops that carry `strategy` | labelled as today, no `hop-resolution-unstated` gap | 04 |
| Alias import of the target in a file the text scan cannot read (budget spent) | gap still added ("could not check") rather than none | 05 |
| Nested def with the target's name in a `.py` file that does not parse | fall back to the regex check | 05 |
| `CODE_INTEL.md` is a dangling symlink | refused, never created through the link | 08 |
| `CODE_INTEL.md` symlinked to a file inside the repo | refused: codeintel's own file is never written through a link | 08 |
| `AGENTS.md` symlinked to `.git/config` or `.git/hooks/pre-commit` | refused, target unchanged | 08 |
| `CLAUDE.md` symlinked to a file inside the repo | written through, link kept (existing test) | 08 |
| `project_root` empty on stdio | cwd fallback unchanged; root-not-found only for a non-empty path that is not a dir | 09 |
| Model failure recorded, then `FASTEMBED_CACHE_PATH` set or model files appear | the record no longer applies; an index is tried again | 10 |
| Auto `search` falls back under an RBAC role allowed `search` but not `pattern` | no fallback; the semantic answer is returned as it was | 10 |
| `https://example.com/root/api` under HOME=/root | left unchanged: the home string is redacted only where it starts a path token | 09 |
| Ctrl-C while waiting on the index lock | `interrupted`, exit 130 | 12 |

## Design

```
 query ─► gateway.query ─► freshness key = (generation,
   │        │               graph db mtime_ns, semantic db mtime_ns)   [03]
   │        │                       ▲ core.paths.graph_db_stamp()       [01]
   │        ▼
   │   GraphProvider.build_result
   │        ├─ _dispatch(op) ─► ops/answer/dispatch render rows
   │        │     ├─ blindspots.py rules add gaps / name-matched rows [05,06]
   │        │     ├─ _kind_counts: module scope ─► IMPORT           [07]
   │        │     └─ _op_chain: resolve B, BFS ─► path | no-path    [04]
   │        ├─ row files ─► graph_index_age ─► stale-index gap      [02]
   │        └─ _settle_evidence (gaps force partial, safe=false)
   │   failure path: semantic index-failed ─► gateway falls back
   │        to graph `pattern` with fallback: graph-name-search      [10]
   ▼
 envelope (gaps, evidence, rows) ─► MCP (wire-byte bound) / HTTP  [09]
```

Freshness is computed once:

- `core/paths.py` gains the graph-db lookup: the slug moves there from `setup/reset.py`, plus `graph_db_stamp`.
- `engines/graph/changed_range.graph_index_age` reads the db mtime through that lookup and adds the HEAD commit time.
- `GraphProvider.build_result` stamps `stale-index` on callers/callees/impact.
- The gateway folds the same stamp into its cache key.

No engine imports another engine. The gateway may import core and engines, so the graph-name fallback for `search` lives in the gateway, next to the existing overview fallback.

New gaps go through the existing `_add_gap` → `attach_confidence` → `_settle_evidence` path. Any gap already forces `partial` and `safe_for_destructive: false`, so no second trust system is added.

### Decisions

- **The lint bar. Decided by the user (brief, `Decided:`).** The brief's bar says `ruff check .`. The plan gates on `ruff check src tests bench` instead, which is exactly what CI's `ruff` job runs (`.github/workflows/ci.yml`). This replaces `ruff check .` everywhere in the plan: the bar here, map.md, commands.md's Lint line and ticket 13. The difference: `ruff check .` would also lint `plans/` (including the acceptance tests under `plans/trust-fixes/acceptance/`), `scripts/` and other top-level files that CI never gates. Acceptance tests under `plans/` are not ruff-gated.
- **The build stand-in. Decided by the user (brief, `Decided:`).** The project has no build step. `python -m compileall -q src/codeintel` is the flow's Build command as a syntax check only; it is in neither `pyproject.toml` nor CI (commands.md says so).
- **Where the blind-spot rules live.** Option A: a new module, `engines/graph/blindspots.py`, with pure functions over rows plus file text, called from `AnswerRendering` before evidence is settled. Option B: inline in `answer.py`. Chosen: A. `answer.py` is already 1,470 lines, and pure functions can be tested without a backend. The text reads reuse `qualifier_scan`'s budgeted file reader. Rejected: B.
- **How the nested-shadow check reads source.** Python uses `ast` (stdlib). TS uses the regex `function\s+<name>\b` inside the caller's body span. If a `.py` file does not parse, fall back to the regex. No tree-sitter query is used here.
- **The gateway freshness key.** Option A: `core.paths.graph_db_stamp(root)`. It stats `<graph_cache_dir>/<slug(realpath root)>.db` and `-wal`. If neither exists, it uses the newest `*.db`/`*.db-wal` mtime in that directory, which covers a repo answered from an ancestor project. Option B: ask the graph provider for its cached project resolution, then stat. Chosen: A. It is one or two `stat` calls with no backend call on the query path, and the brief says the stamp lives in core or the graph engine. Rejected: B, which couples the gateway to provider internals. The semantic half of the key is the mtime of the semantic db file, one stat, rather than `SemanticDb.indexed_at`, which would open sqlite on every query.
- **Symlinked context files (T6). Decided by the user (brief, `Decided:`).** Option A: refuse every symlink. Option B: refuse only a target whose realpath is outside the repo root and follow every link inside it. Neither alone is right: A breaks the pinned shared-rules-file behaviour (`tests/test_mapper.py::test_inject_writes_through_a_symlinked_rules_file`, an intended feature), and B still lets a hostile repo link `AGENTS.md` to `.git/config` or `.git/hooks/pre-commit`, which turns an inject into code execution on the next git command. The rule, per file:
  - `CODE_INTEL.md` (codeintel's own generated file, written by `map`, `index` and `code.map`) is never written through a symlink at all.
  - `AGENTS.md`, `CLAUDE.md` and `USING_CODEINTEL.md` are refused when the target or any parent resolves outside the repo root (`core.containment.contained_path`), or resolves inside the repo's `.git` directory. A symlink that resolves to a regular file elsewhere inside the work tree is still followed.
  - What a user loses: a `CLAUDE.md` symlinked to a dotfiles repo outside the project is now refused. That case can't be told apart from the attack. The refusal message names the path so the user can inject by hand.
  - How new files are created: with `O_CREAT|O_EXCL|O_NOFOLLOW`, so a dangling link is never followed (docs: https://manpages.debian.org/trixie/manpages-dev/open.2.en.html).
- **Ctrl-C.** Option A: `except KeyboardInterrupt: print("interrupted"); sys.exit(130)` in `main()`. Option B: reset SIGINT to SIG_DFL and re-raise, so parents see death-by-signal. Chosen: A. The finding asks for exit 130 and one line, and a CLI exit-code test can check it. Rejected: B. Since Python 3.8 an uncaught KeyboardInterrupt re-raises SIGINT (https://github.com/python/cpython/issues/41078), but that path prints the traceback this fix removes.
- **The MCP 40 KB bound.** Measure `_size` on the bytes the server actually sends. For a non-string tool result, the installed `mcp` 2.3.0 builds the `TextContent` text with `pydantic_core.to_json(result, fallback=str, indent=2)` (`.venv/lib/python3.13/site-packages/mcp/server/mcpserver/utilities/func_metadata.py`, `_convert_to_content`). Option A: `_size` calls that same `pydantic_core.to_json(obj, fallback=str, indent=2)` (pydantic_core is already installed as an mcp dependency). Option B: `json.dumps(obj, default=str, indent=2)`, which escapes non-ASCII and so only approximates. Chosen: A, exact and no new dependency; a test runs the real `_convert_to_content` on a compacted envelope and checks its byte length. Rejected: B. The MCP spec sets no size limit, and Claude Code warns above 10,000 tokens of tool output (https://modelcontextprotocol.io/specification/2025-11-25/server/tools.md, https://code.claude.com/docs/en/mcp), which is why the 40,000-byte bound stays.
- **The index age check.** Compare HEAD's committer time (`%ct`, https://git-scm.com/docs/pretty-formats) with the db mtime, and drop the 2 s slack (`written < newest`). Rejected: a sidecar `<project>.db.started` written by `codeintel index`. It needs a new file the backend does not know about, and `%ct` plus no slack closes both findings.
- **The `changed` (no target) git check.** Use `changed_range._git(root, "rev-parse", "--is-inside-work-tree")`, the same helper range mode uses.
- **Where the backend-error leak is fixed.** Strip project-id lists from the backend-error text in the graph engine (`engines/graph/backend.py`/`provider.py`), so every transport is covered. Rejected: an HTTP-only fixed message with a request id. It covers one transport, and the engine-side strip already removes the leak.
- **The reindexer's change detection.** A cheap fingerprint per root: HEAD sha plus the max mtime over `git ls-files -m -o --exclude-standard`. `maybe_reindex` returns whether it submitted a pass. For a non-git root, keep the time debounce.

## Interfaces

Gaps (in the envelope's `gaps[].kind`): `aliased-import-unbound`, `dispatch-sibling-unbound`, `target-ambiguous` (existing, now also for a nested shadow), `stale-index` and `index-age-unknown` (existing, now on callers/callees/impact/uncommitted changed), `no-path-found`, `hop-resolution-unstated`, `import-only-callers`.

Reasons (`reason`): `not-a-git-repo` (now also uncommitted `changed`), `target-not-found` (chain B), `root-not-found` (MCP/HTTP `code.query`), `project-not-indexed` (backend "project not found").

Envelope field `fallback: "graph-name-search"` on a `search` answer served by the graph.

Entry points for tests:

- CLI: `codeintel query --op <op> --target <t> --json [--engine graph] [--project-root R]`, `codeintel map [--inject]`, `codeintel index`, `codeintel reset [--semantic-only]`, `codeintel doctor --json`, `codeintel status --json`, `codeintel prompt`.
- MCP: `transports.mcp.code_query_handler(args)` and `compact_for_mcp(env, op, target)`.
- HTTP: `GET /code/status`, `POST /code/query` (`transports/http.py`).
- Python:
  - `GraphProvider.build_result(op, target, files, budget, project_root)`
  - `Gateway.query(...)`
  - `ResultCache.get/put`
  - `Reindexer.maybe_reindex(root) -> bool`
  - `Reindexer._do_reindex(root)`, with `_graph_reindex(root)` and `_semantic_reindex(root)` as the two passes a test may replace to read their order
  - `core.containment.write_contained(root, name, content, *, follow_links)`
  - `core.redact.redact_text(text)`
  - `core.paths.graph_db_stamp(root)`
  - `engines.graph.changed_range.graph_index_age(project, root, paths)`
  - `MapGenerator.write(root, content) -> (path, wrote)`
  - `Injector().inject(root, create=...)`

## Migration and compatibility

- Answers that were `complete` may now be `partial`, on purpose. The heading wording for callers counts changes (tests and `bench/score.py` row parsing must keep reading `- ` lines as rows).
- `reset` keeps its default (both indexes). `--semantic-only` is new.
- `Reindexer.maybe_reindex` now returns `bool`. Existing callers ignore the value.
- The cache key changes shape (str instead of int). The cache is in memory, so nothing persisted migrates.

## Risks

- **New gaps make most answers partial.** That is cry-wolf, which the existing `_confidence_note` docstring warns about. Mitigation: each rule fires only on positive evidence (an alias form seen, a sibling with a name-matched row, a nested def seen), never on "might".
- **Staleness stat cost on big answers.** Mitigation: one `stat` per distinct file, capped at the rows already in hand.
- **Removing the 2 s slack makes `stale-index` fire on coarse-mtime filesystems.** Mitigation: comparing against the db mtime with `<` is still correct, since a file written in the same second as the index is genuinely unknown. The test uses `os.utime` to set exact times.
- **Live backend differences (0.9 vs 0.10).** Mitigation: unit tests use the monkeypatched `_run` fake with rows captured from 0.9.0. Live fixture tests skip when the backend is absent, as `test_graph_real.py` does.
- **The plan overlaps the open `plans/dogfood-fixes` tickets 05, 06 and 08.** Mitigation: see the map's Open questions.
