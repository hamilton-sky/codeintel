# Call-edge benchmark

Measures **who-calls-this** accuracy against labelled ground truth, so the choice between engines is
arithmetic rather than argument. It exists because every accuracy claim made about this tool — in
either direction — had rested on a handful of hand-checked symbols, and two careful readings of that
same evidence produced opposite designs.

```bash
# Against real repositories — needs a live graph backend and an indexed clone.
CODEINTEL_BENCH_PATHLY=~/src/pathly-adapters python bench/run.py pathly-adapters
CODEINTEL_BENCH_SNITCH=~/src/snitch-simulator python bench/run.py snitch-simulator

# Against the checked-in corpus — needs nothing, runs in CI, pins the oracle itself.
pytest tests/test_bench_oracle.py
```

The repository paths used to be hardcoded to one laptop, which meant the artifact that turns this
project's accuracy arguments into arithmetic could be reproduced by nobody and re-run by nobody after
a backend release. They come from the environment now, and a missing clone says so instead of scoring
every arm against an empty tree.

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

`bench/fixtures/corpus` is a checked-in micro-repository with a known answer for every site, and
`tests/test_bench_oracle.py` asserts the label of each one. It does not replace a run against real
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

Ten Python symbols in `pathly-adapters`, six in `snitch-simulator`, against
`codebase-memory-mcp 0.10.8`:

| arm | direct precision | direct recall | impact recall |
|---|---|---|---|
| `graph` | 100% | 100% | 100% (pathly) / 60% (snitch) |
| `lsp_raw` | **74%** | 100% | 100% |
| `lsp_classified` | 100% | 100% | 100% |

> **These numbers predate proven negatives and have not been re-measured since.** They were produced
> under positives-only truth, which could charge one fabrication class and not the other. Claiming an
> import or a reference as a call was charged — that is precisely what `lsp_raw`'s 74% is made of, so
> that figure means what it says. Claiming a bare name the file binds to something else was free, and
> that class is not represented in any row above. Re-run both repositories before quoting the table.

Three things worth stating plainly:

1. **`lsp_raw` is the weakest arm.** Its 74% precision is import lines and duplicate rows counted as
   callers — measured, after a hand check on one symbol had suggested 56%. "Promote the LSP to
   authority for callers" would have shipped a regression.
2. **The graph engine was exact on this population** — but read that as *on the sites the oracle then
   judged*, which by construction excluded the bare-name fabrication class. `_broadcast` and
   `_claude_tokens` are in the target list precisely because short common names are where that
   failure lives, and it was the one thing the scoring could not see. It is also a result *about
   0.10.8*, which fixed the Python attribution defect; the same measurement on 0.9.x would look
   materially worse.
3. **No TypeScript arm yet** — and the worst failure ever observed here (`describe`, 32 fabricated
   callers) is TypeScript. Nothing in this table speaks to it. This is the largest open gap.

## The TypeScript arm, and why the negative comes first

Built on positives-only truth, a TypeScript arm pointed at `describe` would have reported `n/a` or
100% — measuring nothing about the failure it exists to measure. With `not-target` in the population
it can charge that failure, which is why the label landed first.

TypeScript should end up **more** decidable than Python here, not less. Python has to abstain on an
unbound global because another module can install one; ES modules have no such escape — a
module-scope symbol in another file is reachable only through an import, and `import * as ns` binds a
namespace object, so uses stay `ns.foo` and remain readable. So "this file calls a bare `describe`,
imports no `describe`, and declares none" is a **proven negative** in TypeScript where its Python
equivalent is an abstention. Ambient `declare global`, `@types` packages and CommonJS `require` are
the parts that still need abstention. `tree-sitter-language-pack` is already a dependency and
`indexer.py` already carries the TypeScript node types, so the parser is in hand.

Also open: the oracle abstains on attribute calls on values, which is exactly where short common
names live. Proven negatives raise coverage on the shadowing cases; the attribute case is untouched
and is harder, because resolving a receiver's type means type inference — which reintroduces the
circularity the oracle exists to avoid.
