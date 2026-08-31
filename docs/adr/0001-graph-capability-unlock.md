# ADR 0001 — Graph Capability Unlock

> **Status: accepted, and partly superseded.** The decision — discrete curated ops over a generic passthrough — stands, and Option A is what the code does. Three details in it no longer describe the code:
>
> * **`deadcode` was retired.** The ADR shipped it; a labelled corpus later measured it at **25% precision as shipped**, and at **zero true positives on real code** once the planted canaries were removed. The op name survives only to explain itself, returning `reason: "op-withdrawn"`. `callers` on a specific symbol answers the same question accurately and is the documented substitute.
> * **`hotspots` was withdrawn too, and then reinstated.** Not in the ADR at all, because it happened after. Its rankings came back 100% `.tsx` on two repositories that are two-thirds Python — caused by two request bugs, not a missing metric: it asked only for `Function` nodes (hiding 2,381 methods on one repo) and capped candidates at 200 rows in *name* order, so the client-side sort ranked an alphabetical 4% slice. Both fixed and pinned by `test_hotspots_ranks_across_languages`.
> * **`chain`'s hop `risk` labels became resolution evidence.** `risk` restated hop distance, which every row already prints.
>
> The "Neutral" note below calls `Method`-node coverage a documented follow-up. It shipped — as the fix that reinstated `hotspots`. See [../graph.md](../graph.md).

**Status:** Accepted — shipped in v0.9.0
**Date:** 2026-08-14
**Deciders:** codeintel maintainers
**Related:** `docs/graph.md`, `src/codeintel/providers/graph.py` (`_dispatch`), `CHANGELOG.md` (0.9.0)

---

## Context

`GraphProvider` wraps the external `codebase-memory-mcp` backend but exposes only a thin
subset of it. `_dispatch` (graph.py ~line 426) supports exactly **6 ops** — `impact`,
`callers`, `callees`, `chain`, `pattern`, `overview` — all routed through the single
`self._run(method, payload, timeout_ms)` subprocess seam and rendered to bounded markdown
under a strict **never-raise / safe-null** contract (every failure returns a safe-null
envelope; no exception ever reaches the agent).

Dogfooding the live backend (and re-verifying via the backend's own MCP interface against
the indexed `codeintel` project) confirmed three **high-value, already-supported** backend
capabilities that codeintel does not expose:

1. **`detect_changes`** — maps the repo's current git diff to impacted symbols.
   Payload `{"project": <name>}` → `{"changed_files":[…],"changed_count":N,
   "impacted_symbols":[…],"depth":2}`. This is the highest-value one: it tells an agent,
   *before it edits*, what its uncommitted changes ripple into. (The backend reindexer
   already calls this method for its own side effect — see graph.py `_graph_reindex`.)
2. **`search_graph` with degree filters + complexity metrics** — one method powering two
   views: **dead-code candidates** (`label:"Function", max_degree:0,
   exclude_entry_points:true`) and **complexity / fan-in hotspots** (`min_degree` high,
   sort by complexity). Real envelope: `{"total":N,"results":[{…per-symbol metrics…}],
   "has_more":bool}` — each result carries `in_degree, out_degree, complexity, cognitive,
   loop_depth, param_count, is_exported, is_test, is_entry_point, lines, signature,
   file_path, qualified_name`.
3. **`trace_path` with `risk_labels:true`** — the existing `chain` op already calls
   `trace_path`; adding the flag returns risk-classified hops (each hop dict gains
   `"risk":"CRITICAL|HIGH|…"`). Purely additive to the existing response shape.

The design question is **how** to expose these while preserving the never-raise contract,
the bounded-markdown output discipline, per-op RBAC granularity, and — above all —
*guessability* (an agent should be able to guess the right op name).

```
  code.query {op, target, project_root, engine, role}
        │
        ▼
  Gateway.query ── RBAC ── maybe_reindex ── cache ── _dispatch_single
        │
        ▼
  GraphProvider.build_result ──► _dispatch(op, …) ──► _op_<name>()  ◄── change site
        │                                                   │
        └──────────── safe_null_result on None ◄────────────┘
```

The change site is intentionally narrow: `_dispatch` is the only place new ops are wired;
`build_result` wraps it and guarantees the contract regardless of what an `_op_*` does.

---

## Decision

Adopt **Option A** — add **discrete, curated ops**:

| new op | backend method | intent |
|---|---|---|
| `changed` | `detect_changes` | impact of the repo's uncommitted git changes (no `target`) |
| `deadcode` | `search_graph` (`max_degree:0`) | unreferenced non-test symbols |
| `hotspots` | `search_graph` (`min_degree` high) | highest fan-in / complexity symbols |
| `chain` (enriched) | `trace_path` (`risk_labels:true`) | existing op, hops now carry a risk badge |

Each is a thin `_op_*` behind the existing `_dispatch` seam, renders bounded markdown in
the style of the existing `_op_*` methods (`## Header (N)` + `- item` lines), returns
`None` **only** when the backend call fails or returns a malformed shape (→ safe-null
upstream), and never raises. `deadcode` and `hotspots` share one `search_graph` helper
(two faces of the same backend method), mirroring how `_op_impact` composes
`_op_callers` + `_op_callees`.

Reject Option B (generic passthrough) and Option C (do nothing).

---

## Options Considered

| Option | Impl complexity | Ongoing cost | Agent value | Contract fit |
|---|---|---|---|---|
| **A — discrete curated ops** | Low–Med: 4 `_op_*` + 1 shared helper, all behind existing seam | Low: one small `_op_*` + renderer per future capability | **High**: guessable op names, bounded output, per-op RBAC | **Preserves** never-raise + bounded-markdown |
| **B — generic `graph` passthrough** (`{op:"graph", method, payload}`) | Low: one op | **Med–High**: unbounded raw JSON dumped into agent context; support burden | Med: powerful but **unguessable** (agent must know backend method names + payload schemas) | **Breaks** bounded-render; collapses per-op RBAC to a single scope |
| **C — do nothing** | None | None | **Zero**: verified capabilities stay buried; agents keep grepping | N/A |

**Option A — discrete curated ops.** Each capability becomes a named op with a bounded
renderer and tests. Cost is a small `_op_*` per capability and a one-line tool-description
update. It is the *deepest correct altitude*: the same `_dispatch` → `_op_*` → `_run`
pattern every existing op already uses, so `build_result`'s contract wrapper covers the
new ops for free, `_AUTO_ENGINE`'s default already routes unknown ops to graph, and RBAC
remains per-op.

**Option B — generic passthrough.** One op forwards an arbitrary `{method, payload}` to
the backend CLI. Attractive for future-proofing, but it optimizes the wrong axis:
- It **breaks bounded rendering** — the backend returns arbitrary, potentially large JSON
  (a `search_graph` with a loose filter can return hundreds of rich-metric rows); dumping
  that raw into an agent's context is exactly what codeintel's curated markdown exists to
  prevent.
- It **collapses RBAC granularity** — one op name (`graph`) would gate *every* backend
  method behind a single scope; an operator can no longer allow `callers` but deny a
  heavier scan.
- It **leaks the backend surface** — codeintel's value is a curated, stable, guessable
  facade; a passthrough turns it into a thin proxy whose contract is the backend's raw
  CLI, and whose op the agent cannot guess without out-of-band schema knowledge.

**Option C — do nothing.** The backend already supports these; not exposing them wastes
verified capability. `changed` in particular is a flagship *pre-edit safety* feature with
no current equivalent. Rejected.

---

## Trade-off Analysis

- **Guessability vs. extensibility.** A wins guessability (`deadcode`, `hotspots`,
  `changed` are self-describing); B wins raw extensibility. codeintel's thesis is that a
  *curated, guessable* surface beats a powerful-but-opaque one — an agent adopts
  `code.query` only when it can guess the op. A is aligned; B is not.
- **Bounded output is a contract, not a nicety.** Every existing `_op_*` caps its output
  (`LIMIT 50`, capped lists). A keeps that discipline per op; B cannot without
  re-implementing per-method rendering — at which point it *is* A.
- **`changed` is not a pure read.** `detect_changes` drives the backend's own reindex of
  changed files (the reindexer uses it for exactly that), so it can be slower than a
  symbol lookup and its answer depends on the **live git worktree** — state the
  content-hash cache key does not capture. A can handle this precisely (higher timeout
  floor; opt out of caching for this one op). B would cache/mis-time it blindly.
- **Dead-code over Python is test-dominated.** Verified: pytest test functions come back
  with `in_degree:0` **and** `is_test:false`, so `max_degree:0` alone returns almost
  entirely live tests. A can apply a client-side test-path filter in the renderer; a raw
  passthrough (B) hands the agent the noise.

---

## Consequences

**Positive**
- Three high-value capabilities exposed with zero change to the never-raise contract;
  `build_result`, `_dispatch`'s wrapper, and `_run` are untouched in behavior.
- `changed` gives agents a *before-you-edit* impact check with no current equivalent.
- `deadcode` / `hotspots` share one backend method and one helper — low marginal cost.
- `chain` gains risk context additively (no new op, no shape break).
- New ops auto-route to the graph engine for free (`_AUTO_ENGINE` default is `graph`).

**Negative / cost**
- One small cross-cutting change **outside** graph.py: the gateway must **not cache**
  `changed` (its input is the live worktree, invisible to the content-hash key). This is a
  minimal, principled `_UNCACHED_OPS = {"changed"}` guard; `deadcode`/`hotspots` remain
  cached (pure functions of the index, correctly keyed by the freshness generation).
- Renderers must carry a little domain logic (client-side test/builtin filtering,
  client-side sort for hotspots because `search_graph` does not sort). Kept inside the
  `_op_*` methods, bounded and never-raising.
- Tool description, `docs/graph.md`, and the "six ops" wording must be updated so agents
  discover the new ops.

**Neutral**
- RBAC: new ops are allowed by default; an operator running a *restricted* role must add
  the new op names to that role's allowlist to grant them (standard, no code change).
- v1 scopes `deadcode`/`hotspots` to `Function` nodes (matches the dogfooded payload);
  `Method` coverage is a documented follow-up (a second `search_graph` label pass).

---

## Outcome

The action items this ADR carried were completed in v0.9.0; what follows is where each landed and
what has happened to it since, which is the part still worth reading.

| Planned | Where it is now |
|---|---|
| `_op_changed` | `providers/graph.py`. Shipped and live. |
| `_op_hotspots` | `providers/graph.py`. Shipped, later withdrawn, **reinstated** once it asked for `Method` nodes and stopped ranking an alphabetical slice. |
| `_op_deadcode` | **Gone.** The implementation was removed; `"deadcode"` remains in `_GRAPH_OPS` and `_ROOT_SCOPED_OPS` only so the name still explains itself and cannot be silently un-scoped if something later takes it. |
| `_search_symbols` helper | `providers/graph.py`. Shipped, and still shared. |
| `_UNCACHED_OPS` in `gateway.py` | Shipped — and grown to `frozenset({"changed", "changes"})`, the ADR having specified only `changed`. |
| `chain` + `risk_labels:true` | Shipped, then replaced: hops carry resolution **evidence**, not a risk badge. |
| Tool description, tests, `docs/graph.md` | All done. `graph.md` is the current reference and wins wherever it disagrees with this record. |

One consequence the ADR predicted correctly is worth naming, because it is the reason two of these
ops did not survive contact: it listed "renderers must carry a little domain logic … client-side
test/builtin filtering, client-side sort for hotspots because `search_graph` does not sort" under
**cost**. That client-side sort over an unsorted, capped candidate list is exactly what made
`hotspots` rank an alphabetical 4% slice. The cost was real and was priced too low.
