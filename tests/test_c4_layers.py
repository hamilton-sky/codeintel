"""Tests for `codeintel.c4_layers` — inferred architectural layers. No backend involved: every input
here is a hand-written element/relation list, matching the module's pure contract.

The tests are organised around the properties the design argues for, not around the functions, because
the properties are what could silently break. In particular `test_a_shortcut_edge_does_not_drag_its_
target_up` pins the single decisive algorithmic choice in §2.2 — longest path rather than shortest —
and would pass under either rule if it were written with a simpler graph.
"""
from __future__ import annotations

import tomllib

from codeintel.c4_layers import (
    DEGRADED_FRACTION,
    compute_layers,
    empty_layers,
    suggest_config,
)


def _elements(*ids: str) -> list[dict]:
    return [{"id": i, "path": f"{i.replace('.', '/')}.py", "title": i.rsplit(".", 1)[-1]}
            for i in ids]


def _imports(*pairs: tuple[str, str]) -> list[dict]:
    return [{"from": a, "to": b, "n": 1, "kind": "imports"} for a, b in pairs]


# ── the theorem (§2.3) ────────────────────────────────────────────────────────────────────────────

def test_every_import_edge_strictly_descends():
    """The invariant the whole feature rests on. If this fails, a layer diagram drawn from these
    ranks shows edges pointing sideways or upward, and the claim that vertical position means
    something is void."""
    els = _elements("a", "b", "c", "d")
    rels = _imports(("a", "b"), ("b", "c"), ("c", "d"), ("a", "d"))
    ranks = compute_layers(els, rels)["ranks"]
    for rel in rels:
        assert ranks[rel["from"]] > ranks[rel["to"]], rel


def test_a_shortcut_edge_does_not_drag_its_target_up():
    """The decisive test for longest-path over shortest-path ranking.

    `top` imports both `mid` and, directly, `leaf` — the shortcut. Under `height = 1 + min(...)`,
    `top` would sit one above `leaf` and therefore level with `mid`, and the `top -> mid` edge would
    stop descending. Under `1 + max(...)` the chain is respected. A three-node test without the
    shortcut passes under either rule, which is why this shape is the one pinned.
    """
    els = _elements("top", "mid", "leaf")
    layers = compute_layers(els, _imports(("top", "mid"), ("mid", "leaf"), ("top", "leaf")))
    ranks = layers["ranks"]
    assert ranks == {"leaf": 0, "mid": 1, "top": 2}


def test_pure_leaves_all_share_rank_zero():
    """Height-to-sink, not depth-from-source: two leaves reached by chains of different length still
    land together, which is the reading a layer diagram implies."""
    els = _elements("cli", "deep1", "deep2", "leafA", "leafB")
    layers = compute_layers(els, _imports(
        ("cli", "leafA"),                                  # one hop
        ("cli", "deep1"), ("deep1", "deep2"), ("deep2", "leafB"),   # three hops
    ))
    ranks = layers["ranks"]
    assert ranks["leafA"] == 0
    assert ranks["leafB"] == 0


# ── decision (1): isolated elements ───────────────────────────────────────────────────────────────

def test_an_isolated_element_is_unassigned_and_never_lands_in_the_foundation():
    """The measured defect from Phase 0: longest-path ranking gives a zero-degree element height 0,
    which is the bottom band, and a reader takes the bottom band to be the foundation. On brightsky-ai
    that put 18 of 29 elements — configs, scripts and docs — into the 'foundation'."""
    els = _elements("app", "lib", "vite.config", "eslint.config")
    layers = compute_layers(els, _imports(("app", "lib")))

    assert layers["unassigned"] == ["eslint.config", "vite.config"]
    assert "vite.config" not in layers["ranks"]
    assert layers["ranks"] == {"lib": 0, "app": 1}
    # The bottom band must contain only the genuinely-depended-upon element.
    assert layers["bands"][-1] == {"rank": 0, "elements": ["lib"]}
    assert layers["stats"]["elements_unassigned"] == 2
    assert layers["stats"]["elements_ranked"] == 2


def test_an_element_with_only_incoming_imports_is_still_ranked():
    """Zero OUT-degree is a sink, which is a real foundation. Only zero degree BOTH ways is
    unassigned — conflating the two would drop every leaf in the repository."""
    layers = compute_layers(_elements("a", "b"), _imports(("a", "b")))
    assert layers["unassigned"] == []
    assert layers["ranks"]["b"] == 0


def test_every_element_isolated_yields_no_bands_rather_than_an_error():
    layers = compute_layers(_elements("x", "y"), [])
    assert layers["bands"] == []
    assert layers["ranks"] == {}
    assert layers["unassigned"] == ["x", "y"]
    assert layers["degraded"] is False


# ── cycles (§2.4) ─────────────────────────────────────────────────────────────────────────────────

def test_a_cycle_becomes_one_rank_shared_by_every_member():
    """Members of an import cycle genuinely have no relative order; stacking them would invent one."""
    els = _elements("cli", "a", "b", "c", "leaf")
    layers = compute_layers(els, _imports(
        ("cli", "a"), ("a", "b"), ("b", "c"), ("c", "a"), ("c", "leaf")))
    ranks = layers["ranks"]
    assert ranks["a"] == ranks["b"] == ranks["c"]
    assert ranks["cli"] > ranks["a"] > ranks["leaf"]

    assert len(layers["cycles"]) == 1
    cycle = layers["cycles"][0]
    assert cycle["size"] == 3
    assert cycle["members"] == ["a", "b", "c"]
    assert cycle["rank"] == ranks["a"]
    assert layers["stats"]["condensation_nodes"] == 3      # {a,b,c}, cli, leaf


def test_an_edge_inside_a_cycle_is_the_only_place_ranks_may_be_equal():
    """Outside a cycle, equal ranks would mean a non-descending edge — the invariant's failure mode."""
    els = _elements("a", "b", "c")
    rels = _imports(("a", "b"), ("b", "a"), ("b", "c"))
    layers = compute_layers(els, rels)
    ranks, members = layers["ranks"], {m for c in layers["cycles"] for m in c["members"]}
    for rel in rels:
        src, dst = rel["from"], rel["to"]
        if ranks[src] == ranks[dst]:
            assert src in members and dst in members, rel
        else:
            assert ranks[src] > ranks[dst], rel


def test_two_separate_cycles_are_reported_separately():
    els = _elements("a", "b", "c", "d", "mid")
    layers = compute_layers(els, _imports(
        ("a", "b"), ("b", "a"),          # cycle 1
        ("a", "mid"), ("mid", "c"),
        ("c", "d"), ("d", "c"),          # cycle 2
    ))
    assert [c["size"] for c in layers["cycles"]] == [2, 2]
    assert layers["stats"]["cycles"] == 2
    assert layers["stats"]["largest_cycle"] == 2


def test_cycle_members_are_capped_for_display_but_the_size_is_not():
    """A 21-member SCC must not bury the payload, and truncating the display must not truncate the
    fact — the count and the omitted tally are both carried."""
    names = [f"n{i:02d}" for i in range(20)]
    ring = [(names[i], names[(i + 1) % len(names)]) for i in range(len(names))]
    layers = compute_layers(_elements(*names), _imports(*ring))
    cycle = layers["cycles"][0]
    assert cycle["size"] == 20
    assert len(cycle["members"]) == 12
    assert cycle["members_omitted"] == 8


# ── decision (2): the degradation guard ───────────────────────────────────────────────────────────

def test_degraded_is_false_at_the_fractions_real_repositories_reach():
    """Phase 0's largest measured IMPORTS SCC was 13.8%. The guard must not fire there, or it would
    stamp `degraded` on views that read perfectly well."""
    names = [f"n{i}" for i in range(10)]
    ring = [("n0", "n1"), ("n1", "n0")]                       # 2 of 10 ranked = 20%
    chain = [(names[i], names[i + 1]) for i in range(1, 9)]
    layers = compute_layers(_elements(*names), _imports(*ring, *chain))
    assert layers["stats"]["largest_cycle"] == 2
    assert layers["degraded"] is False


def test_degraded_fires_when_one_cycle_swallows_most_of_the_graph():
    names = [f"n{i}" for i in range(6)]
    ring = [(names[i], names[(i + 1) % 5]) for i in range(5)]  # 5-member cycle
    layers = compute_layers(_elements(*names), _imports(*ring, ("n0", "n5")))
    assert layers["stats"]["largest_cycle"] == 5
    assert layers["stats"]["largest_cycle_fraction_ranked"] > DEGRADED_FRACTION
    assert layers["degraded"] is True


def test_both_cycle_fractions_are_reported_so_the_phase_0_numbers_stay_comparable():
    """The flag uses the RANKED denominator (the population the view draws), but Phase 0's recorded
    table was computed against ALL elements. Carrying both means adding the isolated-element decision
    did not silently redefine the number that calibrated the threshold."""
    els = _elements("a", "b", "lonely")
    layers = compute_layers(els, _imports(("a", "b"), ("b", "a")))
    stats = layers["stats"]
    assert stats["largest_cycle_fraction_ranked"] == 1.0       # 2 of 2 ranked
    assert stats["largest_cycle_fraction_total"] == round(2 / 3, 4)


# ── edge source discipline (§1) ───────────────────────────────────────────────────────────────────

def test_calls_usage_relations_are_ignored_entirely():
    """The union is not layerable — Phase 0 measured 38.8%-70.4% of elements collapsing into a single
    SCC. A `calls_usage` edge leaking in would re-import that problem."""
    els = _elements("a", "b")
    rels = [{"from": "a", "to": "b", "n": 1, "kind": "calls_usage"}]
    layers = compute_layers(els, rels)
    assert layers["ranks"] == {}
    assert layers["unassigned"] == ["a", "b"]
    assert layers["stats"]["edges_used"] == 0
    assert layers["stats"]["edges_ignored_kind"] == 1


def test_a_self_loop_does_not_make_an_element_its_own_cycle():
    layers = compute_layers(_elements("a", "b"), _imports(("a", "a"), ("a", "b")))
    assert layers["cycles"] == []
    assert layers["stats"]["edges_self"] == 1
    assert layers["ranks"] == {"b": 0, "a": 1}


def test_an_endpoint_that_is_not_an_emitted_element_is_dropped_not_invented():
    """Inventing a node would put a box in the layer view that the index view does not have."""
    layers = compute_layers(_elements("a"), _imports(("a", "ghost"), ("ghost", "a")))
    assert layers["ranks"] == {}
    assert layers["unassigned"] == ["a"]
    assert "ghost" not in layers["ranks"]


def test_a_duplicated_relation_is_counted_once():
    layers = compute_layers(_elements("a", "b"), _imports(("a", "b"), ("a", "b")))
    assert layers["stats"]["edges_used"] == 1


# ── determinism and shape ─────────────────────────────────────────────────────────────────────────

def test_output_is_deterministic_and_sorted():
    """`c4`'s output is committed and diffed, so an unstable order would churn the model on an
    unchanged repository."""
    els = _elements("d", "a", "c", "b")
    rels = _imports(("d", "a"), ("c", "a"), ("b", "a"))
    first = compute_layers(els, rels)
    second = compute_layers(list(reversed(els)), list(reversed(rels)))
    assert first == second
    assert list(first["ranks"]) == sorted(first["ranks"])
    for band in first["bands"]:
        assert band["elements"] == sorted(band["elements"])


def test_bands_run_top_to_bottom_so_index_zero_is_the_highest_rank():
    """Matches §3.2's declared schema, where `order[0]` is the top layer — so a generated config is
    already in the right direction rather than reversed."""
    layers = compute_layers(_elements("a", "b", "c"), _imports(("a", "b"), ("b", "c")))
    assert [band["rank"] for band in layers["bands"]] == [2, 1, 0]
    assert layers["bands"][0]["elements"] == ["a"]
    assert layers["bands"][-1]["elements"] == ["c"]
    assert layers["stats"]["depth"] == 3


def test_a_chain_deeper_than_the_recursion_limit_still_ranks():
    """Tarjan and the height DP are iterative for this reason: element count comes from whatever a
    repository contains, and a `RecursionError` would turn a diagram into a crash."""
    names = [f"n{i:04d}" for i in range(3000)]
    chain = [(names[i], names[i + 1]) for i in range(len(names) - 1)]
    layers = compute_layers(_elements(*names), _imports(*chain))
    assert layers["ranks"][names[0]] == len(names) - 1
    assert layers["ranks"][names[-1]] == 0
    assert layers["cycles"] == []


def test_empty_layers_is_a_fresh_copy_each_call():
    """It is written into `_EMPTY` and returned from error paths; a shared nested dict would let one
    caller's mutation leak into the next payload."""
    first, second = empty_layers(), empty_layers()
    first["stats"]["depth"] = 99
    assert second["stats"]["depth"] == 0


def test_no_elements_at_all_is_not_an_error():
    layers = compute_layers([], [])
    assert layers["bands"] == [] and layers["ranks"] == {} and layers["degraded"] is False
    assert layers["stats"]["elements_total"] == 0


# ── suggest_config ────────────────────────────────────────────────────────────────────────────────

def test_suggested_config_is_parseable_toml_with_members_matching_order():
    els = _elements("cli", "core", "util")
    layers = compute_layers(els, _imports(("cli", "core"), ("core", "util")))
    parsed = tomllib.loads(suggest_config(layers["ranks"], els))["layers"]

    assert parsed["order"] == ["layer_0", "layer_1", "layer_2"]
    assert list(parsed["members"]) == parsed["order"]
    # Top to bottom: the highest rank is layer_0.
    assert parsed["members"]["layer_0"] == ["cli.py"]
    assert parsed["members"]["layer_2"] == ["util.py"]
    assert parsed["strict_adjacent"] is False
    assert parsed["require_all"] is False


def test_suggested_config_lists_file_paths_not_element_ids():
    """§3.3 — membership matches file paths, which is what keeps a config valid across a `--depth`
    change: element ids are a function of the roll-up depth and paths are not."""
    els = [{"id": "src.pkg.mod", "path": "src/pkg/mod.py", "title": "mod"}]
    ranks = {"src.pkg.mod": 0}
    members = tomllib.loads(suggest_config(ranks, els))["layers"]["members"]
    assert members["layer_0"] == ["src/pkg/mod.py"]


def test_a_cross_language_merge_contributes_every_one_of_its_paths():
    """`build_c4_payload` joins the paths of a merged element with ', ' into one `path` string.
    Splitting it is what stops a config silently covering only the first file of the pair."""
    els = [{"id": "src.thing", "path": "src/thing.py, src/thing.ts", "title": "thing"}]
    members = tomllib.loads(suggest_config({"src.thing": 0}, els))["layers"]["members"]
    assert members["layer_0"] == ["src/thing.py", "src/thing.ts"]


def test_no_ranks_yields_an_empty_order_rather_than_a_guess():
    parsed = tomllib.loads(suggest_config({}, _elements("a")))["layers"]
    assert parsed["order"] == []


def test_a_path_containing_a_quote_is_escaped_rather_than_breaking_the_file():
    """Paths are data from an index, not literals written here."""
    els = [{"id": "odd", "path": 'src/we"ird.py', "title": "weird"}]
    members = tomllib.loads(suggest_config({"odd": 0}, els))["layers"]["members"]
    assert members["layer_0"] == ['src/we"ird.py']


def test_an_element_with_a_rank_but_no_path_keeps_its_layer_in_order():
    """Dropping the layer would change `order`'s length and silently renumber every layer below it."""
    els = [{"id": "ranked", "path": "", "title": "ranked"},
           {"id": "real", "path": "src/real.py", "title": "real"}]
    text = suggest_config({"ranked": 1, "real": 0}, els)
    parsed = tomllib.loads(text)["layers"]
    assert parsed["order"] == ["layer_0", "layer_1"]
    assert parsed["members"]["layer_0"] == []
    assert parsed["members"]["layer_1"] == ["src/real.py"]
