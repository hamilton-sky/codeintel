"""Infer architectural layers from a C4 payload's element-level ``IMPORTS`` graph.

Phase 1 of [docs/layers-design.md](../../docs/layers-design.md): **inference only.** No DSL, no new
views, no exit codes, no config parsing. Two pure functions over the payload `build_c4_payload`
already produces, so the boxes a layer view would draw are the boxes the index view draws — the same
objects, re-read.

``IMPORTS`` ONLY, never the ``CALLS|USAGE`` union. Not a preference — measured. Phase 0 surveyed four
indexed repos and the union collapsed between 38.8% and 70.4% of all elements into a *single*
strongly-connected component, which is not layerable in any useful sense; ``IMPORTS`` alone stayed
between 0% and 13.8%. See §1 and the Phase 0(b) table.

**The theorem that shapes this module** (§2.3). Height-to-sink longest-path ranking gives every edge
``u -> v`` the property ``rank(u) >= rank(v) + 1``, so every edge strictly descends. Therefore
*inferred* layers can never produce a layer violation — by construction, not by luck. The only thing
inference can flag is an import cycle. That is why this phase computes no findings and gates nothing:
an empty violation report against inferred ranks is not a clean bill of health, it is the absence of
an opinion. Declared layers (Phase 2) are what can actually fail.

**Why height-to-sink and not depth-from-source.** Depth-from-source buckets by distance from an entry
point, which scatters shared leaves: a utility reached both directly from the CLI and through four
hops of provider code lands at whichever depth its longest caller chain gives it, so identical
primitives end up on different rows. Height-to-sink puts every pure leaf at rank 0 together.

**Why longest path and not shortest.** With ``1 + min``, the strictly-descending invariant fails: one
shortcut edge from a high module straight to a leaf drags that leaf up next to its own dependencies,
and the diagram then shows edges pointing sideways and upward for no architectural reason. Longest
path is the only rule that makes the picture read correctly.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# ── The two decisions this module makes that the design left explicitly open ──────────────────────
#
# (1) ISOLATED ELEMENTS ARE EXCLUDED FROM THE LAYERING, not ranked into it (§2.2 Phase 0 note).
#     Longest-path ranking gives a zero-degree element height 0, which is the BOTTOM band — read by
#     every reader as "the foundation everything rests on". Measured on brightsky-ai that put
#     `frontend/vite.config`, `eslint.config`, `backend/scripts` and `frontend/docs` — 18 of 29
#     elements, 62% — into the foundation. pathly-adapters had the same defect at 21 of 81 (25.9%).
#     Those elements are not low-level, and a rank cannot say what they are. They are reported as
#     `unassigned` instead: visible, counted, and never silently called the foundation.
#     Removing zero-degree nodes cannot cascade — it changes no other node's degree — so one pass is
#     correct and there is no fixed point to iterate to.
#
#     What `unassigned` does NOT mean is "unrelated", and the distinction cost a wrong line of output
#     before it was caught. `IMPORTS` is MODULE-LEVEL only: `src/codeintel/doctor.py` has seven
#     `codeintel` imports and every one is inside a function, so it has zero IMPORTS edges while being
#     heavily coupled. Zero degree here is a fact about the edge source, not about the architecture,
#     and anything rendering this set must say so.
#
# (2) THE DEGRADATION THRESHOLD STAYS AT 50%, AND IS A GUARD, NOT A TUNED PARAMETER (§9.3).
#     Phase 0 measured the largest real `IMPORTS` SCC at 13.8% across four repos, so this threshold
#     sits ~3.6x above anything observed and would not have fired once. The design's instruction was
#     to pick deliberately rather than leave it at 50% while describing it as tuned, so: it is
#     deliberately a catastrophe guard for a pathologically cyclic repository, and it is expected
#     never to fire on ordinary code. Lowering it into the 20-25% range real repos reach would
#     exercise it, but at the cost of stamping `degraded` on views that are still perfectly readable
#     — a worse trade than an unexercised guard, because the flag's whole value is that it means
#     something when it appears.
DEGRADED_FRACTION = 0.5

# How many cycle members to name before summarising the rest, so a 21-member SCC does not bury the
# payload. The full count is always carried alongside, so the cap truncates the display, not the fact.
CYCLE_MEMBER_CAP = 12

_EMPTY_LAYERS: dict[str, Any] = {
    "mode": "inferred",
    "ranks": {},
    "bands": [],
    "unassigned": [],
    "cycles": [],
    "degraded": False,
    "stats": {
        "elements_total": 0, "elements_ranked": 0, "elements_unassigned": 0,
        "edges_used": 0, "edges_ignored_kind": 0, "edges_self": 0,
        "condensation_nodes": 0, "cycles": 0, "largest_cycle": 0,
        "largest_cycle_fraction_ranked": 0.0, "largest_cycle_fraction_total": 0.0,
        "depth": 0,
    },
}


def empty_layers() -> dict[str, Any]:
    """A fresh zero-value layers block, for `_EMPTY` and for any early return."""
    block = dict(_EMPTY_LAYERS)
    block["stats"] = dict(_EMPTY_LAYERS["stats"])
    return block


# ── graph construction ────────────────────────────────────────────────────────────────────────────

def _imports_digraph(elements: Iterable[dict], relations: Iterable[dict]) -> dict[str, Any]:
    """The element-level ``IMPORTS`` digraph, restricted to elements the payload actually emitted.

    Endpoints not present in `elements` are dropped rather than auto-created: an id that is not an
    emitted element is out of scope, and inventing a node for it would put a box in the layer view
    that the index view does not have.
    """
    ids = sorted({str(e.get("id") or "") for e in elements} - {""})
    known = set(ids)
    succ: dict[str, set[str]] = {i: set() for i in ids}
    pred: dict[str, set[str]] = {i: set() for i in ids}
    used = ignored_kind = self_edges = 0

    for rel in relations:
        if str(rel.get("kind") or "") != "imports":
            ignored_kind += 1
            continue
        a, b = str(rel.get("from") or ""), str(rel.get("to") or "")
        if a not in known or b not in known:
            continue
        if a == b:
            # Already folded into element cohesion upstream; a self-loop is not a layer relation and
            # would make every element its own 1-member cycle.
            self_edges += 1
            continue
        if b not in succ[a]:
            succ[a].add(b)
            pred[b].add(a)
            used += 1

    return {"ids": ids, "succ": succ, "pred": pred,
            "used": used, "ignored_kind": ignored_kind, "self_edges": self_edges}


# ── Tarjan ────────────────────────────────────────────────────────────────────────────────────────

def _sccs(nodes: list[str], succ: dict[str, set[str]]) -> list[list[str]]:
    """Strongly-connected components, iteratively, in deterministic order.

    Iterative rather than recursive because the recursion depth is the graph's depth, and a payload
    is built from whatever a repository happens to contain — a module chain deeper than Python's
    recursion limit would turn a diagram into a `RecursionError`.

    Determinism is load-bearing, not tidiness: `c4`'s output is meant to be committed and diffed, so
    an unstable component order would churn the emitted model on an unchanged repo. Roots are
    iterated in sorted order, successors are visited in sorted order, and each component is returned
    sorted.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    chain: list[str] = []
    out: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        chain.append(root)
        on_stack.add(root)
        work: list[tuple[str, Any]] = [(root, iter(sorted(succ.get(root, ()))))]

        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    chain.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(succ.get(child, ())))))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue

            work.pop()
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    popped = chain.pop()
                    on_stack.discard(popped)
                    component.append(popped)
                    if popped == node:
                        break
                out.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    return out


# ── longest-path height over the condensation ─────────────────────────────────────────────────────

def _heights(comp_ids: list[int], comp_succ: dict[int, set[int]]) -> dict[int, int]:
    """``height(c) = 0`` when c has no outgoing edges, else ``1 + max(height(successors))``.

    Computed on the condensation, which is a DAG by construction, so the recursion terminates without
    a cycle guard. Iterative for the same reason `_sccs` is.
    """
    height: dict[int, int] = {}
    for root in comp_ids:
        if root in height:
            continue
        stack: list[tuple[int, bool]] = [(root, False)]
        while stack:
            comp, resolved = stack.pop()
            if resolved:
                # Every successor is below this frame on the stack and therefore already resolved.
                # `default=-1` is what makes a sink come out at 0 rather than needing a special case.
                height[comp] = 1 + max((height[s] for s in comp_succ.get(comp, ())), default=-1)
                continue
            if comp in height:
                continue
            stack.append((comp, True))
            stack.extend((nxt, False) for nxt in sorted(comp_succ.get(comp, ()))
                         if nxt not in height)
    return height


# ── public API ────────────────────────────────────────────────────────────────────────────────────

def compute_layers(elements: Iterable[dict], relations: Iterable[dict], *,
                   degraded_fraction: float = DEGRADED_FRACTION) -> dict[str, Any]:
    """Infer layer ranks from the element-level ``IMPORTS`` graph. Pure; never raises.

    Returns ``{mode, ranks, bands, unassigned, cycles, degraded, stats}``:

    * ``ranks``   — ``{element_id: rank}``. Rank 0 is the BOTTOM band (pure leaves); higher ranks sit
      closer to the entry points. Isolated elements are absent, by decision (1) at the top of this
      module.
    * ``bands``   — top-to-bottom, so ``bands[0]`` is the highest rank. That ordering matches the
      declared-config schema in §3.2, where ``order[0]`` is the top layer and imports may only point
      down; a config generated from these bands is therefore already in the right direction.
    * ``cycles``  — one entry per SCC of size > 1. Each occupies exactly one rank, because its
      members genuinely have no relative order and stacking them would invent one.
    * ``degraded`` — see decision (2). True only when one cycle swallows most of the graph.
    """
    elements = list(elements)
    relations = list(relations)
    graph = _imports_digraph(elements, relations)
    ids: list[str] = graph["ids"]
    succ: dict[str, set[str]] = graph["succ"]
    pred: dict[str, set[str]] = graph["pred"]

    block = empty_layers()
    stats = block["stats"]
    stats.update({
        "elements_total": len(ids),
        "edges_used": graph["used"],
        "edges_ignored_kind": graph["ignored_kind"],
        "edges_self": graph["self_edges"],
    })

    unassigned = sorted(i for i in ids if not succ[i] and not pred[i])
    unassigned_set = set(unassigned)
    ranked = [i for i in ids if i not in unassigned_set]
    block["unassigned"] = unassigned
    stats["elements_unassigned"] = len(unassigned)
    stats["elements_ranked"] = len(ranked)

    if not ranked:
        # Every element is isolated, or there are none. Not an error and not a failure to compute:
        # a repository whose modules do not import one another has no layering to report, and saying
        # so is the answer. `unassigned` above still names every element.
        return block

    ranked_set = set(ranked)
    ranked_succ = {i: {j for j in succ[i] if j in ranked_set} for i in ranked}

    components = _sccs(ranked, ranked_succ)
    comp_of: dict[str, int] = {node: idx for idx, comp in enumerate(components) for node in comp}
    comp_succ: dict[int, set[int]] = {idx: set() for idx in range(len(components))}
    for node in ranked:
        for nxt in ranked_succ[node]:
            if comp_of[node] != comp_of[nxt]:
                comp_succ[comp_of[node]].add(comp_of[nxt])

    height = _heights(sorted(comp_succ), comp_succ)
    ranks = {node: height[comp_of[node]] for node in ranked}
    block["ranks"] = dict(sorted(ranks.items()))

    by_rank: dict[int, list[str]] = {}
    for node, rank in ranks.items():
        by_rank.setdefault(rank, []).append(node)
    # Descending, so index 0 is the top band — see the `bands` note in the docstring.
    block["bands"] = [{"rank": rank, "elements": sorted(by_rank[rank])}
                      for rank in sorted(by_rank, reverse=True)]

    cycles = [comp for comp in components if len(comp) > 1]
    block["cycles"] = [
        {"rank": height[comp_of[comp[0]]],
         "size": len(comp),
         "members": comp[:CYCLE_MEMBER_CAP],
         "members_omitted": max(0, len(comp) - CYCLE_MEMBER_CAP)}
        for comp in cycles
    ]

    largest = max((len(c) for c in cycles), default=0)
    stats.update({
        "condensation_nodes": len(components),
        "cycles": len(cycles),
        "largest_cycle": largest,
        # Both denominators are reported. The flag uses the RANKED population, because that is the
        # set the layer view actually draws — but Phase 0's recorded table was computed against all
        # elements, so carrying that figure too keeps this comparable with the measurements that
        # calibrated the threshold, instead of silently changing the definition.
        "largest_cycle_fraction_ranked": round(largest / len(ranked), 4) if ranked else 0.0,
        "largest_cycle_fraction_total": round(largest / len(ids), 4) if ids else 0.0,
        "depth": len(block["bands"]),
    })
    block["degraded"] = bool(ranked and (largest / len(ranked)) > degraded_fraction)
    return block


def _toml_string(value: str) -> str:
    """A TOML basic string. Paths are data from an index, so they get escaped rather than trusted."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def suggest_config(ranks: dict[str, int], elements: Iterable[dict]) -> str:
    """Render inferred ranks as a pasteable ``[layers]`` TOML block.

    Layers are named ``layer_0`` … top to bottom, and each band's comment lists the modules in it so
    the names can be replaced with real ones. The generator does not invent semantic names: it can
    see that a band exists, not what it *is*, and a plausible-looking wrong name ("gateway") is worse
    than an obviously placeholder one because it reads as an opinion the tool does not hold.

    Membership is emitted as FILE PATHS, not element ids (§3.3), which is what keeps a config valid
    across a `--depth` change: element ids are a function of the roll-up depth, file paths are not.

    By §2.3 this block is a green baseline on the commit that generated it — every inferred edge
    descends, so pasting it in and running the Phase 2 check cannot fail on the same tree. That is the
    property that stops a first adoption drowning in false positives, and the header says so.
    """
    paths_of: dict[str, list[str]] = {}
    for element in elements:
        eid = str(element.get("id") or "")
        if not eid:
            continue
        raw = str(element.get("path") or "")
        # `build_c4_payload` joins the paths of a cross-language merge with ", " into one element.
        paths_of[eid] = sorted(p.strip() for p in raw.split(",") if p.strip())

    by_rank: dict[int, list[str]] = {}
    for eid, rank in ranks.items():
        by_rank.setdefault(rank, []).append(eid)
    descending = sorted(by_rank, reverse=True)

    lines: list[str] = [
        "# Generated by `codeintel c4 --suggest-config` from the inferred IMPORTS ranks.",
        "#",
        "# Layers are ordered TOP to BOTTOM: index 0 is highest, and imports may only point down.",
        "# Names are placeholders — rename them; the tool can see that a band exists, not what it is.",
        "#",
        "# On the tree that generated it this config is a GREEN baseline: inferred ranks make every",
        "# edge descend, so `--check` cannot report a violation here until the code moves. Paste it,",
        "# confirm it passes, then tighten it — that is the intended adoption path, not a starting",
        "# point you have to fight.",
    ]
    if not descending:
        lines += ["#",
                  "# No element had any IMPORTS edge, so there is nothing to lay out. An empty",
                  "# `order` is left deliberately rather than guessing at one.",
                  "", "[layers]", "order = []"]
        return "\n".join(lines) + "\n"

    names = [f"layer_{position}" for position in range(len(descending))]
    lines += [
        "",
        "[layers]",
        "order = [" + ", ".join(_toml_string(n) for n in names) + "]",
        "",
        "# Both default false. `strict_adjacent` makes skipping a layer a violation;",
        "# `require_all` makes an element matching no layer a finding.",
        "strict_adjacent  = false",
        "allow_same_layer = true",
        "require_all      = false",
        "",
        "[layers.members]",
    ]

    for name, rank in zip(names, descending, strict=True):
        members: list[str] = []
        titles: list[str] = []
        for eid in sorted(by_rank[rank]):
            members.extend(paths_of.get(eid, []))
            titles.append(eid)
        lines.append(f"# rank {rank}: {', '.join(titles)}")
        if members:
            rendered = ", ".join(_toml_string(m) for m in sorted(set(members)))
            lines.append(f"{name} = [{rendered}]")
        else:
            # An element with a rank but no recorded path cannot be matched by a glob. Emitting an
            # empty list keeps the config parseable and makes the gap visible, rather than dropping
            # the layer and silently changing `order`'s length.
            lines.append(f"{name} = []   # no file paths recorded for these elements")

    return "\n".join(lines) + "\n"
