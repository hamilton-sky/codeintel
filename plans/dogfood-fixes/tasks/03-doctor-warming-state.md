# Doctor reports "warming" while a language-server dependency is still downloading

Type: task
Status: open
Blocked by: —
Test first: yes
Review: auto

When the LSP deep verification times out (`src/codeintel/doctor.py`,
`lsp_deep_timeout_s = 20.0`) while serena is still installing a dependency —
the serena log shows the install in progress (e.g. "Installing..." /
`npm install` running for the TypeScript server), or the boot died in the
one-time uvx fetch ("unhandled errors in a TaskGroup") — `doctor` currently
blames configuration ("check `.serena/project.yml` names the right
language(s)"). The file was correct in the observed case; the true cause was
"still downloading" and the true remedy "wait ~60s and retry".

Add a "warming" outcome to the LSP probe: when the verification does not
complete but there is evidence a dependency install is in flight, report the
engine as warming — installed, not yet runnable, retry shortly — instead of
the configuration hint. When there is no such evidence, keep today's
message exactly. The three-column installed/runnable/repo-indexed shape does
not change; only the note under it does.

## Not in this ticket

- Making the warm itself finish the downloads — ticket 02. This ticket is
  the messaging for the residual case, and it owns the "warming" wording:
  define it once here so ticket 02's in-progress line reuses it.
- The semantic engine's states — unchanged.

## Done when

- New tests in `tests/test_doctor.py` fail before and pass after:
  timeout-with-install-in-progress → the note names warming and a retry,
  never the project.yml hint; timeout-with-no-install-evidence → the
  project.yml hint, as today; boot failure during the uvx fetch → warming,
  not a TaskGroup stack.
- The full suite still passes: `pytest tests/ -q` exits 0.

```check
# The conductor runs this block before review.
$ pytest tests/test_doctor.py -q
exit 0
prints passed
$ pytest tests/ -q
exit 0
prints passed
```

## Reference

- spec.md § Problem (item 2), § Decisions (second bullet)
- `src/codeintel/doctor.py` — the LSP deep probe, `lsp_deep_timeout_s`
- `tests/test_doctor.py` — existing probe tests (read-only)

## Answer

