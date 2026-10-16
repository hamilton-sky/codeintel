# Map: dogfood-fixes

## Destination

The three defects found dogfooding 0.28.0 on daycap and feature-flow are
fixed, each pinned by a test that failed before its fix: a failed
embedding-model download names the network remedy (not the config key); a
cold `setup --all` finishes every dependency download it starts and `doctor`
says "warming" instead of blaming configuration while one is in flight; and
`callers` on a method whose callers go through a TypeScript interface lists
them under "Callers through" instead of missing them and naming the interface
declaration as a caller.

**The bar.** On a fixture repo with a TS interface plus a structural-only
implementation: `codeintel query --op callers --target Impl.write` prints
"Callers through" and no "module scope of" row for the interface file; a
simulated failed embedding download for a shipped model names
huggingface.co, not the `model` config key; `doctor --deep` on a cold repo
reports warming while the language server downloads. Ticket 07 runs all
three end to end.

## How to work this

- See what is ready: a ticket is ready when its `Status:` is `open` and every ticket in its
  `Blocked by:` line is `resolved`. Take the lowest numbered ready ticket.
  (`grep -H '^Status:\|^Blocked by:' plans/dogfood-fixes/tasks/*.md` shows all of them at once.
  feature-flow's `scripts/flow-status.py` is not vendored into this repository.)
- Claim: set `Status: claimed` in the ticket and save before starting.
- Resolve: write the result under `## Answer`, set `Status: resolved`, then add one line to
  Decisions so far below.
- See the graph: the dependency diagram in `spec.md` § Design.
- Commands live in `commands.md` (frozen). Lessons live in `learnings.md` (append only).
- Status lives only in the ticket files. This map never repeats it.

## Decisions so far

(none yet)

## Open questions

- Where this plan should live in the codeintel repo: feature-flow native is
  `plans/dogfood-fixes/` (this draft's layout). The hq-copilot precedent in
  pathly-adapters used `<area>/features/<name>/` with `FLOW_DIR` and
  `FLOW_TICKETS=tasks` since feature-flow isn't installed there either. The
  maintainer picks; the draft copies cleanly to either.
- Ticket 04's decision (source of the TS structural-implements link) needs
  the maintainer after the investigation.
