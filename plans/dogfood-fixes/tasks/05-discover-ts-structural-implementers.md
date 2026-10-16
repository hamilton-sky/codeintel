# Discover TypeScript classes that structurally implement an interface

Type: task
Status: open
Blocked by: 04
Test first: yes
Review: auto

Build the discovery that ticket 04 settles: given a TypeScript interface,
find the classes that implement it — both explicit `implements` declarations
and structural-only matches per 04's decision. This is the lookup half of
the "Callers through" work; rendering is ticket 06.

Add the TS fixture under `tests/fixtures/ts-structural/` (new directory):
an interface `StorePort` with two methods (`read(key: string): string`,
`write(key: string, value: string): void`); one class `MemoryStore` with an
explicit `implements StorePort` clause; one structural-only class
`AtomicFileStore` with matching methods and no `implements` clause (this
mirrors the real dogfood case); one unrelated class `ConsoleLogger` with a
same-named `write(msg: string): void` but a different shape; and at least
one call site through a `StorePort`-typed reference. The fixture is
asserted on by this ticket's tests and reused by 06 and 07 — do not
hand-roll a second one.

Place the discovery next to the existing Protocol/base handling in
`src/codeintel/graph_dispatch.py` so the two structural cases share the
matching rules where they genuinely coincide and diverge explicitly where
they don't (Python Protocol vs TS interface semantics differ — say where in
a comment).

## Not in this ticket

- Rendering "Callers through" in answers — ticket 06.
- Changing what `callers` returns for the interface method itself.

## Done when

- New tests fail before and pass after: discovery returns both the explicit
  and the structural implementer for the fixture interface, returns neither
  for the unrelated class, and returns empty for an interface with no
  implementers.
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

- spec.md § Edge cases (rows 4–5), § Design
- `src/codeintel/graph_dispatch.py` — Protocol/base discovery to mirror
- Ticket 04's Answer — the settled link source (read-only)

## Answer

