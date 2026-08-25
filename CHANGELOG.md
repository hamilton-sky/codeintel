# Changelog

All notable changes to codeintel are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **A search hit that lands inside a function now says which function.** A hit renders as
  `path:line | <first meaningful line>`, which works when a chunk starts at a definition — but a
  def longer than `max_chunk_lines` is window-split, so most of its chunks open mid-body and the
  preview shows whatever line the window happened to start on. Real queries against this repository
  returned `searcher.py:373 | continue` and `searcher.py:383 | except Exception as exc:`: correctly
  located, and useless, because nothing said which function that was. Measured with `ast` across
  the indexed repositories, 11–33% of Python chunks start strictly inside a definition rather than
  at one. The parser already knows the enclosing def when the chunk is cut, so it is recorded
  (`chunk_hashes.chunk_symbol`) and the preview leads with it — `searcher.py:373 | search() …
  except Exception as exc:`. The symbol index is a full walk of the parse tree rather than the
  chunk spans, so the **innermost** definition wins (a method reports the method, not its class),
  and it covers tree-sitter languages including `const X = () => {}` components. A file that falls
  back to line windowing records no symbol: guessing one by scanning backwards for a `def` would
  confidently name the wrong function. Migrates and backfills in place exactly like `chunk_end`,
  with no re-embedding — after one pass, 469 of 469 mid-function chunks in this repository name
  their function.

### Fixed
- **A search hit is now verified to still describe the code it was indexed from.** `chunk_hashes`
  stores a chunk's *line number*, and the snippet has always been re-read from the current file at
  that line — so once a file was edited, a hit pointed at whatever now occupied those lines.
  Deleting a `charge_credit_card()` at line 1 made a search for "charge the credit card" return
  `app.py:1 | import logging`, ranked first and reported as `confidence: complete`. An agent has no
  way to doubt that, which makes it worse than an empty result — and it is the exact failure the
  engine's partial/complete contract exists to prevent. Each chunk's span is now recorded
  (`chunk_hashes.chunk_end`) and re-hashed at query time; a chunk whose source no longer matches is
  **withheld** rather than shown, counted, and reported as a `freshness` gap that marks the answer
  `partial`. When every match is stale the result is `reason: 'index-stale'` with a re-index hint,
  no longer conflated with `'below-floor'` ("nothing matched"). Verification is per-chunk, so
  editing one function does not blind the rest of its module, and the text it reads is reused by
  the reranker — the check costs no extra file reads. Existing caches migrate by `ALTER`, not a
  rebuild: an ordinary index pass backfills every span in place with **no re-embedding**
  (measured: 3,971 spans backfilled on a 86k-chunk cache, zero vectors recomputed).
- **Search no longer silently discards result slots on code-heavy repositories.** The code/prose
  interleaving capped code at two thirds of the display budget unconditionally, but never handed
  the remainder back when prose couldn't fill it: 58 code hits and 2 prose hits returned 8 results,
  dropping 52 qualifying code hits to leave four slots empty. It fired on 8 of 18 sampled queries
  against real repositories — losing up to 3 of 10 results — and hardest exactly where code hits
  are most plentiful, inverting the intent of ranking code first. The prose share is now a ceiling
  rather than a quota (0 of the same 18 queries under-fill).
- **`rerank_candidates` now does something.** The semantic provider applied its own hardcoded
  `display_k * 6` widening on top of the config key, and `Searcher.search` takes
  `max(k, rerank_candidates)` — so every configured value at or below 60 was swallowed. The
  documented default of 30 changed nothing, every query performed 60 file reads instead of 30, and
  the `_RERANK_CANDIDATES_CAP` guard was bypassed by the provider's own over-retrieval. Candidate
  breadth is now this key alone, clamped to the cap. The default moves 30 → **60**, the value that
  was really in use, so fixing the wiring doesn't silently halve anyone's retrieval.
- **`CODEINTEL_HOME` now actually redirects everything it claims to.** The variable exists because
  `Path.home()` raises on a host with no resolvable home directory — a container UID with no passwd
  entry, which is routine for an MCP server launched by an agent. Only the semantic cache honoured
  it: `config.py` and `auth.py` each called `Path.home()` inline, so on such a host `load_config()`
  raised `RuntimeError` **even with `CODEINTEL_HOME` set** — the documented escape hatch did not
  work in the one environment it was written for, and the failure surfaced several layers away as
  an opaque `provider-error`. Resolution now lives in one place (`codeintel.paths.codeintel_home`)
  and covers the cache, the global `config.toml`, and `auth.toml`. A host with nowhere to look
  falls back to defaults with RBAC off, rather than crashing — absent config is not an error.
- **A deleted file is no longer reported as a suspected symlink attack.** `contained_path` returns
  "don't read this" for a missing file (correctly — `_cleanup_deleted` relies on it to reconcile
  deleted rows away), but `open_contained` attributed *every* refusal to a planted link: each
  ordinary deletion between indexing and a query logged a WARNING asking whether a symlink had been
  "planted after indexing". False alarms on the most routine event in the system are how real ones
  get ignored. A path that does not exist now raises `FileNotFoundError` and logs at debug; genuine
  escapes (including dangling symlinks) still raise `ContainmentError` and still warn.

### Internal
- **The test suite no longer reads or writes the real `~/.codeintel`.** Tests that indexed a
  fixture repo without redirecting the cache left a project root behind on every run — ~90 dead
  `pytest-of-<user>` entries had accumulated, each of which `doctor` reported as a healthy indexed
  project. The reverse direction was worse and silent: `load_config` merges a machine-wide
  `config.toml`, so a developer with one on disk ran the suite against different defaults than CI,
  and a green local run proved nothing about the shipped values. An autouse fixture now points
  `CODEINTEL_HOME` at a per-test temporary directory, so isolation is not something an individual
  test can forget. (Existing orphaned rows are cleared by `codeintel reset --all`, or by re-running
  against a fresh cache; they are inert either way, since rows are partitioned by `project_root`.)

## [0.17.0] — 2026-08-24

### Added
- **`codeintel index` now shows live progress instead of going silent for minutes.** A large repo
  used to print skip-warnings and then nothing until `Indexed N chunks` — impossible to tell "working"
  from "hung," so people Ctrl-C'd healthy runs. It now renders a phase checklist in the same visual
  language as `doctor` (`✓` glyphs, one line per phase): a `scan + chunk` liveness counter, a real
  `embed  3,980/6,381 chunks  62%` bar (the total is known before the batch loop), a distinct
  `loading embedding model…` state so the first-run model download doesn't read as a second hang, and
  a ticking `graph reindex  12s → 47s` elapsed heartbeat (a background thread; the step is an opaque
  subprocess, so a moving number is the honest signal and no percentage is fabricated). A dim header names the model and chunk
  strategy (`BAAI/bge-small-en-v1.5 · def-aligned chunks`) — it says the work is local and pre-explains
  the download. On a TTY the active line redraws in place (throttled); piped/CI output prints one clean
  line per phase with **no** carriage-return or ANSI bytes; `--quiet`/`-q` suppresses progress and the
  header while keeping the result line. Skip warnings (a null-byte file, a symlink escape) print as
  clean permanent lines *above* the redrawing status line — routed through the counter so they never
  collide with it mid-line. The progress is inert to the result: the indexer emits through
  a null-safe, never-raise sink (`progress.ProgressSink` + `_Guard`), so a broken renderer can never
  change the indexed count — proven by a progress-on-vs-off count-invariance test. `index` also gained
  `--no-color`/`--ascii` (it was the one command missing them).

### Changed
- **`codeintel reset <repo>` now clears BOTH engines, so a single repo is truly "as if never indexed".**
  It used to drop only the semantic index and leave the repo's graph index behind — still listed by
  `list_projects` and still answering graph queries — so a real clean slate was only possible with the
  global `--all`. Scoped reset now also removes that one project's graph index (the backend's
  per-project `<slug>.db` and its `-wal`/`-shm`/`.corrupt` siblings), matching what `--all` does
  globally. It stays pure file-removal — no backend spawn, works with the graph engine down — and the
  slug matches the backend's real naming (runs of non-alphanumerics collapse to one `-`; a naive
  `/`→`-` replace double-dashed odd paths and silently left the file, leaking the very index the reset
  was meant to remove). The report now says `removed N chunk(s) and the graph index for this project`.
- **The "looks binary despite its extension" index-skip warning now says WHY and how to fix it.** A
  source file gets skipped when it carries a raw NUL byte (the same rule git uses), which in a `.ts`
  or `.py` is almost always a deliberate separator (`.join('\0')`, a composite key) saved as a byte
  instead of the `\0` escape. The old message left the user to guess; it now reads *"contains a null
  byte at line N … write it as the `\0` escape instead of a raw byte and re-run index"* — the exact
  line and the exact fix.

## [0.16.0] — 2026-08-20

### Changed
- **`callees` stopped subtracting the ambiguity out of its answer and started reporting it.** The op
  resolves its target by unqualified name, so a repository with four methods called `invoke` returned
  the edges out of all four flattened into one list. On the pinned corpus repo that was literally
  `## Callees of invoke (34)` — thirty-four rows belonging to four different functions, presented as
  one symbol's callees, with `confidence: complete` and no gap. `callees` feeds "is this symbol safe
  to change?", where a list read as complete is the dangerous direction.

  Rows are now GROUPED by the symbol they came out of. Each group renders under a heading naming
  that symbol and its file, the number of same-named symbols is stated in the body before any row,
  and nothing is dropped for being ambiguous — the same 34 rows come back as four labelled answers
  with a `target-ambiguous` gap and `confidence: partial`. Three symbols named `handle` is not a
  degraded answer, it is a question, and `chain` already asked it (it renders the backend's own
  `status: ambiguous` as a candidate list); `callees` now does the same in its own voice.

  Grouping also makes the language-family check structural. `0.15.5` fixed that check by hand — it
  compared each callee against a UNION of every matched caller's family, so a `.ts` collision reached
  from a Python caller survived on the strength of an unrelated TypeScript caller three rows down.
  A row can now only be compared against its own group's caller, so that bug is no longer
  expressible here rather than merely fixed.
- **`callers` now groups by the symbol being called, the way `callees` groups by the symbol doing the
  calling.** It was given the disambiguator and not the grouping, on the argument that a merged caller
  list over-reports and over-reporting is the safe direction for "is this symbol safe to change?".
  That argument is about the SET, not about the answer: a reader told "3 callers of `invoke`" cannot
  tell that one of the three calls a *different* `invoke`, and believing a caller exists that does
  not is a wrong fact whichever direction it errs in. It also left `impact` internally inconsistent —
  one half grouped per symbol and the other flat, from a single target — so a reader would see the
  callees attributed per symbol and read the callers list as belonging to whichever one they were
  looking at.

  Both ops now render through one `_render_edge_answer`, for the reason the repo-scan ops share
  `_render_scan`: the drift-prone parts are the count in the heading, the ambiguity disclosure and the
  truncation note, and two copies of those is how one op ends up honest and the other silent. A test
  reads both ops' bodies out of the AST and fails if either builds its own answer instead.
- **The unmatched-hint message stopped overstating what it knew.** It said "Nothing in this index
  matches `…`", which is a second false claim inside a message written to avoid the first: the
  population it can speak for is the symbols its own query returned, not the index. On a real
  repository `Group.invoke` has callees and no callers, so it is genuinely indexed and genuinely
  absent from the callers list. It now says no symbol matching the hint has `callers` **here**, and
  that this says nothing about whether the symbol exists.

### Added
- **`codeintel prompt` — a paste-to-your-agent setup prompt, tailored to this machine.** `setup`
  DOES the bring-up and `install` REGISTERS the MCP server; this hands the same job to a coding agent
  instead, for people who would rather onboard by pasting one prompt than by reading the docs and
  running commands. It runs a `doctor` probe and emits ONLY the outstanding steps — a satisfied
  engine is never named as something to install, and a fully-healthy, already-registered machine
  reduces to "just restart me" — which is what makes it worth a command rather than a static README
  block. `--fresh` ignores local state and prints the full sequence from `pip install` (portable,
  path-agnostic) to send to a friend on a clean machine, and `--agent` targets a specific host
  (default `auto` picks the one you have, and names none it cannot find rather than guessing). The
  prompt is written to stdout so `codeintel prompt --fresh | pbcopy` copies exactly it; the
  "paste this" note goes to stderr. `doctor` and `setup` point at it whenever a setup step is still
  outstanding, so it is discoverable without knowing the command name. The step-tailoring is
  unit-tested against synthetic doctor reports, and the render is a pure function so those tests need
  no backend.
- **A target may now say WHICH symbol it means**, so ambiguity can be resolved instead of only
  disclosed: `core.Group.invoke` matches on a segment-aligned qualified-name suffix, and
  `invoke@src/click/testing.py` (or a bare `invoke@testing.py`) matches on file. Both are text the
  previous answer already printed on its result lines, which closes the loop: the ambiguous answer
  names the candidates and shows the syntax to pick one. A narrowed answer carries no ambiguity gap
  and reports `complete`, because it is.

  Applied to rows in hand rather than pushed into the Cypher `WHERE`: a suffix match needs a string
  predicate, and this backend's dialect is not an interface this project can pin — the 0.9→0.10
  wire-format break is the standing reminder. `callers` honours the same hint, applied to the far end
  of the edge, because `impact` is callers + callees on one target and narrowing one half only would
  pair one symbol's callees with a different symbol's callers — an answer that reads as precise while
  being about two different functions. Rejected: making a bare name refuse until disambiguated. The
  merged answer over-reports rather than under-reports, and breaking every existing caller to fix a
  disclosure problem is the wrong trade.
- **A hint that matches nothing says so, and names the symbols that do carry the name.** Falling
  back to every symbol with that bare name would answer a question the caller explicitly narrowed
  away from; reporting zero callees would be a claim about the code rather than about the lookup.
  "I could not find the symbol you named" and "that symbol calls nothing" are opposite answers, and
  only one of them is about your repository.

### Removed
- **`deadcode` is retired: the implementation is deleted, and the `CODEINTEL_ENABLE_UNVERIFIED_OPS`
  opt-in with it.** The op was withdrawn pending one condition — *"it returns when a labelled corpus
  measures its precision and recall, not before"* — and that measurement is what retired it.

  The corpus: two pinned real Python repositories (`pallets/click`, `psf/requests`), every function
  and method collected from the **AST** rather than a regex, 2,425 definitions, `async def` and class
  methods included. Each labelled live or dead with the reference behind the label recorded beside it,
  by an oracle that errs toward LIVE — a decorator, a dunder, an override of an interface declared
  outside the tree, a string-dispatch mention or public-API status is each enough — so "dead" is only
  what survives all of them, and the numbers come out against the op rather than for it. Known-answer
  canaries are planted in both trees because otherwise recall has no denominator at all.

  **Precision as shipped: 6/24 = 25%.** Recall 60%. And the measurement that actually decided it:
  restricted to real code with the canaries removed, the op named **18 candidates across the two
  repositories and every single one was live** — all 18 Makefile targets, which the graph backend
  indexes as `Function` nodes.

  Both repairs were measured before the decision, not assumed away. Requesting `Method` nodes (the
  fix `hotspots` already had) takes recall to 80% and precision to 67%; excluding interpreter-called
  dunders takes precision back to 100% on `click`; restricting candidates to code files (the fix
  `changed` already had, from `0.15.4`) reaches **89% precision and 80% recall** on the planted set.
  On real code that repaired version names exactly one candidate, and it is
  `MockRequest.get_type` in requests — a method `http.cookiejar` calls by duck-typed convention,
  whose name appears once in the source.

  That last false positive is why the repair stops there. The verification was a name-frequency scan,
  so it fails on exactly one condition: a symbol whose name appears once and is called by a
  convention outside the source. Two repositories produced three distinct instances of it (non-code
  nodes labelled `Function`, dunders, stdlib duck-typed protocol methods); the 2026-08-17 TypeScript
  evidence adds a rollup plugin hook and object-literal properties. The set is not enumerable — no
  specification lists `get_type` — and every repository added revealed a new member. Weighed against
  that: in 2,425 real definitions across two maintained repositories there was **not one** dead
  private symbol to find. An op whose measured yield on real code is zero true positives has no
  benefit to set against that error rate.

  Reinstating narrowed was the option most seriously considered — `click` alone measured 100%
  precision, and `Function`-plus-`Method`-minus-dunders on Python looked defensible. Adding the
  second repository is what refused it, which is also the argument for `n>1`: a decision to hand an
  agent a delete list on the strength of one repository is the pattern the withdrawal existed to
  break. What survives is the `_WITHDRAWN_OPS` entry, so the op name still explains itself with a
  hint naming `callers` as the accurate substitute, and the corpus machinery, so the decision can be
  re-opened by measurement rather than by argument.

  The escape hatch went with the code: a flag that enables nothing is a promise the product cannot
  keep, and worse than no flag, because a reader sets it and believes something changed. The gate is
  now unconditional, and three tests hold the pieces together — a withdrawal hint may only advertise
  the opt-in if `build_result` still consults it AND the op still has an implementation; the corpus
  job must not export a flag that enables nothing; and an op declared in `_GRAPH_OPS` with no dispatch
  route must have a `_WITHDRAWN_OPS` entry, or it answers `unsupported-op` and sends an agent looking
  for a different tool.

### Fixed
- **The live test suite leaked a backend project registration per indexed tmp repo and never cleaned
  one up.** Several tests (RBAC scoping, the MCP handshake, `code.query` over a real index, the
  reindexer) build a repo under pytest's `tmp_path` and index it into the real codebase-memory-mcp
  backend; nothing deleted it, so `list_projects` on one dev machine had accumulated **572** dead
  `pytest-of-<user>` registrations, one per test run — noise, and a standing source of the
  stale-index confusion the graph engine already fights. A session-scoped reaper now deletes them at
  teardown (which also clears the backlog from earlier runs). It matches only roots containing
  `/pytest-of-`, a segment a real checkout can never hold, and the selection is unit-tested to prove
  it can never name codeintel, the corpus, or a user's own repo — the one way this fixture could do
  harm.
- **`callers` counted the backend's module-scope pseudo-nodes as callers, so half a real count was
  fiction.** `callers invoke` on the pinned corpus rendered `## Callers of ... (4)` for
  `src.click.core.Context.invoke` and listed `src.click.core.__file__` and
  `src.click.decorators.__file__` as two of the four. `__file__` is not a function anyone can call:
  the backend has no node for code that runs at module or class-body scope, so it hangs those edges
  off a whole-file container — a `File` node whose qualified name it synthesises as `<module>.__file__`
  and, for the same file, a `Module` node named after it. Both reached output verbatim as symbols, so
  a reader trusting "4 callers" was reading two call sites that do not exist. Invisible to 818 unit
  tests and immediate on the first real repository, which is why the guard is now a corpus invariant.

  Three answers were weighed against what the backend actually emits, not against the description of
  the bug:

  - **Drop the rows.** Rejected. The edge is real — `core.__file__` references `builtins.len` and
    `exceptions.BadParameter` from another file, which is module-scope code, not a containment
    artifact — and dropping manufactures false absences: **147 symbols on this corpus are referenced
    ONLY from module scope** (`_check_iter`, `Parameter.make_metavar`, `_param_memo`, …), so dropping
    would report a live, referenced function as having zero callers, the "safe to delete" misread this
    project retired `deadcode` over.
  - **Relabel the rows as the location they are.** Chosen. Each pseudo-node now renders as
    `- module scope of src/click/core.py`; the edge and the count survive, and nothing asserts a
    symbol that does not exist. `invoke` stays at 4 — two methods plus two files' module scope — with
    no `__file__` anywhere. Because nothing is dropped, the answer stays `complete` rather than
    growing a gap.
  - **Leave `Module` nodes alone** (only `__file__` is an obvious fiction). Rejected. `src.click.core`
    rendered as a caller is the same class of fiction with a less obvious name, and the label
    population is DERIVED from the live graph rather than typed — the corpus test asserts every
    caller-side label that is not a `Function`/`Method` is one this fix handles, so excluding `Module`
    would have failed the project's own no-hand-typed-population rule.

  A single file can carry BOTH the `File` and the `Module` representation of its one module scope (38
  target/file pairs do here), so the two collapse to one row rather than double-counting. The filter
  lives on the displayed side of the shared `_render_edge_answer` path and both edge ops reach it;
  `callees` is a deliberate no-op today because the backend never emits a container node as a callee,
  kept symmetric so a future one is handled without a second fix. One limitation left standing and out
  of scope: a `Module` node can carry a mis-attributed non-code `file_path` (the backend labelled
  `examples/aliases/aliases.py`'s module scope as `aliases.ini`, a sibling file), so a relabelled row
  can name a `.ini`; the references there are genuine click-API uses, so relabelling is honest and
  dropping them as "non-code" — the `callees` collision filter's separate domain — would have lost
  real edges.
- **`callers` reported cross-language and non-code name collisions as callers; the collision filter
  `callees` had was never lifted to it.** `callers X` matches by the bare name `X`, and the extractor
  emits an edge for a bare local name, so a `.ts` function three files over or a node in a `.json`
  that merely shares the name became a "caller" of a Python symbol. A call edge cannot cross a
  language family without an FFI/IPC mechanism the extractor does not emit, and a data file defines no
  caller, so both are collisions — the same ones `callees` has always dropped, resolved per group
  against the CALLED symbol's language. The two ops now share one `_drop_edge_collisions` and one
  disclosure path (`_collision_note`, `_empty_edge_answer`), so a filtered or fully-emptied answer
  discloses its count and reason in the body identically in both directions — a `callers` answer
  emptied entirely by the filter says "found N, all filtered", never "0 callers", which reads as safe
  to delete. Module-scope rows are exempt from the drop and left for the relabel above, because the
  backend mis-attributes their paths (the `aliases.ini` case) and the references behind them are real.
  On a pure-Python repository the filter is dormant, as it should be — nothing to collide.
- **A `callees` answer emptied entirely by the collision filter reported the dropped count in
  `gaps` and nothing in the body.** The gap said `1 row(s) were dropped`; the body said
  `(no callee survived name-collision filtering)` with no number anywhere in it. `attach_confidence`
  states the rule this broke — the engine "is expected to have said the same thing in the body text,
  because that is the field an agent actually reads" — so a count that lives only in the JSON is a
  promise kept to the schema and broken to the reader. Both the count and the reason now render in
  every partial case, and a new test derives the population of `callees` gap kinds from `graph.py`'s
  **AST** and fails if any of them reports a number the body does not repeat. A hand-typed list
  there would keep passing on the day a new gap kind ships with no body text behind it.
- **A symbol whose name is also a file extension kept the backend's project id — the host's absolute
  path — in rendered output.** `requests.models.Response.json` is a method, the most-called one in
  that library, and `_strip_project_prefix`'s filename guard read the trailing `json` as an extension
  and returned the name unstripped: `private-tmp-codeintel-corpus-requests.src.requests.models.Response.json`
  in a `hotspots` row, where in ordinary use that prefix is the user's home directory. `Renderer.html`,
  `Config.toml` and `Query.sql` are the same shape.

  The guard exists for a real reason — `use-toast.ts` rendered as `ts` on two repositories — and no
  rule on the string can separate `my-component.spec.ts` from `Response.json`; they are the same
  shape. What separates them is the FIELD the value came from: a filename arrives in a row's `name`
  (which is why the guard was added, for `changed` rows, which carry `name` and no `qualified_name`),
  while a `qualified_name` is a module path whose last segment is a symbol. So the guard is now the
  caller's to claim, defaulting to ON so an unexamined call site keeps today's behaviour, and every
  site handling a qualified name turns it off.

  Found by adding a second repository to the corpus — not by reasoning about the string — which is
  the same lesson as every other fix in `0.15.x`. The enumeration test that already swept the module
  for renderers emitting a raw qualified name now has a companion that sweeps for renderers stripping
  one with the filename guard left on, and both were upgraded to scan logical lines: a line break
  through the middle of a call had been enough to silence them.
- **A callee list truncated by the query's own row cap read as a complete answer.** The Cypher ends
  in `LIMIT 50`; a result that came back with exactly 50 rows had almost certainly been cut short,
  and nothing in the rendered list said so. It now discloses the truncation in the body and carries a
  `row-cap-reached` gap, and does not claim the target is the only symbol by that name — a symbol
  whose rows fell past the cap is indistinguishable from one that is not there. A list one row short
  of the cap is a complete answer and is left uncaveated.

### Tests
- **The corpus harness gained a second pinned repository and a labelled dead-code oracle.**
  `psf/requests` joins `pallets/click`; every corpus invariant now runs against both, which is how
  the `Response.json` path leak above was found, and how the `deadcode` measurement avoided being
  decided by one repository. The oracle derives its population from the AST, labels every definition
  with recorded evidence, and is itself checked for non-vacuity against planted known-answer canaries
  — an oracle whose liveness rules fire too eagerly labels a whole tree live and then "proves" any
  dead-code heuristic worthless, so the canaries are the control. The `async def` canary has its own
  assertion, because the human verification that once cleared a tree used `^\s*def ` and could not
  see 33 of its 66 functions.

  What remains measurable after the retirement is the SIGNAL rather than the op: the graph's raw
  in-degree-0 symbol set, scoped as `deadcode` scoped it. The test asserts the premise the decision
  rested on — that this signal is majority-live on real code — rather than the historical numbers,
  which would churn on every backend release. If it ever fails, the premise has stopped holding and
  the decision is worth re-opening with fresh measurements. That is a result, not a flake.
- **The most destructive path in the product was the least tested.** `reset.py` sat at 60%, the
  lowest in the codebase, and the uncovered lines were not scattered — they were the deletions.
  The whole of `_reset_graph_cache`, which removes every per-project graph index, and the entire
  `--all` branch of `run_reset`, the nuke-everything path, had never been executed by a test. On an
  irreversible command, line coverage is less "how much of the intended behaviour runs" than "how
  much of this deletion have we ever watched happen", and the one command a user reaches for when
  things are already broken had the least evidence behind it. Now 100%, with the deleting paths
  watched deleting.

  The property asserted first is `apply=False`: a dry-run reports what it would remove and removes
  nothing. Checked against a recursive walk of the whole temp tree with content digests rather than
  against the files the report happens to name — so a deletion nobody thought to list, or a
  truncate-in-place, fails it — and paired with a count assertion, because "it deleted nothing" is
  only evidence if it also found something. Then: `--all` clearing both caches while sparing the
  backend's `_config.db`, which is registration rather than an index; an unreadable graph directory
  reported as NOT cleared instead of as clean; a **real** exclusive lock, waited out, proving a busy
  database comes back as an error rather than being discarded, since deleting it would destroy a
  healthy index to recover from a lock; sqlite-vec failing to load; a `close()` that raises after
  the delete has committed; and a graph cache that can be listed but not written to.

  Every path resolves through overrides the product already honours — `CODEINTEL_HOME` and
  `CODEBASE_MEMORY_HOME` — so these are real deletions of real files. Mocking `os.remove` was
  rejected: it would have tested the plumbing and watched no deletion at all. Two guards keep them
  inside `tmp_path`: the resolved paths are checked before anything runs, and `os.remove`/`os.unlink`
  are wrapped for the duration to refuse — and record — any deletion aimed outside. Wrapped rather
  than merely asserted because reset's never-raise contract swallows exceptions from its own
  deletion attempts, so an `AssertionError` raised in there would vanish into an
  `except Exception: pass`; the recorded list is checked at teardown, where nothing can swallow it.
  That guard is itself of the "X never happens" shape this project has learned to distrust, so one
  test aims a deletion outside the sandbox and proves it is refused rather than only noticed.

  These tests pass against correct code, which makes "it passes" no evidence at all. Each was
  shown to fail against a deliberately broken `reset.py` — `apply` ignored in `_reset_all` and in
  `_reset_graph_cache`, `_config.db` no longer spared, the graph sweep dropped from `--all`, a lock
  classified as corruption, `_discard_cache_file` claiming success on a failed unlink — six
  mutations, six caught, none surviving. One thing deliberately NOT asserted: that a dry-run leaves
  sqlite's own `-wal`/`-shm` siblings in place. Opening a WAL database checkpoints and clears a
  stale journal, so the scoped dry-run, which opens the db to COUNT it, legitimately removes one.
  Measured while writing these, not assumed; the durable cache is what a dry-run must not touch.

### Documentation
- **Every live mention of `deadcode` now says it is retired rather than withdrawn-but-runnable**, and
  the sections describing machinery that no longer exists are gone: `docs/graph.md`'s "Why `deadcode`
  re-reads the source" described a source-verification pass that has been deleted, and the README's
  escape-hatch paragraph offered a flag that no longer exists. The README carries the measurement in
  full — the population, the oracle's bias, both repairs, and the one number that decided it.

  `docs/deploy.md`'s RBAC allowlist example **drops** `deadcode`, reversing the call `0.15.5` made.
  That call was to keep it listed so `reader` would not be silently denied the op "the day it is
  reinstated"; there is no such day now, so listing it advertises a capability that cannot exist. The
  comment records both the reversal and its reason rather than leaving a silent edit.

  And the drift test learned its missing direction. Every existing check read `_WITHDRAWN_OPS` and
  looked for the word "withdrawn", so on the day an op is REINSTATED they would all keep passing
  while the docs still told a reader it refuses to run — the same shape as the README's CI claim that
  `0.15.5` had to correct, pointing the other way. A table row naming an op that does run must now
  not call it withdrawn.

## [0.15.5] — 2026-08-19

### Added
- **`status` and `doctor` now report when this process is serving code older than what is
  installed.** Upgrading the package replaces files on disk and changes nothing about a running
  server, which keeps answering from the module it imported at startup. Nothing goes red — every
  engine stays green and `status` reports the stale version as though it were the truth — so a fix
  that is committed, released, installed and readable in this file can be absent from every answer
  the user actually gets. Not hypothetical: it happened while releasing `0.15.4`, where the server
  kept serving `0.15.3` after the install succeeded.

  Detected by re-reading `__version__` out of the very file the module was loaded FROM, at call
  time. `importlib.metadata` is the obvious route and the wrong one: it answers "what does the
  installed distribution claim", which is the same number for a fresh process and a stale one, and
  its path caches make the negative case unreliable. Parsed with `ast` rather than imported —
  re-importing would either hand back the cached stale module or execute freshly-installed code
  inside a process running the old version, and a health check should do neither. Every uncertain
  case (missing file, unparseable source, non-literal `__version__`) stays silent: a false "restart
  your server" prompt costs more trust than a missed one. Surfaced on `code.status` as
  `version_skew`, and as a `fix:` note in `codeintel status` / `codeintel doctor` — the check is
  worthless in a field nobody reads.

### Fixed
- **`callees` could keep a name-collision row instead of dropping it, when the collision was a
  cross-language match reached from a DIFFERENT caller than the one on the row.** `target` is a bare
  name, so a `callers`-matching query can return edges from more than one distinct symbol sharing
  it. The collision filter unioned every matched row's `a.file_path` into one `caller_families` set
  and checked each callee's language against that set — so a `.ts` name-collision callee reached
  from a *Python* caller of `target` survived, because the set also contained `ts-js` from an
  unrelated TypeScript caller of the same bare name, three rows down. Each row already carries the
  one caller file it actually came from; the check now compares a row's callee language against that
  row's own caller, not the union across every caller the bare name happened to match.

### Documentation
- **`README.md` and `docs/` still presented `deadcode` as a working, source-verified capability
  after `graph.py`'s `_WITHDRAWN_OPS` pulled it** — the capability table, a dedicated "candidate
  list, not a delete list" section, the "be careful with" table, the project-status paragraph, and
  `docs/graph.md`'s op table and op-detail section all read as if the op ran. A user acting on the
  README would reach for an op that refuses to run and get no explanation why. Every mention now
  says the op is withdrawn, why in one line, that `callers` on a specific symbol is the accurate
  substitute, and the `CODEINTEL_ENABLE_UNVERIFIED_OPS=1` escape hatch with its risk — rather than
  silently dropping the capability-table row, which would tell a returning user nothing happened to
  it. `docs/deploy.md`'s RBAC allowlist example still lists `deadcode` as an op a role may run:
  withdrawal is enforced server-wide regardless of role, so listing it grants no capability today,
  and removing it would silently deny it to `reader` the day it is reinstated — kept, with a note
  explaining both facts, rather than removed.

  A hand-typed doc-vs-code population is exactly the defect class `0.15.4` fixed for `source_kind`
  a day earlier; a new test derives the withdrawn set from `_WITHDRAWN_OPS` by import (never a
  literal op name) and fails if any live doc mentions a withdrawn op without also saying so.

- **The README understated what CI verifies.** It said the graph and LSP backends were "not
  installed in CI, so those two engines are exercised against hand-authored mocks rather than the
  real wire contract" — true when written, false since the contract jobs were added. `graph-contract`
  installs the pinned `codebase-memory-mcp` and fails if its live tests skipped; the nightly corpus
  job runs that same backend against pinned third-party repositories; `lsp-contract` runs live
  serena tests but is `continue-on-error`, so the LSP wire contract is watched rather than gated,
  and the README now says which is which. The release canary remains semantic-only — that half of
  the claim was checked and is still accurate.

  This is the `deadcode` drift pointing the other way: there the docs promised a capability the
  product had withdrawn, here they denied assurance the product had gained. A new test derives the
  contract jobs and their `continue-on-error` flags from `ci.yml` and fails if the README stops
  matching, rather than trusting the next reader to notice.

## [0.15.4] — 2026-08-18

### Fixed
- **`changed` reported files that are not source, including this tool's own artifact.** On one
  repository it answered *"4 files → 28 symbols"* where the four files were `.gitignore`, two plan
  JSONs and `CODE_INTEL.md` — and every one of the 28 "impacted symbols" was a markdown **heading**
  out of that file, which codeintel itself had written into the repo. On another it counted
  `.DS_Store`. A change-impact answer is about code; noise of this shape reads as signal, and an
  answer that opens by citing four files nobody edited spends the reader's trust before it gets to
  the real ones.

  The indexer's corpus policy could not be reused as the filter, and that is exactly how the
  artifact leaked through: the corpus admits `.md` **deliberately**, because semantic search over
  documentation is worth having. So `source_kind` now owns the narrower question — `CODE_EXTS` plus
  `is_code_path` (a known code extension, not prose, not generated) — and the indexer derives its
  corpus from that set rather than keeping a second hand-typed copy of it. Two hand-typed copies of
  one population is how these drift apart with nothing to say so.

### Changed
- **`changed` now separates "no source changes" from "working tree clean."** Filtering can empty the
  list on a tree that genuinely has uncommitted edits. Calling that clean would trade a noisy answer
  for a false one, so it reports which of the two it is, and how many non-source changes it set
  aside.

## [0.15.3] — 2026-08-17

### Security
- **The LSP engine served serena's error text to agents as an answer, instructions included.**
  `_extract_text` harvested `.text` from every MCP content block and ignored the result's
  `isError` flag, so a failed tool call came back as `ok: true`, **no `reason`**, and a `result`
  body containing the backend's error message. Three problems in one string: a failure presented
  as data; a dump of the LSP initialisation parameters; and — the reason this is filed here —
  imperative text addressed to a language model, verbatim:
  > `do not attempt workarounds. Inform the user and wait for further instructions before you continue!`

  A backend's error path had a direct channel into what the calling agent reads as its answer. The
  provider now detects an error result (by `isError`, and by response shape for servers that do not
  set it), returns a safe-null with `reason: backend-error`, and hands back a **fixed, boring
  summary** — the backend's own prose is logged for the operator and forwarded nowhere a model can
  read it. Detection is anchored to the start of the payload and skips JSON, so a `symbol` lookup
  quoting real source containing "Exception:" is not mistaken for a failure.

### Fixed
- **A failed language-server call reported the symbol as not found.** When a tool call returned
  nothing, `symbol` rendered `(not found)` — asserting the symbol does not exist, on no evidence.
  For an agent deciding whether to create something, "I could not ask" and "it is not there" are
  opposite answers. It now degrades to a safe-null with a reason.
- **A backend failure is no longer reported as `unsupported-op`**, which sent the agent looking for
  a different tool when the language server simply had not started.

### Added
- **A corpus harness, run nightly against real repositories.** This automates the one technique
  that has actually found this project's bugs. Every serious defect so far — a vendored tree
  ranked as a hotspot, answers served from a containing repo, a dead graph engine, a language
  server error returned as an answer — was found by pointing the tool at an unfamiliar codebase,
  and none by the unit suite, whose fixtures are all synthetic two-to-five-file micro-repos.
  Repositories are pinned by commit SHA, adversarial artifacts are planted around them (a secret
  outside the root reachable by symlink; a minified bundle in a directory on no skip list), and the
  checks assert **invariants that hold on any codebase** rather than golden output that would churn
  and get rubber-stamped: nothing raises; no result carries content from outside the root; no
  result cites a file that is generated by shape or that `git check-ignore` claims; `deadcode`
  hits have no textual reference in the tree; a nonsense target reports absence rather than
  failure; no absolute host path or backend project id leaks; and the same query gives the same
  answer in two fresh processes.
  > Two of the checks exist only to stop the rest from lying. **Every invariant is of the form "X
  > never appears in a result", and all of them pass trivially against an engine that returns
  > nothing** — which is the exact state this project shipped in for a whole release. Explicit
  > non-vacuity guards assert the engines produced substantial output first, so a green corpus run
  > can never again certify an outage. The harness was verified to go red by disabling the
  > generated-content detection and confirming the planted bundle enters the corpus.
- **A `lsp-contract` CI job** running the live serena tests, which had never executed anywhere —
  the same blind spot as the graph backend, one engine over, and it hid the bug above. Marked
  `continue-on-error` because serena is fetched from an upstream git HEAD this project does not
  control: a breakage there must be visible without blocking an unrelated release.

## [0.15.2] — 2026-08-17

A production-readiness review — four independent audit passes over correctness, security,
first-run/lifecycle, and the test suite itself. The findings were not eight unrelated bugs but two
structural properties producing bugs of a fixed shape: **the health layer is computed on a separate
code path from the answer layer, so `doctor` could report a repo healthy while `code.query`
answered from the wrong index**; and **cross-cutting properties are retrofitted per-site**, so each
one lands at some call sites and misses others (the commit titled *"a third and fourth renderer
were still leaking the home path"* is that property announcing itself).

### Fixed — the graph engine was entirely non-functional, silently
These three were found the first time the real backend was ever installed alongside the test
suite. Each one disabled the graph engine completely while the tool reported itself healthy.

- **Project resolution used a 3000 ms budget against a backend that takes ~5.8 s.** `list_projects`
  spawns a native binary that re-initialises its own allocator on every invocation — measured at
  ~5.8s *consistently*, not merely on a cold start. Every graph query resolves a project first, so
  every graph query timed out, and the caller reported `project-not-indexed` with the advice to run
  `codeintel index` — on a repository that was already fully indexed. The budget is now 20 s
  (overridable via `CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS`), a successful lookup is cached, and a
  timeout is no longer cached as a miss so one slow moment is not remembered as "no index".
- **A timeout is no longer reported as "not indexed".** Those are different facts with different
  remedies, and collapsing them produced the most useless possible instruction: re-index a repo
  that is indexed. Resolution now distinguishes `backend-unreachable` from `project-not-indexed`.
- **`codebase-memory-mcp` 0.10.x is incompatible, and this is now detected instead of silent.**
  0.9.x answers `query_graph`/`search_graph` with `{"columns": [...], "rows": [...]}`, which every
  renderer here parses. 0.10.x replaced that with a compact text format — while keeping
  `list_projects` as JSON. The combination is the worst available: resolution and `doctor` kept
  succeeding, so the engine looked healthy, while `callers`, `callees`, `impact`, `chain`,
  `pattern`, `overview`, `changed`, `deadcode` and `hotspots` all returned nothing and the tool
  said `not-in-graph` — a false claim about the user's index. The provider now tells "the backend
  spoke a dialect I cannot read" apart from "the process failed", reports `backend-incompatible`
  with the pin, and `doctor` probes with a real query rather than trusting `list_projects`.
  **`0.9.x` is the supported range**; the backend self-updates, so `codebase-memory-mcp update` can
  break the engine.

> **None of this was reachable by the existing suite**, because no CI job installs the backend and
> the provider's unit tests assert what codeintel *intends* to send. The one live test that would
> have failed skipped instead — with "project not indexed in this environment", a condition the
> resolution-timeout bug itself produced. A new `graph-contract` CI job installs the pinned backend,
> runs the live tests, and **fails if they skip**, because a skipped contract test is what let this
> ship.

### Security
- **A file swapped for a symlink AFTER indexing could be read back from outside the root.**
  Containment was enforced in the indexing *walk* and nowhere else: `Searcher._read_snippet` and
  `_read_chunk` re-opened the stored path at query time with no check, and the stale row was never
  reconciled because `_cleanup_deleted` asked `.exists()`, which follows a symlink to an existing
  target and answers `True`. An actor able to write inside their own allowed root could commit a
  normal file, wait for indexing, then point it at anything the server process can read — another
  role's repository, or `~/.codeintel/auth.toml`, which holds the tokens — and have ~40 lines per
  planted chunk returned as result snippets. A standing hole, not a race. Containment now lives in
  one module (`codeintel.containment`) that the indexer, the searcher and the cleanup pass all
  call, so it is asserted on the data path rather than at one point in it. Every prior containment
  test planted its link *before* indexing, which is why this shipped; the new tests plant it after.
- **A denied project root now returns HTTP 403**, like a denied op. Data was already withheld, so
  this was never a leak — but the docs promised 403, and a role probing for roots it does not own
  produced a wall of 200s that no 4xx-based alerting could see.
- **`docs/deploy.md` no longer presents multi-tenant RBAC without qualification.** The two-team
  example config read as an isolation boundary; RBAC is sound for separating privilege levels among
  callers you already trust, and that is now what it says.

### Fixed
- **`code.query` answered from the wrong repository while `doctor` warned about it.** 0.15.1 fixed
  only the diagnostic: `probe()` detected that resolution had fallen back to a containing project,
  but the query path resolved through a different code path that discarded that fact, so a human
  running `doctor` was told and the agent consuming the answers was not. Both now share one
  `ProjectResolution` record, and the handling is per-op rather than uniform:
  - **Repo-scan ops (`overview`, `changed`, `deadcode`, `hotspots`) refuse**, with a distinct
    `project-not-indexed-standalone` reason. These answers are *defined by* the repo boundary —
    the monorepo's dead code is not a lower-confidence answer to this repo's dead code, and a
    symbol dead in one repo is routinely live in its sibling, which is how `deadcode` came to tell
    an agent to delete working code.
  - **Symbol-scoped ops (`callers`, `callees`, `impact`, `chain`) still answer**, because for a
    genuine subdirectory of a monorepo the containing index is exactly where a symbol's callers
    live — but the result text now discloses the scope.
  - **`map` and `graph` refuse outright.** Both write artifacts that get committed or shared, and
    a committed `CODE_INTEL.md` describing a sibling repository is the worst version of this bug.
- **`doctor` printed no note for an engine it had just flagged.** The renderer showed `detail` only
  when the status was non-ok, and the ancestor-repo case is "ok" with three green cells — so the
  one command a user runs when confused stayed silent about the most likely cause of the confusion.
- **"No engine could be asked" is no longer reported as "nothing found".** `_merge` collapsed every
  engine's reason into a flat `no-result`, the string agents are told to read as "does not exist /
  not indexed yet". A fan-out with both backends missing therefore produced a confident denial.
  Fan-out results now carry the per-engine causes and use `engines-unavailable` when nothing could
  be reached. `context` fans out by default, so this was the common path.
- **A cached answer served during a reindex no longer hides that it may be behind.** The staleness
  marker was applied on one of four return paths — and the cache key's freshness generation only
  advances when a reindex *completes*, so the paths that could actually serve a stale answer were
  the three that stayed silent. It is now applied on every exit.
- **`codeintel status` reported another repository's index age as this one's.** "Index age" was the
  mtime of the shared per-model database file, which every project writes to — so indexing any
  other repo made a months-stale index look freshly built, and the number was most convincing
  exactly when someone was checking it because an answer looked wrong. A per-project `indexed_at`
  is now recorded and reported; a project with no recorded pass says "unknown" rather than
  inventing a number, because an authoritative-looking wrong number is worse than none.
- **A deleted or moved repository stayed "indexed" forever.** The index pass returned `0` when the
  root was missing — and `0` also means "nothing new" — so it never reached cleanup and `doctor`
  went on reporting the repo as healthy and indexed. A missing root is now the moment its rows are
  reconciled away, scoped to that project so no other repo's index is touched.
- **A cleanup pass could silently do nothing.** `code_embeddings` is created lazily at the first
  embed, so on a database that has recorded chunks but never embedded, the first `DELETE` against
  it raised, the surrounding never-raise handler swallowed it, and every stale row survived —
  including a row for a file swapped for a symlink, which is exactly what must not persist. Found
  by a test written for the deleted-repo fix above; the same flaw was present in two sibling
  cleanup paths and is fixed in all three.
- **The `not-in-graph` hint no longer names the backend's project id.** For a path-slug
  registration that id *is* the flattened absolute path of the repository, so the hint disclosed
  the server's directory layout to any caller. The earlier home-path sweep grepped for
  `qualified_name` and never covered this channel.

### Added
- **`CODEINTEL_HOME` overrides the cache location.** `Path.home()` raises when a process has no
  resolvable home directory — a container running as a UID with no passwd entry and no `$HOME`,
  which is routine for the coding agents this server exists to be launched by. There was no way to
  proceed except changing the environment, and the failure surfaced as a `RuntimeError` several
  layers from anything that named it.
- **`doctor` now names an unresolvable cache directory instead of calling it "not indexed yet".**
  That report sent the user to `codeintel index`, which fails identically for the same reason — two
  commands, neither naming the problem.
- **`setup --all` names the reason indexing failed.** Its step table said only "indexer reported an
  unrecoverable failure" while the real cause — a blocked model download, an unwritable cache
  directory — sat in an unlinked stderr line above it. `Indexer.index()` still returns `-1`, but it
  now also keeps the reason on `last_error` for callers that must show one rather than log one.
- **A CI check that a documented version was actually released.** Releases ship on a `vX.Y.Z` tag
  and nothing else, so bumping the version and writing the CHANGELOG without pushing the tag fails
  silently — green CI, and the release never reaches users. It had already happened three times:
  0.13.2, 0.14.1 and 0.15.0 were each bumped, documented and committed without a tag, and their
  fixes sat unshipped behind a newer version number. `scripts/check_release_consistency.py` fails
  when a CHANGELOG version has no matching tag, when the newest entry disagrees with
  `__version__`, and when tags were not fetched; it exempts the in-flight version and records the
  three known-superseded ones. It runs as its own CI job so the failure reads as one red line.
- **A release checklist in CONTRIBUTING.md**, including the step that was being skipped: confirm
  the version actually appears on PyPI.
- **A `Project status` section in the README** stating plainly what is solid, what is young, what
  the tool is designed for, and what to be careful with — plus PyPI, Python-version and license
  badges, and a status note at the top.

### Changed
- **"Not hand-written source" is now a shape, not a list of directory names.** Every entry in the
  four separate skip-lists this replaces was added *after* a real repository produced a wrong
  answer, and the tests were parametrized over those same lists — proving only that the list
  contains what it contains. A tree named `bazel-bin/`, `_generated/`, `.output/` or `Pods/`
  reproduced the minified-bundle-tops-the-hotspots bug exactly as `out/` and `dist/` did before
  0.15.1. `codeintel.source_kind` answers the question three ways: path patterns (including
  prefixes like `bazel-*`, whose full name is unknowable in advance, and generated FILENAMES such
  as `*.min.js`, `*_pb2.py`, `*.g.dart` that sit beside real source where no directory rule can
  reach them); content shape (a generator banner near the top, or line geometry no human writes —
  which is the only signal that catches a bundle whose directory and filename both look ordinary);
  and `.gitattributes` `linguist-generated`/`linguist-vendored`, the repository's own declaration
  and the one authoritative signal available, which nothing consulted before.
  > The new checks are deliberately **additive and conservative**. Ambiguous names stay with the
  > callers that already judge them well: the indexer treats `out`, `build`, `vendor`, `generated`
  > and `coverage` as output only at the repo root, because one level down they are somebody's
  > package — matching them at any depth previously indexed 0 files of pypa/build and hid
  > coverage.py's own collector. `packages/` is source in every monorepo, and `output/` is not
  > `out/`. Thresholds err toward under-hiding, and a test asserts an ordinary repository comes
  > through completely intact.
- **`map`'s ranked-symbol table now applies the same noise filter as `hotspots`.** It had three
  skip paths and nothing else — no archived, generated or test filtering — despite being the
  sibling of the op that was specifically hardened after a minified bundle took its top two slots.
  It is also the ranking that gets written to `CODE_INTEL.md` and committed, so a webpack chunk
  listed as the repo's most load-bearing symbol became a wrong answer that survived in the tree.
- **The PyPI classifier is now `4 - Beta`, not `5 - Production/Stable`.** The package is
  well-tested and its one-call surface has been stable since 0.8, but the project is days old,
  pre-1.0, and still finding real defects on first contact with unfamiliar repositories — as
  0.15.0 and 0.15.1 record. `Production/Stable` was a claim it could not yet back.
- **`deadcode` now carries an explicit caveat in the README and `docs/graph.md`**, since it is the
  one op whose output invites a destructive action. The source verification removes the common
  false positives but cannot make the answer complete: entry points in packaging metadata, plugin
  discovery, reflection, and callers in unparsed languages are all invisible to it. Documented as
  a candidate list, never a work order, and not to be wired into an agent that deletes unattended.

## [0.15.1] — 2026-08-16

Found by pointing the tool at two more unfamiliar repositories.

### Changed
- **The lexical half of the hybrid ranking is now IDF-weighted.** Reciprocal Rank Fusion already
  combined a semantic rank with a lexical one, but the lexical score was flat token coverage — in
  "the auth middleware", the word "the" carried a third of the score while appearing in nearly
  every chunk of the corpus. Document frequency is measured across the retrieved candidates, so
  there is no schema change and no extra reads: the texts are already in hand for the snippet.
  On the motivating case the gap between a real match and a stopword-only match widens from 3.0x
  to 4.3x.

> **Upgrading:** the corpus fixes below only affect what is written at index time, so an
> already-indexed repository keeps its archived and generated chunks until you re-run
> `codeintel index <repo>`.

### Fixed
- **Answers could come from a different repository, reported as ready.** Project resolution falls
  back to the nearest indexed *ancestor* — correct for a subdirectory of an indexed repo, wrong
  for a repo that merely sits inside one. Asking about `~/projects/my-app` when only `~/projects`
  was indexed said "3/3 engines ready" and then answered from a graph spanning every repo on the
  machine. `doctor` now states plainly that the repo is not indexed on its own, names the project
  the answers would come from, and gives the command to index it properly.
  > **Scope:** this release fixed the *diagnostic* only. `probe()` detects the ancestor fallback,
  > but the query path still resolved to the containing project without saying so, so an agent
  > calling `code.query` — rather than a human running `doctor` — still got answers from the wrong
  > repository. The answer path is fixed in 0.15.2, above. 0.15.1 was never published to PyPI.
- **Generated output ranked as the top thing to refactor.** A checked-in minified bundle took the
  first *two* hotspot slots on a real repo (cx:586, cog:1145) — a webpack chunk is by far the most
  "complex" function in any tree containing one. Only dot-directories were excluded, so a plain
  `out/`, `dist/` or `vendor/` sailed through. Graph scans and the source verifier now share one
  definition of "not hand-written source".
- **Semantic search ranked archived prose above live code.** The indexer excluded neither retired
  trees nor build output, so a search for "websocket reconnect logic" returned an archived
  markdown file's blank line first and the actual implementation second.
- **Chunks made only of punctuation were embedded**, and such a vector matches everything weakly,
  so it surfaces for anything: a `---` front-matter fence was the top hit for a real query. A
  chunk now needs at least one letter. The test is deliberately the weakest possible one —
  anything stricter starts rejecting real one-line code.
- **Result previews showed the chunk's first line whatever it was**, rendering hits as
  `path:line | ---`, which the reader cannot judge without opening the file. The preview now picks
  the first line that says something.

## [0.15.0] — 2026-08-16

Everything here came from pointing 0.14.2 at two repositories it had never seen. Three adversarial
review rounds had not found any of it.

### Added
- **`codeintel query --json` emits the full result envelope.** 0.14.2's new "Reporting a problem"
  section asked users to include `engine`, `cached` and `reindexing` when a result looks wrong —
  fields the CLI had no way to display, printing only the rendered result or the reason. The
  documentation described a workflow the tool did not support. It does now, and those are exactly
  the fields that explain *why* an answer looks off.

### Fixed
- **`deadcode` was systematically wrong on callback-heavy code, and confident about it.** The op
  asks the graph for functions with **in-degree 0**, and a function passed as a *reference* rather
  than called has in-degree 0 — every React event handler, every
  `addEventListener('keydown', onKeyDown)`, every framework callback. On a real TypeScript repo it
  returned 181 candidates and every one sampled was live code; an agent acting on that answer
  would delete working code. Candidates are now verified against the source with a bounded
  word-boundary scan, and a name appearing anywhere beyond its own definition drops out. The same
  repo now returns **4**, three of them confirmed genuinely dead and the fourth a framework-called
  object-literal property — precision from roughly zero to roughly three-quarters, with the
  residue named in the output rather than implied away. The result states which verification
  actually ran: a full source check, a missing `project_root`, or a repo past the scan cap.

  The verification is a name-frequency heuristic, and the notes say so. It deliberately errs
  toward hiding real dead code rather than reporting live code as dead: an agent that deletes a
  working function has done damage a shorter list never could.
- **Repo scans ranked archived code as the thing most worth refactoring.** Nothing excluded
  dot-directories, so an 8MB `.archive/` tree put a retired 507-line component *third* in a
  repo's hotspots — a near-duplicate of the live one. `.github` is kept, since its workflows are
  live.
- **`hotspots`/`deadcode` kept the backend's project id on every row.** 0.14.1 stripped it in
  `_display` (callers/callees) and missed `_render_scan`, so the longest results in the tool —
  200 rows — carried the author's full home path on each line.
- **Labels repeated themselves**: `MarkdownEditor.EditorHeader.EditorHeader.EditorHeader`, with
  the file path printed right beside it. Consecutive duplicate segments are collapsed.

## [0.14.2] — 2026-08-16

Clears the remaining known limitations from 0.14.0's list.

### Fixed
- **Binary files with a source extension are no longer indexed.** A compiled artifact named `.py`
  was read with `errors="replace"` and embedded as replacement-character garbage — 196KB of random
  bytes produced 162 chunks — which then competed for rank against real code in every search.
  Files are now sniffed for a NUL byte in the opening block, the same rule `git` uses.
- **`setup`, `status`, `map` and `graph` now reject a project root that does not exist.** They
  produced confident, well-formed output about a directory that isn't there — `setup /typo`
  rendered a full three-engine health table for it — which in a script is indistinguishable from
  success. All four now exit 1 with a clear message, matching `index`.
- **The vector-dimension warning named a command that cannot fix it.** It advised `codeintel
  reset`, but the vec0 table's dimension is fixed at creation and the table is *shared* across
  every project in a cache file, so a project-scoped reset only deletes that project's rows and
  the warning repeats forever. It now names `codeintel reset --all`, which does resolve it.
- **Overlapping background reindexes.** The debounce timestamp was set when a pass was
  *submitted*, not when it finished, so on a repo whose reindex outlasts the window every later
  query stacked another concurrent pass — overlapping writers against one SQLite file and one
  graph subprocess, for no benefit. A root already being reindexed is now skipped.

### Docs
- README gains a **Reporting a problem** section pointing at `codeintel doctor --json`, so a bug
  report arrives actionable rather than needing a round trip.

## [0.14.1] — 2026-08-16

Quality-of-life for the thing this tool actually serves: an agent reading its output.

### Added
- **An answer served while a reindex is running now says so** (`reindexing: true`, with a hint to
  re-ask). Structural answers hash a symbol *name*, not file bytes, so nothing else in the
  envelope could reveal that the index was behind — and an agent's loop is *edit → ask what I
  broke*, which lands exactly in that window. Note what this deliberately is NOT: invalidating the
  cache faster would not help, because the **index** is what is stale, so re-asking would refetch
  the same data more expensively. Saying so is the honest fix. Flagged only when there is an
  answer to qualify, so a safe-null still explains itself through `reason`/`hint`.

### Changed
- **Result lines no longer carry the backend's project id.** Every row began
  `Users-alice-Documents-project-myrepo.src.pkg.fn` — the author's home directory repeated on
  every line of results that can run to a hundred rows. Noise for a human, wasted tokens for the
  agent. Only a leading path-slug segment is stripped; a hyphen cannot appear in a Python package
  name, so a genuine dotted module path passes through untouched.

### Fixed
- **`codeintel install --agent zed` failed for every real Zed user.** Zed ships `settings.json` as
  JSONC, and the installer parsed strict JSON, so it stopped at an opaque `Expecting value`.
  Parsing the JSONC and writing it back would be *worse* — `json.dumps` would silently delete the
  user's comments, which in Zed's default config is most of the file. It now detects JSONC
  specifically, leaves the file untouched, and prints the exact block to paste and where to put
  it. A genuinely corrupt file still reports its parse error rather than being misdiagnosed.

## [0.14.0] — 2026-08-16

Found by adversarial review, then reproduced against a live server before fixing and again after.
**If you run the HTTP transport with RBAC, this is a required upgrade and it needs a config
change** — see Breaking below.

### Security
- **RBAC scoped operations but never targets, so any token could read any readable directory.**
  `auth.toml` mapped token → role → allowed *ops*. `project_root` arrives in the request body and
  reached the providers unchecked, so the least-privileged role in this project's own documentation
  (`searcher = ["search", "context"]`) could name an arbitrary path; the semantic engine would then
  walk it, index it, and return its contents as search snippets. Confirmed by exploit against a
  running server: a `searcher` token read a file from a directory it was never granted.
  Roles now carry a `[roots]` allowlist, enforced before any work — before the reindex, so a
  rejected path is never even walked. Comparison resolves symlinks and `..` on both sides, so
  neither escapes an allowed root, and `/srv/repo-secrets` is not inside `/srv/repo`.
- **A config whose roles were all `["*"]` disabled the policy entirely**, and a disabled policy
  enforced no root scoping either. `build_policy` set `enabled=bool(rules)`; it is now True
  whenever RBAC is configured. Op behavior is unchanged.

### Fixed
- **A semantic-engine failure permanently froze cache invalidation.** Both reindex passes shared
  one `try`, semantic first, so a blocked model download, full disk, or corrupt vector DB skipped
  the graph pass *and* the freshness-generation bump. That bump is the only invalidation for
  non-file targets — `callers`, `impact`, `chain`, `hotspots`, whose content hash is of a symbol
  name and never changes — so the counter stayed pinned at 0 for the process lifetime and cached
  answers were served `ok: true, cached: true` indefinitely. The passes are now independent and
  the generation advances in a `finally`.
- **Content-hash invalidation never engaged for relative targets.** `_compute_hash` resolved the
  target against the process's working directory rather than `project_root`, and the ops that take
  a path take it repo-relative. For a long-lived server answering for arbitrary roots the file was
  effectively never found, so entries fell back to hashing the *string* — which cannot change when
  the file does. This contradicted the guarantee in docs/architecture.md that "an edit changes the
  content hash and forces a refresh".

### Security (second adversarial pass — the first fix was incomplete)
- **A symlink planted inside an allowed root defeated project scoping entirely.** `os.walk`
  defaults to `followlinks=False`, which stops recursion into symlinked *directories* but leaves
  symlinked *files* in the listing — and the later `open()` follows them. Any tenant able to write
  inside their own root could link to any file the server process could read and have it indexed,
  embedded, and returned as a snippet. Reproduced, then fixed: `Indexer._walk_files` now resolves
  every candidate and requires it to land back under the resolved root.
- **`GET /code/status` had no authorization at all** — authenticated, but never checked op or
  root — so any valid token could ask "is *that* directory indexed?" about any path. It now
  carries the server-authoritative role through and applies the same root scoping as `query`.
- **`POST /code/doctor` was op-gated but not root-gated**, so a role scoped to one project could
  run doctor against another, and `deep: true` would boot a live LSP session rooted at the
  attacker's path. Now root-checked after the op check.
- **A blank `project_root` resolved to the server's working directory** rather than being denied.
  `os.path.realpath("")` returns the cwd, so the `if not target` guard never fired and containment
  was evaluated against wherever the server was launched. Rejected before resolution now.

### Fixed (second adversarial pass)
- **An `overview` auto-fallback answer could be served to an explicit `engine=graph` request.**
  `auto` and an explicit `graph` both resolved to the string `"graph"` and shared one cache key,
  but they are different questions: `auto` accepts the LSP fallback, explicit `graph` does not. A
  single `auto` miss parked an LSP answer under the graph key, and the next explicit request got
  it back with `cached: true` and an `engine: "lsp"` field contradicting its own request —
  reachable on any cold start, since "graph not indexed yet" is the normal first-query state.
  Cache keys now distinguish what was asked from what auto resolved to.

### Security (third adversarial pass)
- **A hardlink defeated the symlink containment guard.** A hardlink is a second directory entry
  for the *same inode*: it sits physically inside the root, so its `realpath` is inside the root
  and the guard passed it — `realpath` cannot see a hardlink at all. A tenant able to write in
  their own allowed root could `ln` another tenant's file in and have it indexed and returned.
  Reproduced, then fixed: a candidate with more than one link is skipped with a logged reason.
  Measured at 0 occurrences across 3213 source files of a real repository, so ordinary indexing
  is unaffected.
- **A blank entry inside a `[roots]` list silently granted the server's working directory.**
  `_normalize_root("")` returns the cwd rather than an empty string, so a stray `""` or a
  trailing-comma artifact survived the filter — the one fail-OPEN case in an otherwise
  fail-closed model. Blank entries are now dropped before normalization.

### Fixed (third pass — whole-surface audit)
- **`codeintel reset` could not repair a corrupt cache and reported success anyway.** sqlite
  refuses to open a corrupt file, so there were no rows to DELETE and the aggregate synthesized
  "removed 0 indexed chunk(s)" while discarding the per-file error. `doctor` diagnoses the
  corruption and prescribes `reset`, so the user was left in a loop that could never terminate.
  An unreadable cache is now removed (it is a rebuildable cache, not user data), the reason is
  reported, and per-file failures reach the summary instead of being summed away.
- **`codeintel map --inject` could duplicate your own instructions without bound.** A `CLAUDE.md`
  holding an END marker without its START — a hand-edit, a bad merge, or this function's own
  append branch — made every run re-emit the text between the stray marker and the block: one
  extra copy per invocation. `CLAUDE.md` is prompt context, so this silently degraded the agent
  it exists to help. The end marker is now searched for *after* the start.
- **`--inject` widened file permissions, rewrote line endings, and replaced symlinks.** The
  atomic-write pattern installs a new inode, which dropped the original's mode, converted a CRLF
  file to LF (a whole-file diff on every run), and replaced a symlinked rules file with a regular
  one. Mode is now carried across, line endings preserved, and the link target written through.
- **`codeintel install` widened `~/.claude.json` from 0600 to 0644 and orphaned symlinked
  configs.** That file holds OAuth tokens and per-server `env` secrets. Same root cause and same
  fix as above; a newly created config is written 0600 rather than inheriting the umask.
- **`install` destroyed a `~/.claude.json` that wasn't a JSON object.** A top-level array or
  string was silently replaced with `{}` and overwritten, exit 0, reported "registered" — the one
  branch that lost data where every sibling fails safe. It now refuses and says why.
- **~200x memory amplification on a single long line.** Chunk splitting is line-based, so a
  minified bundle or generated one-liner became one unsplittable chunk: a 20MB single-line file
  peaked at **3.4 GB** RSS through the embedder, on the reindexer's daemon thread inside the
  long-lived MCP server. Chunk text is now capped by characters as well as lines — the same file
  now peaks at 455 MB.
- **Every runtime failure in `graph` and `map` exited 0**, so a `make` or CI step gating on `$?`
  saw success while no file was written. Both now exit 1 on failure. `query`/`status` keep exit 0,
  where an empty result is the never-raise contract rather than an error.
- **`codeintel index` reported "Nothing new to index" on unrecoverable failure** (`-1` fell into
  the `else` of a `> 0` test), and accepted a project root that does not exist — a mistyped path
  in a script was indistinguishable from a clean incremental run. Both now fail loudly and exit 1.

### Known limitations (not fixed here, deliberately)
- **Non-file targets can be stale for up to the reindex debounce (~30s).** For symbol-name and
  free-text targets the content hash is constant, so the freshness generation is the only
  invalidation, and it only advances when a debounced background reindex completes. An edit
  followed immediately by the same query can return the pre-edit answer. This is inherent to the
  current design rather than a regression; the README's freshness section now says so plainly.
- **A `codeintel index` run in another terminal does not immediately invalidate a running
  server's cache.** The CLI builds a throwaway `Reindexer`, so its generation bump is invisible to
  the long-lived server, which self-heals on its own next reindex.
- **Zed registration fails on a real Zed `settings.json`,** which is JSONC (comments + trailing
  commas) while the installer parses strict JSON. It fails safe — the file is left untouched — but
  the error is opaque and Zed users cannot register.
- **Other subcommands still accept a project root that does not exist** (`setup`, `status`, `map`,
  `graph` will render confident output for a directory that isn't there). Only `index` validates.
- **Binary files with a source extension are indexed** as replacement-character garbage, competing
  in search ranking. There is no content sniff before chunking.
- **A vector-dimension mismatch is a dead end via the advised command:** scoped `reset` deletes
  rows but never drops the fixed-dimension table, so the advice to run it does not resolve the
  warning. `reset --all` does.
- **Background reindexes have no in-flight guard.** The debounce timestamp is set when a reindex
  is *submitted*, not when it completes, so on a repo whose reindex outlasts the debounce window
  overlapping passes can stack.

### Breaking
- **RBAC deployments must add a `[roots]` table.** A role with no entry may now target nothing.
  This fails closed on purpose: a root allowlist defaulting to "everywhere" would be the
  vulnerability above with extra configuration. `load_auth` logs the exact line to add at startup
  rather than leaving you to discover it as 403s. Deployments without an `auth.toml` — every local
  stdio and CLI user — are unaffected.

## [0.13.2] — 2026-08-16

**0.13.1's reindex fix was itself wrong.** Upgrade over it.

### Fixed
- **The background graph reindex passed `project_root` where the backend wants `repo_path`**, so
  it still refreshed nothing. 0.13.1 correctly swapped `detect_changes` (which only reports
  uncommitted drift) for `index_repository`, but kept the argument name from the old call. The
  backend answers a wrong parameter name with `Indexing worker crashed on a file` — which reads
  like a parser bug in some source file and points away from the cause; only the worker log says
  `repo_path is required`.
- The unit tests could not catch either mistake, because they assert what codeintel *intends* to
  send, which is worthless when the intent is wrong. Added a live test that runs a real reindex
  against the real backend and asserts the repo actually got registered — verified to fail on the
  0.13.1 payload and pass on this one. It skips when the backend is absent, as the other live
  graph tests do.

## [0.13.1] — 2026-08-16

A patch release for one class of bug, found by pointing codeintel at its own source and not
believing the answer: **the graph engine reported code as it had been hours earlier, with nothing
to indicate the reading was old.** Three separate defects had to line up for that, and each on its
own was survivable — together they made staleness invisible. If you use `callers`, `impact`,
`hotspots` or `map`, upgrade; those are the answers that were wrong.

### Fixed
- **The graph engine could answer from a stale index while a complete one sat beside it.** The
  backend can hold more than one project for the same root path — typically one under a short name
  and one under a path slug — and they drift apart independently. Resolution took the *first*
  exact `root_path` match, so on this repo it bound every query to a 1475-node snapshot while a
  2631-node index for the identical path went unused. `callers`, `hotspots` and `map` therefore
  described code as it had been hours earlier, with no indication anything was wrong. Among exact
  matches codeintel now prefers the most complete index, falling back to the original
  first-listed rule when there is no completeness signal to go on.
- **The background graph reindex never reindexed anything.** It called `detect_changes` with a
  `project_root` argument, and was wrong twice over: the backend takes `project` (a name from
  `list_projects`), so every call returned an argument error that got folded into `None` — and
  `detect_changes` only *reports* uncommitted drift, it never rebuilds the index. It now calls
  `index_repository`, and logs a backend-reported failure instead of swallowing it, which is what
  let a broken reindex look exactly like a working one.
- **`reason: "unsupported-op"` for a supported op that simply found nothing.** `callers` on a
  symbol missing from the index — overwhelmingly a stale index — reported the operation itself as
  unsupported, and was the only failure path in the graph provider carrying no `hint`. That sends
  an agent looking for a different tool when the fix is `codeintel index`. Now
  `reason: "not-in-graph"` with the exact command.
- **`CODE_INTEL.md` embedded the author's home directory.** The architecture heading used the
  backend's internal project id, which is often a flattened absolute path
  (`Users-alice-Documents-project-myrepo`) — published into a file designed to be committed and
  pushed. It now uses the repository's own directory name.

### Docs
- **README: a "Keeping answers fresh" section**, because nothing told you the graph is a snapshot
  while LSP is live and semantic is incremental — the gap that let all of the above go unnoticed.
  The "never stale" claim beside the cache was overstated and now says what it actually means: the
  *cache* never disagrees with the index; how current the index is depends on the engine.
- CLI reference: added the `reset --json` and `query --project-root` flags it was missing.

## [0.13.0] — 2026-08-16

This release is about **enforcement**: the project's quality claims now have something checking
them on every push, rather than resting on care and review. Two things change for you — `codeintel
install` no longer tracebacks, and the wheel finally carries the marker that lets a type checker
see the annotations it has always shipped. Every command's behavior is otherwise identical.

### Fixed
- **Host-config handshake tests now launch the build under test.** They read a command out of an
  agent's own config and run it, but resolved it through `shutil.which`, which on a developer
  machine finds a previously-installed global `codeintel` ahead of the editable one — so they
  registered, launched, and handshook with a different build and passed. A `console_script`
  fixture pins PATH to the running interpreter's scripts directory and skips (rather than passes)
  when this checkout has no console script installed at all.
- **The wheel now ships `py.typed`.** The package has advertised the `Typing :: Typed` classifier
  since it was first published, but without the PEP 561 marker a type checker ignores an installed
  package's annotations entirely — so every downstream user got `Any` from a fully annotated
  library. Verified against the built wheel, not just the source tree.

### Changed
- **Classified `Development Status :: 5 - Production/Stable`** (was `4 - Beta`). The label now
  rests on something checkable rather than on confidence: every push runs ruff, mypy, and the
  suite under a coverage floor across Python 3.11–3.13, and the build job installs the wheel into
  a clean environment and drives a real `code.query` through a registered MCP host before the
  artifact is kept. What earned the change was watching that gate catch a live regression in the
  primary tool rather than letting it ship.
- **`codeintel install` degrades with a message instead of a traceback.** It was the last
  subcommand where an unexpected failure — an unresolvable home directory, an unreadable agent
  config — reached the user as a stack trace rather than a line explaining what broke. It now
  reports `install failed: <reason>` and exits 1, matching every other command.

### Internal
- **The CLI moved out of one 470-line `main()` into `codeintel/commands/`, one module per
  subcommand.** Each is `run(args) -> int` — it returns its exit code instead of calling
  `sys.exit`, so the bodies are directly callable from a test. That closed the project's largest
  coverage gap: install's verification/legacy/skipped reporting, reset's confirmation guard, and
  query's LSP warming poll were previously reachable only by driving argv through a subprocess.
  Dispatch imports the command module lazily, so `codeintel serve` still does not pay for the
  semantic engine's imports — an invariant now enforced by a test.
- **ruff, mypy, and a coverage floor gate CI.** The toolchain had no linter and no type checker at
  all. Both now run as their own fast CI job, and the suite fails under 83% coverage. The rulesets
  are configured to respect the never-raise architecture rather than fight it: `S110`, `S112`, and
  `SIM105` are off, because the ~190 blanket-except and best-effort-`pass` blocks they flag are the
  design, and each carries its reason as a comment that `contextlib.suppress` has nowhere to keep.
  mypy is deliberately not `--strict` for the same reason — the provider seams cross into untyped
  backends.
- **Type and lint fixes surfaced by adopting them**, including two stale `type: ignore` codes,
  four unguarded `None` dereferences (`Popen.stdin`, two `dict.items()` on possibly-absent auth
  config sections, one `body_location`), and two paths that would have passed `None` into a
  `subprocess` argv when the graph backend was off PATH.

## [0.12.1] — 2026-08-16

### Added
- **`codeintel help` — every command, grouped by what you're trying to do, with descriptions,
  examples, and color.** argparse listed twelve commands in declaration order with no grouping, so
  "what can this thing do?" meant reading every line to find the one verb you wanted. Color comes
  from `codeintel.term`, so it degrades on a pipe, under `NO_COLOR`, and on a dumb terminal like
  every other human-facing command.
- **A mistyped command now suggests what you meant.** `codeintel gragh` → *did you mean `graph`?*;
  `codeintel dector` → *did you mean `doctor`?*. argparse's own error prints the full list of
  choices and stops, which is a dead end for a one-character typo — the way it actually gets hit.
  A prefix matches everything it could be (`serve` → `serve`, `serve-http`). Exits 2.

### Changed
- **`/code/doctor` over HTTP no longer returns `registrations`.** That field names the agent config
  files on the machine running the server — a local diagnostic. On a shared deployment the server
  is not an agent host, so it says nothing actionable and only hands a client holding the doctor
  scope the server user's home layout and which agent tools are installed there. The CLI and the
  stdio MCP tool, both running as the user on their own machine, are unchanged.

### Docs
- **New [docs/install.md](docs/install.md)** — the registration reference: what file each of the
  four agent hosts actually reads, with which key and entry shape; the three traps baked into that
  table (Codex is TOML; Claude Code reads `~/.claude.json`; Zed's entry is flat); why the registered
  command is absolute and what that costs; and the three levels of proof — file written, MCP
  handshake, release canary — with what each catches that the level above cannot. Verified against
  the shipped code: the config table is generated from `_CONFIG` and every troubleshooting string is
  grepped out of the source.
- README: corrected a stale test count, and linked the new doc from the registration section, the
  documentation list, and the docs index.

## [0.12.0] — 2026-08-16

This release is about **installation truth**: every change here closes a gap where a green signal
did not mean a working tool.

### Changed
- **`codeintel install` now defaults to `--agent auto`** — it registers only the agents whose config
  root exists on this machine, and names the ones it skipped. The old default (`all`) created
  `~/.gemini/settings.json` and `~/.config/zed/settings.json` for people who had neither, reaching
  into another app's namespace uninvited and claiming coverage of hosts nobody had verified. With no
  agent detected it writes nothing and exits non-zero. `--agent all` still forces every host.
- **Registrations now use the absolute path to `codeintel`.** The bare name is resolved by the
  *host*, not by the shell that ran `install` — and a GUI-launched desktop agent does not source
  your shell profile, so a command your terminal finds can be invisible to the app. This was the one
  failure the handshake verifier was structurally blind to, since it inherits the PATH that works.
  `--relative-command` restores the bare name; re-running `install` repairs a stale absolute path in
  place, leaving neighbouring config untouched.

### Fixed
- **Zed registration wrote a shape Zed does not read.** The `context_servers` entry was written as a
  nested `{"command": {"path", "args"}}` object; Zed's schema is flat — a `command` string with
  `args` beside it. The third instance of writing a config file the host ignores, after the Codex
  TOML and Claude Code `~/.claude.json` bugs, and again with green tests, because the test asserted
  the bytes we wrote. The canary now checks all four hosts' shapes.
- **CI was red on an environment-dependent installer test.** `test_every_agent_path_is_resolved_at_call_time`
  assumed `HOME` redirects every agent path, but `resolve_config_path` deliberately prefers each
  agent's own home var — and GitHub runners export `XDG_CONFIG_HOME`, so Zed correctly resolved
  outside the tmp dir. The test now clears those vars, and a companion test pins the override
  behavior it was accidentally asserting against.
- **`code.status` no longer claims an engine `code.query` cannot reach.** The gateway is built once
  per process, but status/doctor probe a *fresh* provider for any engine the gateway lacks — so a
  backend installed mid-session was reported `installed/runnable/ok` while queries kept routing
  around it for the life of the MCP host. Doctor's own remediation ("install it, then re-check")
  could never converge: the re-check passed and the answers stayed degraded. An engine a probe
  proves present is now **adopted** onto the live gateway, and the query path picks up a
  newly-installed backend on its own (throttled; skipped once all engines are present). A live
  provider is never replaced, so the warmed serena session and graph project cache the singleton
  exists to preserve survive untouched.
- **A cached fallback answer no longer outlives the engine's absence.** `overview` auto-falls back
  to LSP when the graph engine is missing and caches that under the *graph* key, which neither the
  content hash nor the freshness token can invalidate. Adopting an engine now clears the cache.

### Added
- **Release canary (`scripts/release_canary.py`) — CI gates on the built artifact, not the source
  tree.** Every result is a safe envelope with `ok: true` and the CLI never throws, so an exit-code
  smoke test passes against a build that boots cleanly and answers nothing. The canary installs the
  wheel into a clean env, registers Codex + Claude Code into a throwaway `HOME`, launches the command
  those configs name, and asserts on the **answer text** of a real `code.query` against a fixture
  repo. Wired into CI and, critically, into `publish.yml` — `twine check` validates metadata and
  never launches the thing. Verified to go red on both a config written where the host does not read
  it and a server returning `ok: true` with an empty body.
- **`verify.verify_stdio_call`** — handshake *and* invoke a tool, returning its response body, so a
  check can assert on the answer rather than on the envelope.

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
