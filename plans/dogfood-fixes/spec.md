# Dogfood fixes — Spec

## Problem

Pointing codeintel 0.28.0 at two unfamiliar repositories (daycap, TypeScript;
feature-flow, Python) surfaced three defects, each reproduced by hand:

1. **A failed embedding-model download is misdiagnosed as a config error.**
   fastembed 0.9.0 raises `ValueError("Could not load model ... from any
   source")` after its download retries are exhausted, and `load_embedder`
   maps every `ValueError` to "that is not a model fastembed ships — check the
   `model` key in your codeintel config". `BAAI/bge-small-en-v1.5` *is* a
   model fastembed ships (it is in `list_supported_models()`); the failure was
   the network. The user is sent to debug config for a network problem.
2. **Cold-start dependency downloads exceed the boot/verify budgets, and the
   errors blame configuration.** In one session: serena's one-time uvx install
   killed the first boot ("unhandled errors in a TaskGroup"); the TypeScript
   language server's `npm install` outlasted `doctor --deep`'s 20s
   verification, whose fix hint said "check `.serena/project.yml` names the
   right language(s)" — the file was correct, it just needed another minute.
   Everything worked on retry. First-run failure that resolves by waiting is
   the worst kind of onboarding bug.
3. **TypeScript structural interfaces are a recall hole for `callers`.**
   `callers AtomicFileStore.write` missed both real call sites (they call
   through the `StorePort` interface; the class never declares `implements` —
   structural typing) and listed the interface *declaration* as a USAGE
   "caller". No "Callers through" section appeared. Ports-and-adapters is the
   dominant TypeScript architecture style; being precisely wrong on its most
   common shape blocks adoption. The envelope stayed honest
   (`partial`/`advisory`, `safe_for_destructive: false`) — the machinery
   worked, the recall didn't.

## Goal and the bar

A stranger's first hour with codeintel on a TypeScript repository ends with
green engines and trustworthy `callers` answers instead of two rounds of
"retry and it works", and a failed model download tells the truth about what
failed.

**The bar.** On a fixture repo with a TS interface plus a structural-only
implementation: `codeintel query --op callers --target Impl.write` lists the
interface call sites under "Callers through" and no bogus USAGE row; a
simulated failed embedding download names the network remedy, not the config
key; `doctor --deep` on a cold repo reports "warming", not misconfiguration.
Ticket 07 runs all three.

## Scope

In:
- The `ValueError` classification in `load_embedder`
  (`src/codeintel/semantic_db.py`), with a failing-first test.
- Warming: `setup --all`/`--warm` (`src/codeintel/onboarding.py`) finishing
  the serena/uvx and per-language LS downloads it starts, and `doctor`
  (`src/codeintel/doctor.py`) reporting a "warming" state while a dependency
  install is in flight.
- Structural implementer discovery for TypeScript interfaces and its use in
  the `callers` dispatch (`src/codeintel/graph_dispatch.py`), following the
  existing Python Protocol handling.

Not in scope:
- Release hygiene from the same review round (tag 0.28.0, PyPI publish, the
  three stale README sentences, benchmark gate in the nightly corpus job) —
  human chores, minutes each, not ticket work.
- Python Protocol behavior — unchanged; it is the model the TS work follows.
- Any change to the safe-null / never-raise contract.

## Happy path

1. Maintainer runs the plan's tickets in order → each lands with its tests.
2. A new user runs `codeintel setup --all <ts-repo>` → all downloads finish
   inside setup → `codeintel doctor --deep` is green on the first try.
3. The user asks `codeintel query --op callers --target Impl.write` → the
   answer lists real call sites under "Callers through `<Iface>.write`",
   marks them candidates (not evidence), and names nothing bogus.

## Edge cases

| Trigger | Expected behaviour | Handled in ticket |
|---|---|---|
| `ValueError` for a model name fastembed never shipped | Config remedy, as today | 01 |
| `ValueError` after exhausted retries for a shipped model | Network/download remedy | 01 |
| LS download already finished when doctor runs | Normal green path, no "warming" | 03 |
| Interface with zero structural implementers | No "Callers through" section, no behavior change | 05 |
| Class declares `implements` explicitly | Found the same way as structural matches | 05 |
| `callers` on the interface method itself (not the impl) | Unchanged behavior | 06 |

## Design

Three independent repairs plus one decision, then the TS recall work built on
that decision:

```
[01 semantic error classification] ─┐
[02 warm finishes downloads] ───────┼──► [07 acceptance]
[03 doctor warming state] ──────────┘         ▲
[04 settle: link source] ──► [05 discover TS structural implementers]
                                 ──► [06 callers-through rendering] ──┘
```

Tickets 01–03 touch three different files and share nothing; 04–06 are the
only chain. 07 is blocked by 01, 02, 03, 06.

### Decisions

- **Where the TS interface→implementation link comes from** — options: (a)
  codeintel-side structural matching by method names, mirroring the existing
  Python Protocol handling in `graph_dispatch.py`; (b) IMPLEMENTS edges read
  from the `codebase-memory-mcp` backend. Chosen: undecided — ticket 04
  investigates what the backend actually records and the maintainer decides.
  Why not decide here: the backend is an external binary; only a fixture
  against the real index answers it.
- **"Warming" is a doctor state, not a setup flag** — options: (a) longer
  timeouts everywhere; (b) a distinct state while a dependency install is in
  flight. Chosen: B. Why: longer timeouts just move the cliff; a named state
  tells the user the true cause and the true remedy (wait, then retry).
- **Misclassified download errors keep the old message for truly unknown
  models** — the config remedy stays for names outside
  `list_supported_models()`; only the known-name case changes. Why: the
  original classification was written for exactly that case and is correct
  there.

## Interfaces

- `doctor` gains a "warming" row state for the LSP engine (text only; the
  three-column installed/runnable/repo-indexed shape is unchanged).
- `callers` answers gain a "Callers through `<Iface>.<m>`" section for TS
  structural interfaces, with a new `via_kind` value; rows are candidates,
  never `verified`, and their presence keeps `safe_for_destructive: false`
  (same rules as the existing Protocol/base sections).
- No CLI flag changes. No envelope field changes.

## Risks

- **Backend edge vocabulary is external.** If `codebase-memory-mcp` records
  no IMPLEMENTS edge and no usable structural signal, ticket 04 may conclude
  the link must be codeintel-side name matching — which is weaker. Mitigation:
  04 is a settle ticket; the decision (and its weakness) is explicit before
  05 is built.
- **Warm-stage budgets are guesses.** Download times vary by network; a fixed
  budget reintroduces the cliff. Mitigation: 02 stages the warm and reports
  per-stage progress; 03 makes the residual case ("still downloading") a
  first-class message instead of a failure.
- **New fixture trees can rot.** Mitigation: fixtures are asserted on by
  tests in the same tickets that add them (05, 06), not by hand.
