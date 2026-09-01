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


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Phase 2 — DECLARED layers: config, membership matching, and the element→layer assignment.
#
# Inference above answers "what shape is this repo". Everything below answers "does it match the
# shape someone declared", which is the half that can actually fail a build (§2.3). The two are kept
# in one module because they share the element/relation vocabulary, and deliberately kept in separate
# functions because only the declared half is allowed to gate.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

# Defaults for the three switches. Note the design's §3.2 comment says "both default false" while
# listing three switches, one of which its own example sets true — §5.2's table and its reasoning are
# the authority, and they are unambiguous: same-layer imports are normal (so `allow_same_layer` is
# TRUE by default) and strict adjacency produces a wall of findings on real code (so
# `strict_adjacent` is FALSE). Recorded because the next reader will hit the same inconsistency.
_SWITCH_DEFAULTS: dict[str, bool] = {
    "strict_adjacent": False,
    "allow_same_layer": True,
    "require_all": False,
}


def _split_segments(value: str) -> list[str]:
    return [seg for seg in value.replace("\\", "/").strip("/").split("/") if seg]


def glob_match(pattern: str, path: str) -> bool:
    """Does a repo-relative path match a layer-membership glob?

    ``*`` matches within ONE path segment; ``**`` matches across segments.

    **`fnmatch` on the whole path would be wrong**, and the design names this trap explicitly:
    `fnmatch`'s ``*`` crosses ``/``, so ``src/*.py`` would match ``src/a/b/c.py`` and silently widen
    every pattern an author writes — a check that quietly covers more than it says. `fnmatch` is used
    here only ONE SEGMENT AT A TIME, where it is exactly right: a segment contains no ``/``, so there
    is nothing for ``*`` to cross, and character classes like ``[0-9]`` come for free.

    `pathlib.PurePath.match` is also not a substitute: its ``**`` handling only became correct in
    3.13 and this package supports 3.11.
    """
    from fnmatch import fnmatchcase

    pats = _split_segments(pattern)
    segs = _split_segments(path)
    # (pattern index, path index) -> reachable. Memoised because `**` branches.
    seen: set[tuple[int, int]] = set()

    def walk(i: int, j: int) -> bool:
        if (i, j) in seen:
            return False
        seen.add((i, j))
        if i == len(pats):
            return j == len(segs)
        if pats[i] == "**":
            # Zero or more segments. Trying j..len inclusive is what makes `a/**/b` match `a/b`.
            return any(walk(i + 1, k) for k in range(j, len(segs) + 1))
        if j >= len(segs):
            return False
        if not fnmatchcase(segs[j], pats[i]):
            return False
        return walk(i + 1, j + 1)

    return walk(0, 0)


def _specificity(pattern: str) -> tuple[int, int]:
    """How specific a pattern is: (literal segments before the first wildcard, pattern length).

    Most-specific-wins is what lets ``core = ["src/codeintel/*.py"]`` act as a catch-all while
    ``gateway = ["src/codeintel/gateway.py"]`` still claims its own file, with no obligation on the
    author to list specific patterns before general ones. First-match-in-order would also work, but
    only if authors happen to get the ordering right, and getting it wrong is invisible.
    """
    literal = 0
    for seg in _split_segments(pattern):
        if any(ch in seg for ch in "*?["):
            break
        literal += 1
    return (literal, len(pattern))


def parse_layers_config(config: Any) -> dict[str, Any]:
    """Validate a ``[layers]`` block out of a loaded ``.codeintel.toml``. Never raises.

    `config.py` already preserves unknown keys verbatim, so a `[layers]` table reaches us with zero
    changes there — but it arrives UNVALIDATED, because `_DEFAULTS`/`_ENUMS` are scalar-shaped and a
    nested table does not fit that model. So validation lives here, under the same degrade-and-report
    contract as the rest of this module: a malformed block yields a named `problem`, never a
    traceback and never a silently empty check.

    Returns ``{present, order, members, switches, allow, shorthand, problem}``. `shorthand` is true
    when `order` was given without `[layers.members]` (§3.6) — supported for looking, and refused for
    gating, because failing someone's build on a guess about which layer a file belongs to is the
    worst thing this feature could do.
    """
    out: dict[str, Any] = {
        "present": False, "order": [], "members": {}, "allow": [],
        "switches": dict(_SWITCH_DEFAULTS), "shorthand": False, "problem": "",
    }
    if not isinstance(config, dict):
        return out
    block = config.get("layers")
    if block is None:
        return out
    out["present"] = True
    if not isinstance(block, dict):
        out["problem"] = "layers-not-a-table"
        return out

    order = block.get("order")
    if not isinstance(order, list) or not order:
        out["problem"] = "layers-order-missing"
        return out
    if not all(isinstance(name, str) and name.strip() for name in order):
        out["problem"] = "layers-order-not-strings"
        return out
    names = [str(name).strip() for name in order]
    if len(set(names)) != len(names):
        out["problem"] = "layers-order-duplicate"
        return out
    out["order"] = names

    for key, default in _SWITCH_DEFAULTS.items():
        raw = block.get(key, default)
        if not isinstance(raw, bool):
            out["problem"] = f"layers-{key.replace('_', '-')}-not-a-bool"
            return out
        out["switches"][key] = raw

    members = block.get("members")
    if members is None:
        # §3.6: an order with no membership. Kept, marked, and barred from gating.
        out["shorthand"] = True
        out["members"] = {name: [f"**/{name}/**"] for name in names}
        return out
    if not isinstance(members, dict):
        out["problem"] = "layers-members-not-a-table"
        return out
    unknown = sorted(set(members) - set(names))
    if unknown:
        # A layer with members but no place in `order` has no rank, so an edge touching it could not
        # be judged. Silently ignoring it would mean the config covers less than it appears to.
        out["problem"] = f"layers-members-not-in-order:{','.join(unknown)}"
        return out
    parsed: dict[str, list[str]] = {}
    for name in names:
        patterns = members.get(name, [])
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            out["problem"] = f"layers-members-not-patterns:{name}"
            return out
        parsed[name] = [p for p in (str(p).strip() for p in patterns) if p]
    out["members"] = parsed

    allow = block.get("allow", [])
    if allow is None:
        allow = []
    if not isinstance(allow, list):
        out["problem"] = "layers-allow-not-a-list"
        return out
    entries: list[dict[str, Any]] = []
    for position, raw in enumerate(allow):
        if not isinstance(raw, dict):
            out["problem"] = f"layers-allow-entry-not-a-table:{position}"
            return out
        # A missing `reason` is NOT a parse error — it is a gating FINDING (`allow-no-reason`), which
        # is the single rule separating an allowlist from a mute button. Rejecting the config here
        # would deny the check the chance to report it.
        entries.append({
            "index": position,
            "from": str(raw.get("from") or "").strip(),
            "to": str(raw.get("to") or "").strip(),
            "reason": str(raw.get("reason") or "").strip(),
        })
    out["allow"] = entries
    return out


def assign_declared_layers(elements: Iterable[dict], parsed: dict[str, Any]) -> dict[str, Any]:
    """Map each element onto a declared layer by matching its FILE PATHS against the config globs.

    Paths, not element ids (§3.3) — the single most consequential schema decision. Element ids are a
    function of `--depth`, which auto-fits and moves the moment a repo crosses the element cap. A
    config keyed on ids would silently stop matching as a repo grew, and the failure mode is the worst
    available: the check keeps passing while covering less and less.

    Returns ``{layer_of, index_of, splits, ambiguous, unassigned}``.

    An element whose files land in different layers takes the **HIGHEST** of them, not the majority
    one, and gets a `split` record. Layering constrains what may depend on what, so a container
    holding one `cli` file must be treated as `cli` for direction purposes or edges into it are
    wrongly judged fine. When a check has to guess, it guesses toward reporting more.
    """
    order: list[str] = list(parsed.get("order") or [])
    members: dict[str, list[str]] = dict(parsed.get("members") or {})
    rank_of_layer = {name: position for position, name in enumerate(order)}

    layer_of: dict[str, str] = {}
    splits: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unassigned: list[str] = []

    for element in elements:
        eid = str(element.get("id") or "")
        if not eid:
            continue
        paths = [p.strip() for p in str(element.get("path") or "").split(",") if p.strip()]
        per_path_layer: dict[str, str] = {}
        for path in paths:
            # Every (layer, pattern) that claims this path, so the winner and the ties are both
            # visible. Collecting first and deciding after is what keeps the tie DETECTION separate
            # from the tie BREAKING — an earlier version interleaved them and could only see a tie
            # against the current best, missing one that arrived before it.
            claims = [(_specificity(pattern), name, pattern)
                      for name in order
                      for pattern in members.get(name, [])
                      if glob_match(pattern, path)]
            if not claims:
                continue
            top = max(spec for spec, _, _ in claims)
            contenders = [(name, pattern) for spec, name, pattern in claims if spec == top]
            # Most specific wins; among equals the layer earliest in `order` wins, which is the only
            # tie-break left. The design's literal "all three specificity keys equal" is unreachable,
            # because position in `order` always differs when the layers do — so what actually
            # deserves reporting is a tie broken ARBITRARILY: two equally specific patterns from
            # DIFFERENT layers, resolved by declaration order alone.
            contenders.sort(key=lambda pair: rank_of_layer[pair[0]])
            winner_layer, winner_pattern = contenders[0]
            for other_layer, other_pattern in contenders[1:]:
                if other_layer != winner_layer:
                    ambiguous.append({"path": path,
                                      "pattern_a": winner_pattern, "layer_a": winner_layer,
                                      "pattern_b": other_pattern, "layer_b": other_layer})
            per_path_layer[path] = winner_layer

        if not per_path_layer:
            unassigned.append(eid)
            continue
        distinct = sorted(set(per_path_layer.values()), key=lambda n: rank_of_layer[n])
        chosen = distinct[0]                       # index 0 in `order` is the HIGHEST layer
        layer_of[eid] = chosen
        if len(distinct) > 1:
            counts: dict[str, int] = {}
            for name in per_path_layer.values():
                counts[name] = counts.get(name, 0) + 1
            splits.append({"element": eid, "chosen": chosen,
                           "layer_split": dict(sorted(counts.items(),
                                                      key=lambda kv: rank_of_layer[kv[0]]))})

    return {
        "layer_of": dict(sorted(layer_of.items())),
        "index_of": {eid: rank_of_layer[name] for eid, name in sorted(layer_of.items())},
        "splits": sorted(splits, key=lambda s: s["element"]),
        "ambiguous": ambiguous,
        "unassigned": sorted(unassigned),
    }
