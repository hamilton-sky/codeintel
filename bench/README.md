# Call-edge benchmark

Measures **who-calls-this** accuracy against labelled ground truth, so the choice between engines is
arithmetic rather than argument. It exists because every accuracy claim made about this tool — in
either direction — had rested on a handful of hand-checked symbols, and two careful readings of that
same evidence produced opposite designs.

```bash
# Against real repositories — needs a live graph backend and an indexed clone.
CODEINTEL_BENCH_PATHLY=~/src/pathly-adapters python bench/run.py pathly-adapters
CODEINTEL_BENCH_SNITCH=~/src/snitch-simulator python bench/run.py snitch-simulator

# Against a real TypeScript repository — set the path, then list its disputed symbols in run.py.
CODEINTEL_BENCH_TS=~/src/some-app python bench/run.py typescript

# Against the checked-in corpora — needs nothing, runs in CI, pins both oracles.
pytest tests/test_bench_oracle.py tests/test_bench_oracle_ts.py
python bench/run.py corpus-ts        # the TypeScript arm end to end, on 19 known files
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
3. **The table is Python only.** The worst failure ever observed here (`describe`, 32 fabricated
   callers) is TypeScript. The arm to measure it now exists — see below — but pointing it at a real
   TypeScript repository is still open, and nothing in this table speaks to that failure.

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

`bench/fixtures/corpus_ts` is 19 files covering all of it, and `python bench/run.py corpus-ts` drives
the whole path — oracle, scorer and both engines through codeintel's own envelope. It is a **smoke
test, not a measurement**: files written to have a known answer cannot say anything about real code.
The real measurement needs a real TypeScript repository, which is the largest thing still open.

Also open: the oracle abstains on property accesses on values, which is exactly where short common
names live. Proven negatives raise coverage on the shadowing cases; the receiver case is untouched
and is harder, because resolving a receiver's type means type inference — which reintroduces the
circularity the oracle exists to avoid.
