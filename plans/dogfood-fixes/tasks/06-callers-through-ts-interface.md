# Render "Callers through" for TypeScript structural interfaces in callers answers

Type: task
Status: open
Blocked by: 05
Test first: yes
Review: auto

Wire ticket 05's discovery into the `callers` dispatch. When the target is a
method of a class that structurally implements a TypeScript interface,
call sites written against the interface method appear under a
"Callers through `<Iface>.<m>`" section — the same contract docs/trust.md
already documents for Protocol/base types: each row carries `via` (the
interface method actually called) and a `via_kind` for the structural link,
is never `verified`, is counted as `possible`, and its presence keeps
`confidence: partial` and `safe_for_destructive: false`.

Additionally, the interface *declaration* must no longer appear as a USAGE
"caller" of the implementation (the observed bogus row: "module scope of"
the interface file). A declaration is not a call site.

Keep the envelope honest per trust.md: these rows are candidates — "the
callers you must not change this method blind to, and nothing stronger."

## Not in this ticket

- The discovery itself — ticket 05.
- Python Protocol/base rendering — unchanged.
- `callers` on the interface method itself — unchanged behavior.

## Done when

- New tests fail before and pass after, on ticket 05's fixture: `callers`
  on the structural implementer's method lists the interface-typed call
  site under "Callers through", carries the `via`/`via_kind` fields, is not
  `verified`, and the answer is `partial` with `safe_for_destructive:
  false`; the interface declaration file appears in no caller row.
- The full suite still passes: `pytest tests/ -q` exits 0.

```check
# The conductor runs this block before review.
$ pytest tests/test_graph_provider.py -q
exit 0
prints passed
$ pytest tests/ -q
exit 0
prints passed
```

## Reference

- spec.md § Problem (item 3), § Interfaces (second bullet)
- `src/codeintel/graph_dispatch.py` — the callers dispatch and existing "Callers through" rendering
- docs/trust.md — the "Callers through" contract (read-only)
- Ticket 05's fixture under `tests/fixtures/` (read-only)

## Answer

