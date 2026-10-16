# The setup warm finishes the dependency downloads it starts

Type: task
Status: open
Blocked by: —
Test first: yes
Review: auto

`codeintel setup --all` / `--warm` (`src/codeintel/onboarding.py`,
`warm_lsp`, `lsp_warm_timeout_s = 90.0`) starts downloads it does not finish:
serena's one-time uvx install (72 packages) and the per-language language
server fetch (e.g. the TypeScript server's `npm install typescript@5.9.3`)
both outlasted the warm/boot budgets in a real cold run, so the first
`doctor --deep` failed and the user retried by hand until the downloads
landed.

Stage the warm so each download runs to completion: (1) uvx/serena fetch,
(2) serena boot, (3) one language-server start per language named in the
repo's `.serena/project.yml`, each with its own progress line and a budget
that covers a cold download rather than a warm boot. While any stage is
still running the warm reports "warming", not failure. Do not just raise
the single timeout — that moves the cliff instead of removing it.

The warm must stay idempotent and must not change what a warm repo does
today (stages that find their dependency already present skip silently and
fast).

## Not in this ticket

- What `doctor` prints while a download is still in flight — ticket 03.
- The semantic model download path — ticket 01.

## Done when

- New tests in `tests/test_onboarding.py` fail before and pass after: with
  the stage commands stubbed, the warm invokes all three stages in order and
  reports "warming" (not failure) while a stage is still running; with
  everything present it skips each stage and finishes fast.
- On a real cold repo the e2e holds: `codeintel setup --all <repo>` finishes
  with the language server installed, and the immediately following
  `codeintel doctor --deep <repo>` reports the LSP engine runnable with no
  manual retry. (Needs network; prose for the reviewer, not the check
  block.)
- The full suite still passes: `pytest tests/ -q` exits 0.

```check
# The conductor runs this block before review.
$ pytest tests/test_onboarding.py -q
exit 0
prints passed
$ pytest tests/ -q
exit 0
prints passed
```

## Reference

- spec.md § Problem (item 2), § Decisions (second bullet), § Risks (second bullet)
- `src/codeintel/onboarding.py` — `warm_lsp`, the staged warm block
- `tests/test_onboarding.py` — existing warm tests (read-only)

## Answer

