# Call-edge benchmark

Measures **who-calls-this** accuracy against labelled ground truth, so the choice between engines is
arithmetic rather than argument. It exists because every accuracy claim made about this tool — in
either direction — had rested on a handful of hand-checked symbols, and two careful readings of that
same evidence produced opposite designs.

> **Two benchmarks live in `bench/`, on different axes.** This one measures *accuracy* — is the
> answer right. The [agent-cost benchmark](#agent-cost-benchmark) measures *what the answer costs* —
> tokens and tool calls to get it. Neither substitutes for the other, and the second exists because
> the first cannot support a claim about cost no matter how good its numbers are.

```bash
# Against real repositories — needs a live graph backend and an indexed clone.
CODEINTEL_BENCH_PATHLY=~/src/pathly-adapters python bench/run.py pathly-adapters
CODEINTEL_BENCH_SNITCH=~/src/snitch-simulator python bench/run.py snitch-simulator

# Against a real TypeScript repository.
CODEINTEL_BENCH_DAYCAP=~/src/daycap python bench/run.py daycap

# Against a DIFFERENT TypeScript repository — set the path, then list its disputed symbols in run.py.
CODEINTEL_BENCH_TS=~/src/some-app python bench/run.py typescript

# Against the checked-in corpora — needs nothing, runs in CI, pins both oracles.
pytest tests/test_bench_oracle.py tests/test_bench_oracle_ts.py
python bench/run.py corpus-ts        # the TypeScript arm end to end, on 20 known files
```

The repository paths used to be hardcoded to one laptop, which meant the artifact that turns this
project's accuracy arguments into arithmetic could be reproduced by nobody and re-run by nobody after
a backend release. They come from the environment now, and a missing clone says so instead of scoring
every arm against an empty tree.

## Before you trust a run of this

**The two LSP arms need the clone's own `.serena/project.yml` to name the language being scored.**
serena gets one config per project and that config names a fixed list of language servers; a
language missing from it produces empty `symbol` bodies rather than errors, so both LSP arms report
`n/a` for a reason that has nothing to do with the LSP. Nothing said so here until a re-run lost
both arms to it. Run `codeintel doctor --deep` inside the clone first — it names the gap and the
edit that closes it. A TypeScript clone needs a `tsconfig.json` as well, for a sharper reason given
under [the TypeScript arm](#the-typescript-arm).

**Every run now prints what it measured, before it measures it:**

```text
provenance — what this run measured
  scored tree  /Users/…/pathly-adapters
               fix/board-lifecycle-telemetry @ c1def41b  (clean)
  engine       codeintel 0.23.3  ->  /Users/…/.local/bin/codeintel
  backends     codebase-memory-mcp 0.10.8, uv 0.12.1, fastembed 0.8.0, sqlite-vec 0.1.9
  !! the checkout at /Users/…/codeintel declares 0.23.4, but the `codeintel` on PATH is 0.23.3.
     This run measures the INSTALLED build, not your working tree.
```

That header exists because this harness names neither half of its own provenance by itself. It
shells out to whatever `codeintel` is on `PATH` — which is **not** necessarily the checkout you are
reading; on the machine that produced the table below it was the checkout minus exactly one commit,
and that commit was `2905ea2`, a *caller-resolution* fix — and it scores whatever branch the clone
happens to sit on. A re-run that recorded neither produced numbers
nine points off this table and could not say why; the finding below is what that cost to establish.
Copy this block beside any number you copy out of a run.

When that last line fires, `CODEINTEL_BENCH_EXE` is how you act on it — point it at a wrapper that
runs your working tree and the benchmark scores your edits instead of the build you installed weeks
ago:

```bash
printf '#!/bin/sh\nexec env PYTHONPATH="$PWD/src" python -m codeintel "$@"\n' > /tmp/codeintel-src
chmod +x /tmp/codeintel-src
CODEINTEL_BENCH_EXE=/tmp/codeintel-src python bench/run.py corpus-ts
```

## What it measures

Three arms, per **question**, because the questions have opposite failure costs:

| arm | what it is |
|---|---|
| `graph` | what codeintel reports today, through its own envelope — what an agent actually receives |
| `lsp_raw` | the language server's references taken as callers |
| `lsp_classified` | the same references, with the syntax at each site deciding whether it is a call |

* **direct callers** — precision-first. A fabricated caller sends an agent to edit unrelated code.
* **change impact** — recall-first. A missed dependant is how live code gets broken.
* **wrongly silent** — counted on its own: returned nothing for a symbol that has callers. The
  deletion trap, and the most consequential error this class of tool can make.

## Why the oracle can be trusted

The obvious trap is circularity: if the oracle is a call-graph resolver, scoring engines against it
measures agreement with a third resolver. `oracle_py.py` avoids that by labelling **only what a
file's own syntax and import table make unambiguous, and abstaining on everything else.** An
attribute call on a value (`self.thing.run()`), a star import, a rebound name — those are recorded
`undecidable` and excluded from scoring. Coverage is reported on every run, so the population where
the answer is genuinely known is visible rather than assumed.

## Abstention alone was not enough

Truth has to include **proven negatives**, or the benchmark cannot charge a fabricated caller
anything. Scoring restricts every claim to sites the oracle judged, so while it judged only positives,
a claim on an unjudged site was silently dropped. That is fine for `self.thing.run()`. It was not fine
for the *bare name nothing binds to the target* — which is the exact shape of the worst failure this
project has seen, 32 invented callers for `describe`, matched across files that never imported it.
Measured under positives-only truth those 32 rows cost nothing at all: the symbol left the population,
coverage went to 0%, and every arm scored 100%.

So `not-target` is a fourth label, and the rule for it is deliberately narrower than "not imported
here". A bare name is a proven negative only when the file's own syntax **accounts** for it — a
parameter, an assignment, a `def` in scope, an import of something else, or a builtin. Then it
provably denotes that other binding. A name nothing in the file accounts for is a true injected
global, which in Python another module could have installed, so it stays `undecidable`.

One precedence rule matters as much as the label: **doubt anywhere in a key outranks the negative.**
A function that both binds an unrelated `run` and calls `self.thing.run()` has one readable site and
one unreadable one. Scoring it as a proven non-caller would charge an engine a false positive for a
claim that might be right — the mirror of the defect being fixed, and just as wrong.

Transitive **re-exports are followed**, because `from .mod import name` is an explicit statement, and
following stated imports is what a correct resolver does — the opposite of matching bare names. That
one addition took an early run from judging 17% of a symbol's sites to 100%.

Labels are relationship **kinds**, not confidences: `call`, `reference`, `import`, `not-target`,
`undecidable`. Conflating those axes was the defect that motivated the whole exercise.

## The fixture corpus

`bench/fixtures/corpus` (Python) and `bench/fixtures/corpus_ts` (TypeScript) are checked-in
micro-repositories with a known answer for every site, and `tests/test_bench_oracle.py` /
`tests/test_bench_oracle_ts.py` assert the label of each one. It does not replace a run against real
code — real repositories are where the mess lives — but it is the floor, and it runs without a
backend, a clone, or either private repo. It pins the three defect classes that each silently zeroed
a result: a `src/` source root, a transitive re-export, and a bare name bound to something else.

## Targets are stratified, not sampled

A random draw from a real repository is dominated by easy cases and every engine scores well on them.
The list in `run.py` is built from the cases actually in dispute: names shared with a framework
global, symbols reached through a re-export, functions only ever passed as a value, handlers
dispatched by a framework and never called at all. That biases the absolute numbers **downward** on
purpose.

## Findings so far

Ten Python symbols in `pathly-adapters` and six in `snitch-simulator`, **under proven-negative
truth** — re-measured **2026-09-17** with the two commands at the top of this file, which is what
reproduces the table:

| arm | direct precision | direct recall | impact precision | impact recall | wrongly silent |
|---|---|---|---|---|---|
| `graph` | 90% | 100% | 93% | 100% | 0 / 10 |
| `graph_verified` | 97% | 78% | 97% | 74% | 0 / 10 |
| `lsp_raw` | 73% | 100% | 78% | 100% | 0 / 10 |
| `lsp_classified` | **100%** | 100% | **100%** | 100% | 0 / 10 |

Measured with `codeintel 0.23.3` — the checkout minus `2905ea2` — against
`codebase-memory-mcp 0.10.8`, on `pathly-adapters` at `c1def41b` (branch
`fix/board-lifecycle-telemetry`, clean) and `snitch-simulator` at `373b6dd` (branch
`simulator-with-schema-builder`, clean). That the engine was one commit behind the checkout is worth
recording rather than fixing silently: `2905ea2` is a caller-resolution fix, so the table is a
floor for the current source, not a reading of it.

### `graph_verified`, and the price of filtering

`graph_verified` is the `graph` answer with `rows[].verified` applied — only the edges that followed
a real import or language-server binding. It exists because the readiness doc's general-release gate
names **verified-caller** precision, and every other number on this page is precision over ALL rows,
which is a different quantity that had been standing in for it.

Measured 2026-09-17 with `codeintel 0.23.4` (the checkout, via `CODEINTEL_BENCH_EXE`), same trees
and same backend as the rows above:

| repository | `graph` direct | `graph_verified` direct | `graph` impact | `graph_verified` impact |
|---|---|---|---|---|
| `daycap` | 100% / 100% | 100% / 100% | 100% / 95% | 100% / 95% |
| `snitch-simulator` | 100% / 100% | 100% / 100% | 100% / 82% | 100% / 73% |
| `pathly-adapters` | 90% / 100% | **97% / 78%** | 93% / 100% | 97% / 74% |
| `corpus-ts` | 50% / 80% | **75% / 60%** | 50% / 50% | 75% / 38% |

Two readings, and the second is the one that matters.

**Filtering buys precision and costs recall, as expected.** On the two repositories where the graph
arm is already perfect, the filter changes nothing about direct callers — every row there had
followed a binding. Where it is not perfect it helps: +7 points on `pathly-adapters`, +25 on
`corpus-ts`. The recall cost is larger than the precision gain in both cases (−22 and −20).

**It did not open the deletion trap on this corpus.** `wrongly silent` is **0 on every arm of every
repository, `graph_verified` included** — across all 27 scored symbols, not one had its entire
caller list removed by the filter. That is a real result and it is narrower than it looks: it says
the filter never *silenced* a symbol here, not that filtering is safe. The rows it removes are
overwhelmingly *additional* callers of symbols that also had verified ones (`read_flow_nodes`: 5
rows → 2; `_decompose_flow_dict`: 14 → 12), so the arm keeps answering and answers less. A symbol
whose only callers are name-matched is the case that would score `wrongly silent` here, and the
stratified target lists do not currently contain one.

So this does not settle the standing argument for keeping heuristic rows in the default answer — it
prices it. What it removes is the assumption that the price was unmeasurable.

### Why these numbers went UP, and what that says about the instrument

The 2026-09-03 table read `graph` at 80% / 78% and `snitch-simulator` at 38% / 33% / 60%. The engine
has not changed. **The oracle was wrong, in the one label that exists to charge an engine for being
wrong.**

`not-target` marks a bare name a proven non-caller when the file's own syntax accounts for it — a
parameter, an assignment, *a `def` in scope*. In the target's own defining module that `def` is the
target, and the oracle read it as "some other `x` this module binds". So **every call a function
made to itself from its own file was scored as a fabricated caller**, and the engine that found
those call sites was charged a false positive for each one.

`snitch-simulator` is where it bit hardest, because its targets are methods in the file that uses
them: `_strip_hop_by_hop` is called three times inside `proxy.py`, all three were counted against
the graph arm, and 38% direct precision was the result. Corrected, it is **100%**. On
`pathly-adapters` four targets each lost one mislabelled site and `_claude_tokens` two, taking the
graph arm from 80% to 90% and `lsp_classified`'s impact precision from 84% to 100%.

Three things worth being uncomfortable about:

* **The instrument was the least-tested code in the argument.** `tests/test_bench_oracle.py` pins a
  corpus in which no file ever called a symbol it defined, so the rule was never exercised where it
  is wrong. `bench/fixtures/corpus/src/corpuspkg/self_call.py` now covers both halves — the
  home-module call, and a same-named parameter that must stay a proven negative, because trading
  one wrong label for its mirror would be the same defect facing the other way.
* **It failed in the flattering direction for the argument this file makes.** A benchmark that
  exists to stop a tool overstating itself was understating it, which is the bias least likely to
  prompt anyone to look.
* **It is the same defect class the tool keeps shipping**, one level up: `_accounted_by` returns
  *where* a name is bound, and the caller read it as *what* it is bound to. True about the location,
  false about the identity — the shape `outcome.py` and the `doctor` checks were each written for.

`snitch-simulator` is reported separately rather than averaged in, because its arms did not answer
the same question: `graph` scores **100% direct precision, 100% impact precision and 82% impact
recall** over six symbols, and **both LSP arms are `n/a` — 6 of 6 unanswered**, the language server
having resolved none of those symbols. An arm that answered nothing cannot be pooled with one that
answered ten times.

Three things worth stating plainly:

1. **`lsp_classified` is the most precise arm, and `lsp_raw` the least.** Raw references taken as
   callers are import lines and duplicate rows; classifying each site by its syntax removes all of
   them and costs no recall. "Promote the LSP to authority for callers" would have shipped a
   regression; "LSP locates, syntax classifies" is what the numbers support.
2. **The graph engine is not exact, and the earlier claim that it was is what proven negatives
   corrected.** Its remaining 10 points of lost direct precision are almost entirely `_broadcast`,
   which has one real caller and five proven non-callers — a short common name, on the list because
   that is where the fabrication class lives. Under positives-only truth those sites left the
   population and every arm scored 100%. This is also a result *about 0.10.8*, which fixed the
   Python attribution defect; 0.9.x would look materially worse.
3. **This table is Python.** The worst failure ever observed here (`describe`, 32 fabricated
   callers) is TypeScript. A real TypeScript repository is now measured too — see
   [daycap](#a-real-typescript-repository-daycap) below — but it is a *different* repository with a
   different shape, and neither table can be read as the other's result.

### A number here belongs to a tree, not only to an engine

A re-run in the intervening fortnight reported `graph` at **71% direct / 70% impact** and read as a
nine-point regression. It was not one, and establishing that took longer than the re-run did.

That run scored a fresh `--depth 1` clone of `pathly-adapters`. The table above scores the working
tree `bench/run.py` defaults to, which is two weeks older, and `_broadcast` — the symbol that
dominates this score — grew in between:

| tree | `_broadcast` proven non-callers | true calls, all ten targets |
|---|---|---|
| `c1def41b`, 2026-08-24 — the table above | 5 | 36 |
| `1371485`, 2026-09-03 — the fresh clone | 10 | 34 |

Those two populations are re-derived here by running the oracle alone, which needs no backend, over
both trees. **The engine did not move. The denominator did**, and precision is a ratio over a
population that the repository owns: the newer tree doubled `_broadcast`'s proven non-callers, and
`_broadcast` is the symbol that dominates this score.

(The counts in this table were themselves re-derived after the oracle defect described below; the
run that first reported the discrepancy saw 6 and 11 against a truth that was wrong in both trees
by the same defect. The conclusion it supported — that a percentage here belongs to a commit —
survived the correction unchanged, which is the only reason it is still stated.)

Two things follow, and the second is the uncomfortable one.

1. A percentage in this file is comparable only against the same commit, which is why every run now
   prints one. A re-run that disagrees with this table should be diffed **per symbol** against the
   per-symbol counts recorded above before it is read as a trend — the totals cannot distinguish a
   worse engine from a bigger repository, and on this occasion they did not.
2. **Stratification makes this worse, not better.** `_broadcast` is on the target list precisely
   because short common names are where fabrication lives, so the one symbol carrying most of the
   score is also the one whose site count moves fastest in an active repository. The bias that makes
   the list honest is the same property that makes the number restless. That is a fact about the
   measurement rather than about the tool, and it is not fixed by choosing gentler targets — it is
   fixed by pinning the commit, which is now recorded rather than assumed.

A number here is only as current as the run that produced it. Re-run both commands rather than
quoting the table after a backend release; the previous version of this table survived a backend
change and a change of truth definition, and was wrong on both counts by the time anyone re-ran it.

## The TypeScript arm

`oracle_ts.py` labels TypeScript with the same five kinds, sharing `Site`, `FileVerdict` and `Truth`
with the Python oracle so one scorer reads both. It could not have been built a commit earlier:
under positives-only truth every `describe` site was unjudged, so an arm pointed straight at them
would have reported `n/a` or 100% and measured nothing.

The result worth stating is that **TypeScript is more decidable than Python on exactly that case.**
Python must abstain on an unbound bare name, because another module can install a global. An ES
module's bindings are exhaustively stated — a module-scope symbol in another file is reachable only
through an `import`, and `import * as ns` binds a namespace object so its uses stay `ns.foo` and stay
readable. So "this file is a module, calls a bare `describe`, imports no `describe` and declares
none" is a **proven negative** where the Python equivalent is an abstention. The two oracles are
asserted against each other on that one shape in
`test_the_case_python_must_abstain_on_is_decidable_here`.

Three guards keep that argument honest, because each is a real way it fails:

| guard | why |
|---|---|
| **script files** | a file with no import and no export is not a module. Its top-level names share the global scope, so reachability says nothing and every bare name in it is undecidable. |
| **self-installed globals** | a tree that assigns `globalThis.foo = ...` anywhere has manufactured the escape hatch the argument denies. The oracle abstains on that *name* tree-wide. |
| **unresolvable specifiers** | `import { foo } from "@app/proxy"` may be a path alias for the target or a package sharing a name. Unless tsconfig `paths` or `node_modules` settles it, the name is undecidable in that file. |

Stated re-exports are followed transitively (`export { x } from`, including `export *`), aliased
imports are tracked under their new spelling — scanning only for the target's own name finds the
import and none of its callers — and a property access on a value stays an abstention, as in Python.

`bench/fixtures/corpus_ts` is 20 files covering all of it, and `python bench/run.py corpus-ts` drives
the whole path — oracle, scorer and both engines through codeintel's own envelope. It is a **smoke
test, not a measurement**: files written to have a known answer cannot say anything about real code.
The real measurement it could not supply is below.

**Index the corpus standalone before reading anything into that run.** It lives inside this
checkout, so unless `codeintel index bench/fixtures/corpus_ts` has been run, the backend answers
from the enclosing project and spells every path relative to *that* root — no key can match the
oracle's, and the arm scored 0% recall with 0 spurious, which reads as an engine that found nothing.
`graph_answer` now refuses on the envelope's `ancestor-scope` gap and reports the arm **unanswered**
instead, the same way it already refused the LSP's `not-asked`. That the benchmark built to catch
silent runs of zeros had one of its own for three arms is the argument for reading a `0%` as a
question rather than a result.

Also open: the oracle abstains on property accesses on values, which is exactly where short common
names live. Proven negatives raise coverage on the shadowing cases; the receiver case is untouched
and is harder, because resolving a receiver's type means type inference — which reintroduces the
circularity the oracle exists to avoid.

### A real TypeScript repository: daycap

`python bench/run.py daycap` — eight symbols, 23 source files, `codeintel 0.23.3` against
`codebase-memory-mcp 0.10.8`, tree at `main @ 71502b8` (clean), 2026-09-17:

| arm | direct precision | direct recall | impact precision | impact recall | wrongly silent |
|---|---|---|---|---|---|
| `graph` | **100%** | 100% | **100%** | 95% | 0 / 8 |
| `lsp_raw` | 77% | 100% | 81% | 100% | 0 / 8 |
| `lsp_classified` | **100%** | 100% | 81% | 100% | 0 / 8 |

Oracle coverage 100% mean. The single impact miss is `resolveSource`, where the graph reported the
three calls and not the one non-call reference — the `forward_released_item` shape, and the reason
impact is scored separately from direct callers.

**Read this table narrowly, because its population is easy and that is a fact about daycap.**
`bench/run.py`'s other lists are stratified onto disputed cases; here there was almost nothing to
dispute. daycap has 23 source files, a `tsconfig.json`, a correct `.serena/project.yml`, and
essentially no name collisions — `scrub`, `isTrusted` and `isUnusable` went on the list as the short
common names and each turned out to be unique in the index. A repository that hands the resolver an
unambiguous name gets an unambiguous answer, and 100% is what that looks like. It is a real result
and it is not a general one.

### The shape this table does not contain

Hand-checked on a third repository in the same tree — 1,483 TypeScript files, a monorepo — and not
scored by this harness, so it is reported as an observation rather than a row:

`codeintel query --op callers --target StrategyChain.resolve` returns **48 direct callers and 2
usages, capped at 50 rows and therefore truncated**. In that entire repository, exactly **five files
mention `StrategyChain` at all** — its definition, a barrel re-export, a spec, and the two agents
that use it. The true production callers are two: `GeneralChatAgent.tryDeterministicPath` and
`WorkflowStepAgent.tryDeterministicPath`.

What matters is what the answer looks like underneath the headline:

* the envelope is `confidence: partial`, and its gaps say **"43 of 50 row(s) were resolved by name
  matching (`suffix_match`), not by following an import or a language-server binding"**, plus
  `row-cap-reached` and `non-call-relationships`;
* only **7 rows carry no confidence badge**, and the **first two of those are exactly the two true
  callers**. The other 43 are badged `[?0.28]` or `[?0.55]`.

So the ranking and the disclosure both work, and the headline count is still wrong by more than an
order of magnitude. An agent that reads `gaps` and prefers unbadged rows gets the right answer; one
that reads "48 callers" and starts editing does not. That gap between *what the envelope says* and
*what the first line says* is the finding, and it is the one shape this file has never scored:
**a qualified method target whose leaf name collides across a large tree.** Adding it needs a
repository whose truth is establishable, which is why it is an observation here and not a table.

**The answer now prints the command that closes it.** Establishing the paragraph above took a person
noticing that the discriminator is `StrategyChain` rather than `resolve` and grepping for it. That
derivation is mechanical — the qualifier is the part of the target the name match did not use — so
the answer emits it:

```text
_Settle it: `rg -n --fixed-strings 'StrategyChain' ~/…/bright-sky`_
```

Re-measured on the same repository: the 43 name-matched rows sit in **26 distinct files**, exactly
**5** files in the tree mention `StrategyChain` at all, and the intersection is **empty** — one shell
command eliminates all 26, leaving the two true callers among the `resolved` rows. The headline is
still 48 and the count is still the finding; what changed is that discharging the doubt is now one
command a reader is handed rather than one they have to design.

### What `corpus-ts` reports today

Re-measured 2026-09-17, after `codeintel index bench/fixtures/corpus_ts`:

| arm | direct precision | direct recall | impact precision | impact recall | wrongly silent |
|---|---|---|---|---|---|
| `graph` | 50% | 80% | 50% | 50% | 0 / 3 |
| `lsp_raw` | n/a | n/a | n/a | n/a | 0 / 0 — **3 unanswered** |
| `lsp_classified` | n/a | n/a | n/a | n/a | 0 / 0 — **3 unanswered** |

Oracle coverage 79% mean. The `graph` row is a smoke test and nothing more — 20 files written to
have a known answer cannot measure an engine — with one exception worth naming: **`describe` now
claims 1 caller where the failure that motivated this entire arm claimed 32.** One spurious against
one proven non-caller is still a spurious, but that class is no longer what it was.

The two LSP rows say `unanswered` rather than a number, and getting them to say that is the whole
story below. They read `1 / 3 wrongly silent` until the run before this one.

### The corpus has no `tsconfig.json`, and that is load-bearing in both directions

That absence is deliberate — it is what the oracle's unresolvable-specifier guard exists to bite on.
Its second effect was not designed. Without a project file, `tsserver` treats every file as its own
inferred project and cannot see across files, so it returns each definition and an **empty reference
list**. `forwardReleasedItem` is imported and called in four of the 20 files, and
`--engine lsp --op symbol` used to answer:

```text
## References (0)
(the language server reports no references to this symbol)
```

at `confidence: complete`, with no gap. On that same directory, `codeintel doctor --deep` reported
`3 / 3 engines ready`. Copy the tree, drop in a plain `tsconfig.json`, ask again: **17 references.**
Nothing else changed.

The empty list was never the language server failing. It was the language server correctly answering
a question about a project that did not exist, and every layer above it relaying that as fact —
green health check included.

Both halves are now closed, and they had to be closed separately because they are read by different
people:

**`doctor`** asks the second half of the question it was already asking. A tree that serves
`typescript` with no `tsconfig.json` anywhere reports `lsp` **not runnable** and names the symptom,
beside the existing check for a language `.serena/project.yml` never mentioned. Both answer *will it
answer for this repo's code?*, which is the question `READY` does not.

**The envelope** no longer asserts the emptiness. [`outcome.py`](../src/codeintel/outcome.py) gained
a `Missing` kind, `unresolvable`, for the state its original rule did not have a name for: `Ok([])`
means "asked, and there is nothing" only when the backend was **in a position to know**. Where it
was not, the same query now answers

```text
## References — not retrieved
> the language server returned no references, but no tsconfig.json covers the 20 TypeScript files
> in this repository — … so this is UNKNOWN rather than none. …
```

at `confidence: partial`, carrying a `references` gap of kind `unresolvable`. The doubt is scoped to
the *file the symbol was found in*, not to the repository: a polyglot tree with loose TypeScript and
no tsconfig must not cast doubt on a Python answer that resolved perfectly well, because a gap that
appears on correct answers is one nobody reads.

Two consequences visible in the numbers above, and one that is deliberately invisible:

* The scorer now excludes both arms as **unanswered** instead of charging them `1 / 3 wrongly
  silent`. That is not the benchmark going easier on the engine — it is the engine no longer making
  a claim. Scoring an honest "I cannot tell you" as a wrong answer would punish exactly the
  disclosure this project keeps asking its engines to make, and `score.py` treats `unresolvable`
  the way it already treats `not-asked`.
* `bench/run.py daycap` — a real TypeScript repository **with** a tsconfig — is byte-identical
  before and after: 100% / 100% direct, 0 / 8 wrongly silent. A fix for a false "none" that made
  correct repositories noisier would be a bad trade, and the measurement is what shows it did not.
* `## References (0)` still exists and still means what it says. It is what a correctly configured
  repository returns for a symbol nothing references, and keeping that answer at `complete` is what
  keeps `partial` worth reading.

---

# Agent-cost benchmark

Measures **tokens and tool calls to answer a real question**, per tool surface — the axis the
call-edge benchmark above cannot reach and the one this market compares on.

It exists because [`README.md`](../README.md) claims codeintel yields "fewer, sharper tool calls,
less re-reading". That is a fact asserted about the world by a project whose own recurring defect
class is *a fact asserted about the world by code that never checked it* — and the accuracy numbers
above cannot support it, however good they get. Accuracy and cost are different axes.

```bash
# Verify the harness — real loop, real tools, real scorer, scripted model. No API calls, no spend.
python bench/agent_bench.py --dry-run
python bench/agent_bench.py --dry-run --repo-key pathly-adapters

# The real thing. Needs ANTHROPIC_API_KEY (or an `ant auth login` profile) and costs money.
python bench/agent_bench.py --repo-key codeintel        --out /tmp/ci.json
python bench/agent_bench.py --repo-key pathly-adapters  --out /tmp/pa.json

# One question, one arm — the cheap way to sanity-check before committing to a full matrix.
python bench/agent_bench.py --questions q_pa_collision --arms codeintel --repo-key pathly-adapters
```

## Three arms, and what makes the comparison fair

| arm | structural tools added |
|---|---|
| `grep_only` | none — the baseline an agent falls back on |
| `codeintel` | `code_query` (this project's own envelope, as an agent receives it) |
| `raw_backend` | `search_graph`, `trace_path`, `get_code_snippet` — `codebase-memory-mcp` direct |

**Every arm keeps `grep`, `read_file` and `list_files`.** The claim under test is about what an agent
*chooses* to do when it has a structural index, not about what it can do when the alternative is
confiscated. Taking grep away from the codeintel arm would measure tool deprivation and report it as
product value. So an agent that greps anyway is charged for it, and the bias runs **against**
codeintel — the same direction `run.py`'s stratified target list takes, for the same reason.

`raw_backend` is included because it is the honest competitor: a user who installs
`codebase-memory-mcp` alone gets most of the capability table with less setup, which the project's
own status section already says. If codeintel's unification does not pay for itself against its own
backend, that is the finding.

## Cost per *correct* answer is the headline, not cost per question

A token count on its own is won by giving up early, and an arm that confidently names a docstring as
a call site scores the same as one that did the work. "57% fewer tokens" with no accuracy column is
the standard way this measurement gets faked, and it is the same hole proven negatives closed for the
call-edge benchmark.

So correctness is scored jointly with cost, and truth has two halves:

* `must_include` — every pattern must appear in the final answer.
* `must_forbid` — the trap. `q_callers_gateway` requires the two real `Gateway.query` call sites and
  **forbids `injector.py`**, whose docstring contains `code.query(op="changed")` and which a `.query(`
  grep finds. `q_pa_route_handler` forbids `CLAUDE.md` and `stop_telemetry`, three live decoys that
  mention `/runner/terminal/result` without handling it.

Scoring is regex, not an LLM judge — deliberately. A judge would add a second model's opinion to a
measurement whose whole purpose is to replace an unverified assertion, and would make the result
unreproducible without spending money. The cost is that a right answer for the wrong reason can pass,
which is why every run's full answer text is kept in the `--out` JSON for a human to read.

## Two repositories, and why neither is enough alone

| set | tree | why |
|---|---|---|
| `codeintel` | this checkout | ground truth cheap to establish — and the author picked the questions knowing the code |
| `pathly-adapters` | 3,284 files, 419 Python / 398 TSX / 343 TS | not written for this benchmark; the tree `run.py` already scores |

Report both or neither. The self-referential set alone is the weaker half, for exactly the reason the
project's own status section gives: this tool's bugs have come from repositories its author did not
write. The `pathly-adapters` questions are picked for failure modes rather than coverage — a
cross-language seam (TypeScript posting to a Python route), a value that lives in a JSON schema
rather than in code, a re-export chain, an entry point whose target is one of **fourteen** functions
named `main`, and a bare name with three separate definitions.

## `--dry-run` is a positive control, not a smoke test

It runs the real loop against the real tools and answers each question from its `canned_answer` — a
realistic correct answer stored beside the ground truth. That proves every `must_include` is
**satisfiable by prose a model would actually write**. An unescaped pattern would otherwise mark every
arm wrong on that question forever and read as a product finding rather than a typo; one such bug was
already caught this way. Run it after touching `questions.py`.

Two harness bugs found during bring-up, both of which would have faked the result, are worth naming
because they are the class to watch for:

* `grep` without `-E` uses basic regex, so the model's natural `\.query\(` died with *"parentheses not
  balanced"* — charging an arm for the harness's regex dialect.
* ripgrep's `-I` is **`--no-filename`**, not "ignore binary" as in `grep`. It silently stripped file
  paths, so `grep_only` could not have answered any question asking *which file* — scoring it near
  zero and handing codeintel a win it had not earned.

## Findings so far

**None. This has not been run against the API yet** — the harness is verified, the number does not
exist. Writing a table here before measuring is precisely the defect this benchmark was built to
correct, so this section stays empty until there is a run to report.

What a run will need stated alongside it: one model, five questions per repository, **one run each**,
so there is no variance estimate and a small spread between arms is not a result. Prompt caching is
off, so the token column is the raw quantity the claim is about rather than a cache-hit artifact.
