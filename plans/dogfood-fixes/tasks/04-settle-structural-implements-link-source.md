# Settle: where the TypeScript structural-implements link comes from

Type: settle
Status: open
Blocked by: —
Test first: no
Review: auto
Check: none — the deliverable is the decision recorded in the Answer.

`callers` on a method whose callers go through a TypeScript interface misses
them when the implementing class never declares `implements` (structural
typing — the daycap case: `AtomicFileStore` vs the `StorePort` interface).
The graph has no INHERITS edge to join there, exactly like the Python
Protocol case `src/codeintel/graph_dispatch.py` already handles by matching
method names ("it is structural typing, so the graph has no INHERITS edge to
join").

Decide the source of the interface→implementation link that ticket 05 will
build on. Two options:

- **A — codeintel-side structural matching.** Mirror the Protocol handling:
  a class counts as implementing an interface when it defines the
  interface's methods by name (signatures where the backend records them).
  No backend change; weaker signal, fully in our control.
- **B — backend IMPLEMENTS edges.** Ask `codebase-memory-mcp` for what it
  records for a fixture with (i) an interface, (ii) a class with an explicit
  `implements` clause, (iii) a structural-only class. If it records a usable
  edge, use it; if not, fall back to A.

Investigate against the real backend on a small TS fixture (add it under
`tests/fixtures/` if none exists — it becomes 05's fixture). Report what the
index actually contains for the three shapes. The maintainer decides; the
Answer records the decision and why, and names what 05 must then implement.
If the answer is "A with B where available", say exactly when each applies.

## Not in this ticket

- Building the discovery (05) or the rendering (06) — this ticket only
  decides their input.

## Done when

- The Answer names the chosen source (A, B, or a precise hybrid), the
  evidence from the fixture that supports it, and what 05 must implement.
  A human approves the Answer.

## Reference

- spec.md § Problem (item 3), § Design (Decisions, first bullet), § Risks (first bullet)
- `src/codeintel/graph_dispatch.py` — the Protocol structural handling and its comment
- docs/trust.md — the "Callers through" contract this must extend, not break

## Answer

