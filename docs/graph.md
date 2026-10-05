# GraphProvider Reference

Wraps the `codebase-memory-mcp` CLI binary. Never raises — always returns an envelope.

## Install prerequisite

`codebase-memory-mcp` must be on `PATH` (detected via `shutil.which`). If absent, every
call returns a **safe null** with `reason: 'engine-unavailable'` — `ok` is still `true`; the
contract never returns `ok: false`.

`codebase-memory-mcp` is a standalone, platform-specific binary distributed by its own project —
install the build for your OS/arch and ensure it is on `PATH` (it self-manages via
`codebase-memory-mcp install|update`). Run `codeintel doctor` to confirm it is detected and that
this repo is indexed.

> ### Supported backend versions: `0.9.x` and `0.10.x`
>
> Both are read. They speak **different wire formats** and `codeintel` translates between them at a
> single seam (`BackendClient._decode` → `codeintel/wire_text.py`), so no op above the transport
> knows there are two:
>
> | backend | `query_graph` reply | notes |
> |---|---|---|
> | `0.9.x` | `{"columns": [...], "rows": [...]}` JSON | the original dialect |
> | `0.10.x` | a compact human-readable text layout | `list_projects` alone stayed JSON |
>
> **Prefer `0.10.x`.** Measured over the same three repositories, the share of `CALLS` edges below
> the 0.85 confidence floor falls from **24% / 33% / 43%** to **9% / 18% / 30%**, and Python
> enclosing-function attribution improves from ~32% of production caller rows collapsing to
> `module scope of <file>` to **2.7%**.
>
> ```bash
> pip install 'codebase-memory-mcp==0.10.*'
> ```
>
> **The 0.10.x text layout is not a contract** — it is output meant for humans and can change in a
> patch release. Every parser in `wire_text.py` therefore refuses rather than guesses: an
> unrecognised shape returns `None` and the op safe-nulls with `backend-incompatible`. A wrong answer
> assembled from a format we no longer understand would be worse than that refusal, which is the only
> thing that made the original `0.9 → 0.10` break diagnosable instead of looking like an unindexed
> repository.
>
> **Two operational traps.**
>
> - `codebase-memory-mcp update` **deletes every project index before** checking it can proceed, and
>   on a non-TTY then fails with `variant selection requires a terminal` — destroying the indexes for
>   nothing. Pass `--standard`.
> - Indexes are **not portable across `0.9`/`0.10`**. After switching, delete the repo's `.db` under
>   `~/.cache/codebase-memory-mcp/` and re-index.
>
> The backend re-initialises a native runtime on every invocation — roughly **6 seconds per call**,
> and `0.10.x` spawns a temporary daemon per CLI call unless one is running.
> `codebase-memory-mcp daemon start` keeps one warm and removes that cost; without it a long test run
> can flake on contention. If your machine is slower still, raise
> `CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS` (default 20000).

## Supported ops

| op | target | What it returns |
|---|---|---|
| `callers` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Up to 20 callers of the symbol (name + file path) |
| `callees` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Up to 20 functions called by the symbol |
| `impact` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Combined callers + callees section |
| `context` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Alias for `impact` — the graph's contribution to the `context` fan-out |
| `chain` | `"A->B"` or symbol | Call path from A (trace_path). Each hop carries how it was **resolved** (`[lsp]`, `[import]`, `[?name-guess]`), and the walk follows `CALL_REFERENCE` as well as `CALLS` |
| `pattern` | text pattern | search_code results for the pattern |
| `overview` | (ignored) | get_architecture output for the project |
| `changed` | *(optional)* a git ref — `main`, `HEAD~3`, a SHA, a tag, or `<ref>...HEAD` | With no target: impact of the **uncommitted git worktree**, changed files → impacted symbols (via `detect_changes`). With a ref: what a **branch** removed, re-signed or rewrote, and who still uses it — see [`changed` against a base ref](#changed-against-a-base-ref) |
| `changes` | *(optional)* a git ref | Alias for `changed` |
| `deadcode` | (ignored) | **Retired.** Always safe-nulls with `reason: "op-withdrawn"` — see below. |
| `hotspots` | (ignored) | Highest complexity / fan-in symbols — refactor-risk hotspots (via `search_graph`, client-sorted) |

### When several symbols share a name

`callers`, `callees` and `impact` resolve the target by its **unqualified name**, so a repository
with four methods called `invoke` matches all four. Rows are reported **separately per matched
symbol**, under a heading naming it, with the count of same-named symbols stated — they are that many
separate answers, not one. Nothing is dropped for being ambiguous. `callees` groups by the symbol
doing the calling and `callers` by the symbol being called; in both cases the heading is the symbol
your target matched and the rows are the other end of the edge.

To ask about one of them, qualify the target with text the answer already printed:

| target | means |
|---|---|
| `invoke` | every symbol named `invoke` |
| `core.Group.invoke` | the one whose qualified name ends in those segments |
| `invoke@src/click/testing.py` | the one defined in that file (a bare `testing.py` works too) |

A qualified or file-hinted target that matches nothing says so and lists the symbols that **do**
carry the name. It never reports zero rows, because "I could not find the symbol you named" and
"that symbol calls nothing" are opposite answers and only one of them is about your code. Note what
that message does and does not claim: a symbol can be indexed and still be absent from one of these
lists — `Group.invoke` has callees but no callers on one real repository — so it says "no symbol
matching this has callers here", never "not in this index".

Two things still limit these ops, and both are disclosed in the result rather than assumed away:

* The extractor emits edges for bare local names, so a callee in a different language family than
  its caller, or in a file that cannot hold code at all, is dropped as a name collision — reported
  as a count in the body and a `name-collisions-dropped` gap.
* The list is capped at 50 **distinct** callers (or callees), not 50 edges. The first query is a
  probe over every symbol carrying the bare name; when it comes back full, the op asks the backend
  what it holds, fetches just the symbol you asked about, folds repeated edges into one row per
  caller, and ranks production code ahead of test code before cutting. A cut list says so with an
  exact total (`50 shown, 63 in total`), how many of the missing callers are tests and how many
  production, and a `row-cap-reached` gap. Only when that follow-up cannot run does the old answer
  stand: truncated, total unknown.

### `deadcode` is retired

`deadcode` is retired (`_WITHDRAWN_OPS` in `graph.py`): it always returns a safe-null with
`reason: "op-withdrawn"` and a hint naming the substitute, and **there is no implementation left to
enable** — the `CODEINTEL_ENABLE_UNVERIFIED_OPS` opt-in that used to run it has been removed with it.

It was withdrawn pending a labelled-corpus measurement of its precision and recall. That corpus is
`tests/test_corpus.py::test_deadcode_precision_and_recall_are_measured_not_assumed`, and the
measurement retired the op: **25% precision as shipped**, and on real code with the harness's own
canaries removed it named 18 candidates across two pinned Python repositories of which **every one was
live**. Repaired as far as this codebase's existing filters reach, it named exactly one candidate, and
that one was live too. The README carries the full numbers and the reasoning:
[`deadcode` is retired](../README.md#deadcode-is-retired).

**Use `callers` on a specific symbol instead.** It answers the same underlying question — "does
anything call this?" — accurately, one symbol at a time.

### Relationship kind, and how an edge was resolved

Two independent axes, and conflating them was this engine's most consequential defect. They are
reported separately because they license different actions.

**Kind — what the edge asserts.** `callers`/`callees` match `CALLS|USAGE|CALL_REFERENCE`:

| kind | what it means |
|---|---|
| `CALLS` | invoked directly |
| `USAGE` | referenced, not called — module scope, or a mention that is not a call site |
| `CALL_REFERENCE` | **passed as a value or registered as a callback** — never invoked here |

Every row is badged with its kind, direct calls sort first, and the heading splits them whenever an
answer mixes kinds (`Callers of X (0 direct, 2 other reference(s))`). An answer made entirely of
`CALLS` keeps its plain count, so the common case is unchanged.

Why it matters: `set_forward_fn(app.forward_released_item)` registers a method at two real sites. The
backend stored both correctly as `CALL_REFERENCE`; codeintel queried only `CALLS|USAGE` and answered
*"no callers"* — the reading that deletes a live method. No confidence threshold could have recovered
it, because the edge existed at full confidence under a kind nothing asked for.

**Provenance — how the target was resolved.** The backend stamps every edge with `c.strategy`, and
that, not a numeric score, decides how a row is presented:

| class | strategies | shown as |
|---|---|---|
| resolved | `lsp_*`, `import_map`, `same_module` for a bare call (`run()` in the module that defines `run`) | no badge |
| **name guess** | `unique_name`, `suffix_match`, `heuristic`, fuzzy — and `same_module` when the recorded call text goes through a receiver (`subprocess.run` bound to the module's own `run`, `console.log` bound to the module's own `log`) | `[?0.75]` and counted in a note |

`same_module` is a lookup by scope, not an import and not a language server, and each row's `why`
says which mechanism produced it. The backend records the call as written (`c.callee`), which is
what separates the two cases above. The check is defined for **Python, JavaScript and TypeScript**,
whose member-call semantics are the same: a bare call is the module's own, `self`/`cls`/`super()`
(Python) and `this`/`super` (JS/TS) are the enclosing object, a receiver that is a segment of the
target's own qualified name (its class or module) is its own, and any other receiver is some other
value — so the row is name-matched, badged, and never dropped.

For every other `same_module` edge — another language, no call text recorded, or call text that is
not a call of the target's name — the check cannot decide, and **the row stays `resolved`, so
`verified` is `true`** (a check that cannot run must not move a row, and the bench numbers for those
languages stay where they were). Its `why` says exactly that and no more: scope resolved the call
inside the caller's own module, scope only binds a call written *bare*, whether this call is
written bare could not be checked (and why), so it is counted as resolved with that risk stated. A
`verified` row never says that no binding was followed.

A guessed row is kept, never silently dropped — dropping it would trade a false positive for a false
negative, and "no callers" is the more dangerous of the two when the next action is a delete.

The strategy is read rather than inferred from `c.confidence` because the two do not line up: on a
real repository `unique_name` appears at **both 0.75 and 0.38**, so a numeric floor splits one
strategy across two tiers and describes the same evidence two different ways. The float survives only
as a fallback for a backend that reports no strategy.

**The collision signature.** When *every* row in an answer of five or more is a name guess, the op
raises the `all-rows-name-resolved` gap — that is the shape of a name the index does not own (a
library function, a test-runner global, a builtin method) collecting every call site that mentions
it. The gateway escalates on exactly that condition and appends the language server's reference list
for comparison. It is the difference between `callers describe` returning 32 fabricated rows at
`confidence: "complete"` and returning them badged, counted, and next to the one real caller the
graph could not bind.

Measured accuracy for these answers is in [../bench/README.md](../bench/README.md) — with the
caveat that its numbers are **Python**. The `describe` failure above is TypeScript, and while a
TypeScript arm now exists, it has not been pointed at a real TypeScript repository, so no
measurement in that table speaks to the case this section describes.

### The repo-scan ops

`changed` and `hotspots` key on the whole index / git state, not a symbol, so `hotspots` ignores
`target`; `changed` reads it as an **optional git base ref** (see
[below](#changed-against-a-base-ref)) and is the original uncommitted-edits op when it is empty. An
empty scan (a clean worktree, no ranked symbols) is a **true answer** and returns an informative
string, not safe-null; only a backend failure returns safe-null.

### `chain` detail

If `target` contains `"->"`, the part before `->` is used as the source for a `trace_path`
call in `calls` mode. Otherwise, `chain` falls back to `impact`.

Two arguments matter:

- **`edge_types: [CALLS, CALL_REFERENCE]`** — so a walk does not stop dead at the point a function is
  handed to something rather than invoked.
- **`include_evidence: true`**, which replaced `risk_labels`. The backend treats those two as
  mutually exclusive, and nothing was lost: `risk` was a restatement of hop distance
  (hop 1 = `CRITICAL`, hop 2 = `HIGH`, hop 3 = `MEDIUM`) that every row already prints as `[hop N]`,
  so it dressed a visible number as an assessment nobody made. Evidence is the fact `chain` could not
  report before — a `[?name-guess]` hop makes everything downstream of it suspect, and used to be
  indistinguishable from a resolved one.

### `changed` detail

`changed` calls `detect_changes`, which drives a backend-side reindex of the changed files (so it
gets a higher timeout floor, 15 s). Its result is **never cached** — the content-hash cache key can't
see the live git worktree, so a cached answer would be stale. The backend returns duplicate
`changed_files` (staged + unstaged) and mixes file markers into `impacted_symbols`; the provider
dedupes the files and keeps the symbol list symbols-only.

The answer has **three** sections, and the middle one is not what its old heading claimed:

1. **Changed files** — the uncommitted source files, non-source dropped but counted (a tree of only
   `.md` edits must not report as "clean").
2. **The backend's symbol list**, whose meaning differs by dialect: `0.9.x` returns the symbols
   *defined in* the changed files (containment), `0.10.x` returns a *transitive* impacted set stamped
   with a `hop`. Same field, two meanings — so the heading is derived from the data and prints the hop
   rather than asserting the older reading, which had filed symbols from three unrelated files under
   "defined in the changed files".
3. **Callers elsewhere that reach into them** — the actual blast radius, computed here rather than by
   the backend: symbols *outside* the changed files with a `CALLS`, `CALL_REFERENCE` or `USAGE` edge
   into them, one row per calling symbol at its strongest claim.

`changed` answers a **recall** question — "what should I look at before committing" — so the
asymmetry runs opposite to `callers`: indirect edges are included rather than withheld, ranked below
direct calls, and every row labelled. Under-reporting impact is how live code gets broken;
over-reporting costs a reader one line. An empty ripple says so out loud, because an absent section
and an empty one read identically to a model and only one of them is a claim.

### `changed` against a base ref

`code.query op=changed target="main"` (or `codeintel query --op changed --target main`) answers the
question the no-target form cannot: **who uses the functions this branch removes or rewrites?** The
no-target op fails it three ways — it sees *uncommitted* edits only (a committed branch reports
"working tree clean"), it is *file*-granular (one function appended to an 80-symbol file reports 80
symbols), and it can say nothing about a function that was *deleted*, because the graph indexed at
HEAD has no node for it.

**What is compared.** `<ref>` is a branch, a tag, a SHA, `HEAD~N`, or `<ref>...HEAD`. The answer
compares `merge-base(<ref>, HEAD)` against the **working tree** — committed, uncommitted and
untracked-but-not-ignored changes together — so it reads as *what this branch would change if it were
committed now*. The merge-base rather than `<ref>` itself, so that work which landed on the base after
this branch forked is not reported as this branch's. Ranges that mean a different diff are refused
(`unsupported-range`) rather than quietly reinterpreted: `a..b` is `a` itself against `b` in git, and
`a...b` with `b` other than HEAD compares two trees that are not the working tree.

**How it is built**, in four steps, each with its own source:

1. **Which files.** `git diff -M` against the merge-base, so a renamed file is one entry, plus the
   untracked files git has not seen. Files that are not source this op reads — configuration,
   documentation, generated files, and any language with no reading (Ruby, PHP, C#, Kotlin, Swift …)
   — are **named and not compared**, and that is a gap (`non-source-changes-not-compared`), not a
   footnote: see [below](#what-was-not-compared). Every `git` call here runs with
   `core.fsmonitor` overridden, because a repository's own config can name a command that git would
   otherwise run on `diff` and `ls-files`.
2. **Which definitions** (`symbol_diff.py`, pure — two strings in, a classification out). Each
   definition, keyed by qualified name (`Class.method`), is `removed`, `signature` (parameters, return
   annotation, decorators or sync/async differ — or the language's equivalent header), `body` (header
   equal, implementation not) or `added`. A signature entry says *which* part moved
   (`parameters (-verified_only, +keep)`), and a body entry whose only change is a docstring is marked
   `docstring only`. A class's own statements changing (an attribute, a dataclass field) is a `body`
   entry marked `class-level statements`, listed after the function bodies. Reformatting and comments
   are not changes. Python is read with `ast`;
   TypeScript, JavaScript, Go, Rust and Java go through the same tree-sitter tables the indexer chunks
   with, so "what is a definition" has one answer across the product. **Everything else degrades
   honestly:** C and C++ (whose definition names the indexer does not read), unknown extensions, and
   a missing grammar come back file-granular with a `symbol-diff-unsupported-language` gap, and a file
   that does not parse comes back `symbol-diff-unparsable` — never a classification nobody checked.
3. **Who calls what still exists.** For every `signature` and `body` symbol the op asks the ordinary
   `callers` op, so the rows, their `resolved` / `name-matched` evidence and their badges are exactly
   what `callers` would say — there is no second query and no second labeller. Each group's callers
   are then split into **callers this diff did NOT touch — these may break** and **callers also
   changed in this diff — probably updated together**. That split is the point: the second list is
   what a reviewer would otherwise have to find by hand, and the first is the part nobody has looked
   at. A *module-scope* caller is never counted as touched (the diff compares definitions, not a
   file's top-level statements), so it stays on the side that over-reports.
4. **Who still mentions what was removed.** The graph cannot say: it was built from a tree that had
   the function. So a removed symbol's survivors come from `git grep -w` over the working tree, and
   they are **text mentions, not resolved calls** — a same-named symbol elsewhere matches, and so
   does a comment. Python hits are labelled `code` / `string` / `comment` / `definition` by the parser
   (comments and definitions are set aside and counted; a string is *kept*, because
   `__all__ = ["name"]` and `patch("pkg.name")` break when `name` goes). Other languages cannot be
   told apart and say so. The group is labelled `discovery`, never `evidence`, and its rows carry
   `evidence: "name-matched"`, `verified: false`, `strategy: "git-grep"`. The search output is read
   up to a fixed bound (2 MB) and no further; a search cut there is a lower bound, so the group's
   total is unknown (`evidence.total: null`). A name that survives only in a file this op does not
   read as code — an entry point in `pyproject.toml`, a YAML or JSON config — is not a row, but its
   files are counted and named (`mentions-outside-source`): a removed function named there fails at
   runtime all the same.

**The order a reader meets it in:** removed-and-still-mentioned, then signature changes, then body
changes (within a section, the symbol with the most untouched callers first), then removed with no
mention, then `added` (listed for context — a caller that is itself new is not one the diff left
alone). At most 40 symbols are looked up, most severe first, and what the cap drops is named.

**Rows and envelope.** `rows[]` is the body's `- ` lines, line for line, each stamped with
`changed_symbol`, `change`, `caller_status` (`untouched` / `also-changed`) and `group_class`
(`evidence` / `advisory` for a graph group, `discovery` for text mentions). The envelope's own
`evidence_class` stays `discovery` — that is `changed`'s ceiling — and `evidence.safe_for_destructive`
is the existing derivation (no unverified row, nothing withheld, no gap), so it is `false` for any
answer containing a text mention or a gap, and `true` only when every group's callers were resolved
and the list was whole. `evidence.total` is stated only when it is known: rows a group's own print
limit leaves out **and** the rows `callers` kept back behind its distinct-caller cap are both counted
(120 callers with 50 kept is `total: 120`, not 50), and a group whose row cap or text search was cut
off has an unknown total (`null`), never the number of rows that happened to fit.

Every way the answer can fall short is a named gap, also stated in the body under *Limits of this
answer*:

| gap | raised when |
|---|---|
| `symbols-truncated` | more than 40 symbols changed; the lookup covers the most severe 40 |
| `files-truncated` | more than 300 source files changed |
| `symbol-diff-unsupported-language` | a changed file is in a language with no definition-level reading |
| `symbol-diff-unparsable` | a changed file does not parse (or could not be read); which of its definitions moved is *unknown*, not none |
| `symbol-diff-unnamed-skipped` | a definition has no usable name (an anonymous export) and was not compared |
| `non-source-changes-not-compared` | a changed file is not source this op reads (config, docs, generated, or a language with no reading); up to five are named |
| `module-level-not-compared` | a changed file shows no definition-level change — it changed only outside any definition (imports, constants, module statements) or is only renamed |
| `renamed-module-importers-unchecked` | a file was renamed or moved; importers of the old module path were not asked about, so a clean caller list says nothing about them |
| `untracked-files-unknown` | `git ls-files --others` failed, so files that are new and not yet added are missing from the comparison |
| `callers-unavailable` | a caller lookup failed, ran out of its time allowance, or a text search failed — *unknown*, never "none". For a backend that **refused to run** the detail carries the fix as well as the message (the same sentence `callers` puts in its hint) |
| `callers-incomplete` | a lookup returned rows **and** a backend call inside it failed, so the rows are a lower bound; the failed call is named |
| `symbol-not-indexed` | the symbol is not in the graph index at all, so its callers are unknown |
| `no-graph-callers` | the symbol is indexed and has no recorded caller — **not** proof of none (framework dispatch, a call through a value) |
| `callers-inconclusive` | the graph returned no rows for exactly that symbol |
| `text-mention-only` | a removed symbol's users could only be found by text |
| `mentions-truncated` | the text search hit its cap (or its output bound); the group's total is unknown |
| `mentions-outside-source` | a removed name is still written in files this op does not read as code (a manifest, a config); up to five are named, configuration before prose |
| `stale-index` | the graph index was last written before a changed file, or before HEAD last moved by a checkout, merge, reset or rebase (a plain commit does not count) — so a caller added since is missing |
| `index-age-unknown` | the index's age could not be read; the answer does not imply it is current |
| *the `callers` gaps* | `row-cap-reached`, `low-confidence-edges`, `all-rows-name-resolved`, `non-call-relationships`, … are forwarded with the symbols they affect. One gap per kind, and each symbol keeps **its own** detail (`` `a`: 12 of 40 rows …; `b`: 3 of 3 rows … ``) — never the first symbol's figures under every name |

The index's age is read from the graph backend's own file for the project (the directory `reset`
already knows how to find). `codeintel status` prints the *semantic* index's age, which is a
different artefact.

<a id="what-was-not-compared"></a>

**What was not compared — said as gaps, never as silence.** The definition-level diff leaves four
things out, and each is **counted, named and raised as a gap whenever the count is above zero**, so
`confidence` is `partial` and `safe_for_destructive` is `false` while any of them is true of the
branch:

- *Files this op does not read as source* (`non-source-changes-not-compared`). A branch that only
  touches `app/user.rb` does **not** read "no source file differs": it says what was compared, what
  was **not**, and that this is not a statement that nothing changed.
- *Changes outside any definition* (`module-level-not-compared`): imports, module constants,
  top-level statements. A definition-level diff cannot see them.
- *Renamed or moved files* (`renamed-module-importers-unchecked`): the definitions are the same, so
  nothing is reported for them, but anything importing the **old module path** breaks and was never
  asked about.
- *Non-source mentions of a removed name* (`mentions-outside-source`).

A function moved between two *different* files is `removed` in one and `added` in the other, with a
note on the removed one that the name now exists elsewhere (whether they are "the same function" is a
guess this op does not make). A nested function is part of its parent's body. A definition that
changes *kind* (`def Foo` becoming `class Foo`) is a `signature` change — `kind changed (function →
class)` — and does not stop the rest of its file from being compared.

**Safe-null reasons for this form** — never a raised error, and never `not-in-graph`:

| reason | outcome | when |
|---|---|---|
| `'unknown-ref'` | `not_found` | `<ref>` does not name a commit (or is not a ref name git would accept) |
| `'not-a-git-repo'` | `unavailable` | `project_root` is not inside a git work tree, or does not exist. The hint does not offer no-target `changed` as the alternative: it asks the backend's `detect_changes`, which asks git, and reports zero changed files in a directory with no `.git` |
| `'no-merge-base'` | `unavailable` | `<ref>` and HEAD share no ancestor (unrelated histories, a shallow clone, or HEAD has no commits) |
| `'unsupported-range'` | `unavailable` | a `..` range, or a `...` range whose right side is not HEAD |
| `'git-unavailable'` | `unavailable` | the `git` binary is not installed |
| `'no-project-root'` | `unavailable` | no `project_root` was given |

## Project resolution

Before any query the provider calls `list_projects` to find a project whose `root_path` matches
or is a prefix of `project_root`. The result is cached per `project_root` for the lifetime of
the provider instance.

## Budget / timeout

`budget` (milliseconds) sets the subprocess timeout. If `budget` is 0 or absent, the timeout
defaults to **5000 ms**.

## Safe-null reasons

| reason | When returned |
|---|---|
| `'engine-unavailable'` | `codebase-memory-mcp` not on PATH |
| `'backend-unreachable'` | The backend did not answer while resolving the project — it timed out, **refused to run** (the `hint` quotes its own message and the fix; see [doctor.md](doctor.md#when-the-graph-backend-is-installed-and-fails)), or replied in a form this release cannot read |
| `'project-not-indexed'` | No project found for the given `project_root` |
| `'project-not-indexed-standalone'` | The repo isn't indexed on its own — it only resolves via a containing ancestor project, and the op (`overview`/`changed`/`changes`/`hotspots`) is scoped to the repo boundary, so it refuses rather than answer for the wrong tree |
| `'unsupported-op'` | `op` is not one of the ops listed above |
| `'op-withdrawn'` | `op` is `deadcode`, which is retired — see [above](#deadcode-is-retired) |
| `'not-in-graph'` | The op ran and the target genuinely is not in the index — a stale index, a typo, or a rename |
| `'no-edges'` | The target **is** indexed and simply has no edge of this kind. A different fact from the row above, and it licenses the opposite action: framework-dispatched handlers (routes, ASGI apps) look exactly like this, so it must not be read as dead code. The hint names where the symbol is defined and censuses the relationships that *do* point at it. Re-indexing will not change it |
| `'backend-incompatible'` | The reply matched **neither** supported dialect — most often a backend newer than this codeintel. Upgrade codeintel first; failing that, pin `codebase-memory-mcp==0.10.*` |
| `'unknown-ref'` / `'not-a-git-repo'` / `'no-merge-base'` / `'unsupported-range'` / `'git-unavailable'` | `changed` with a base-ref `target` only: the ref does not name a commit, the root is not a git work tree, the histories share no ancestor, the range is not `<ref>` or `<ref>...HEAD`, or git is not installed — each [with its own meaning](#changed-against-a-base-ref), and none of them a statement about the index |
| `'timeout'` / `'backend-error'` / `'unparsable'` | Returned dynamically (as `reason: miss.kind`) when a backend call inside the op itself timed out, errored, or returned something unreadable — distinct from `not-in-graph`, which means the call succeeded and the target genuinely isn't there |
| `'error'` | Unexpected exception during execution |

## Envelope shape

```json
{
  "ok": true,
  "op": "callers",
  "target": "build_result",
  "result": "## Callers of build_result\n- gateway (src/codeintel/gateway.py)",
  "engine": "graph",
  "cached": false
}
```

On failure `ok` stays `true`; `result` is `null` and `reason` carries the failure.

## Example CLI call (direct, bypassing the gateway)

```bash
codebase-memory-mcp cli query_graph '{
  "project": "codeintel",
  "query": "MATCH (caller)-[:CALLS]->(fn) WHERE fn.name=\"build_result\" RETURN caller.name, caller.file_path LIMIT 20"
}'
```

```bash
codebase-memory-mcp cli search_code '{"project": "codeintel", "pattern": "safe_null_result"}'
```
