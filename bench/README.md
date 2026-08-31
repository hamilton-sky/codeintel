# Call-edge benchmark

Measures **who-calls-this** accuracy against labelled ground truth, so the choice between engines is
arithmetic rather than argument. It exists because every accuracy claim made about this tool — in
either direction — had rested on a handful of hand-checked symbols, and two careful readings of that
same evidence produced opposite designs.

```bash
python bench/run.py pathly-adapters      # needs a live graph backend + an indexed repo
python bench/run.py snitch-simulator
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

Transitive **re-exports are followed**, because `from .mod import name` is an explicit statement, and
following stated imports is what a correct resolver does — the opposite of matching bare names. That
one addition took an early run from judging 17% of a symbol's sites to 100%.

Labels are relationship **kinds**, not confidences: `call`, `reference`, `import`, `undecidable`.
Conflating those axes was the defect that motivated the whole exercise.

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

Three things worth stating plainly:

1. **`lsp_raw` is the weakest arm.** Its 74% precision is import lines and duplicate rows counted as
   callers — measured, after a hand check on one symbol had suggested 56%. "Promote the LSP to
   authority for callers" would have shipped a regression.
2. **The graph engine is exact on this population.** That is a result *about 0.10.8*, which fixed the
   Python attribution defect; the same measurement on 0.9.x would look materially worse.
3. **No TypeScript arm yet** — and the worst failure ever observed here (`describe`, 32 fabricated
   callers) is TypeScript. Nothing in this table speaks to it. This is the largest open gap.

Also open: the oracle abstains on 24–52% of sites, concentrated in attribute calls on values, which
is exactly where short common names live. Raising coverage there is what would make the numbers
carry real weight.
