# Acceptance: the three dogfood defects stay fixed end to end

Type: task
Status: open
Blocked by: 01, 02, 03, 06
Test first: no
Review: full

Run the spec's bar end to end. No new logic in this ticket — it proves the
earlier tickets hold together on real commands. `Review: full` because it is
the last line of defense for the whole feature.

## Not in this ticket

- New behavior of any kind. A failure here reopens the ticket that owns it.

## Done when

- On ticket 05's TS fixture: `codeintel query --op callers --target
  AtomicFileStore.write --project-root tests/fixtures/ts-structural`
  prints "Callers through" and prints no "module scope of" row for the
  interface file.
- A simulated failed embedding-model download for a shipped model name
  produces the download/network remedy (names huggingface.co), never the
  "not a model fastembed ships" config remedy — covered by ticket 01's
  tests, re-run here.
- `codeintel doctor --deep` on a cold repo reports the LSP engine warming
  while the language server is still downloading, never the project.yml
  misconfiguration hint — covered by ticket 03's tests, re-run here.
- The full suite still passes: `pytest tests/ -q` exits 0.

```check
# The conductor runs this block before review.
$ codeintel query --op callers --target AtomicFileStore.write --project-root tests/fixtures/ts-structural
prints Callers through
$ pytest tests/test_index_failure_reason.py tests/test_doctor.py tests/test_graph_provider.py -q
exit 0
prints passed
$ pytest tests/ -q
exit 0
prints passed
```

(Names are prescribed by ticket 05; the resolver confirms the built
fixture uses them before running the check.)

## Reference

- spec.md § Goal and the bar
- map.md § Destination — the bar restated

## Answer

