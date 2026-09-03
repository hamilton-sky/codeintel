# ADR 0002 — Retire the `deadcode` op

> **Status: accepted, and acted on.** The op is deleted, not flagged off: `deadcode` returns a
> safe-null with `reason: "op-withdrawn"` and no flag brings it back. This record is the
> measurement that decided it, moved out of `README.md` so the README can be an install guide
> rather than the case file for a removal. The README keeps the decision and the replacement;
> this keeps the evidence.

It was withdrawn pending one condition: *"it returns when a labelled corpus measures its
precision and recall — not before."* That corpus exists, in
[`tests/test_corpus.py`](../../tests/test_corpus.py), and the measurement is what retired it.

**How it was measured.** Two pinned real Python repositories (`pallets/click`, `psf/requests`), with
every function and method collected from the **AST** — 2,425 definitions, `async def` and class
methods included, because a verification whose population comes from a pattern like `^\s*def ` cannot
see half of them. Each is labelled live or dead with the reference behind the label recorded beside
it. The oracle errs toward *live*: a decorator, a dunder, an override of an external interface, a
string-dispatch mention, or public-API status is each enough to call a symbol live, so "dead" is only
what survives all of them. That biases the numbers against the op, which is the correct direction for
a check whose output is an instruction to delete code. Known-answer canaries are planted in both trees
so recall has a denominator at all.

**The numbers.**

| | precision | recall |
|---|---|---|
| as shipped | **25%** (6 of 24) | 60% (6 of 10) |
| with the two repairs this codebase already contains elsewhere | 89% (8 of 9) | 80% (8 of 10) |

And the measurement that decided it — **real code only, canaries removed**: the op as shipped named
**18 candidates across those two repositories, and every one of them was live.** All 18 were Makefile
targets, which the graph backend indexes as `Function` nodes. Repaired, it names exactly one, and
that one is `MockRequest.get_type` in requests — a method `http.cookiejar` calls by duck-typed
convention, whose name appears once in the source.

**Why it was not repaired further.** The verification was a name-frequency scan over the source, so it
fails on exactly one condition: a symbol whose name appears once and is called by a convention
outside the source. Two repositories produced three distinct instances of that condition — non-code
nodes labelled `Function`, interpreter-called dunders, and stdlib duck-typed protocol methods — and
the earlier TypeScript evidence adds a rollup plugin hook and object-literal properties. The set is
not enumerable: no specification lists `get_type`. Every repository added revealed a new member of it.

Weighed against that: in 2,425 real definitions across two maintained repositories there was **not
one** dead private symbol to find. An op whose measured yield on real code is zero true positives has
no benefit to set against that error rate.

**Use `callers` on a specific symbol instead.** "Does anything call this?" is exactly the question
`deadcode` was trying to answer in bulk, and `callers` answers it accurately, one symbol at a time.
