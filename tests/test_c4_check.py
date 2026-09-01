"""Tests for the Phase 2 layer check — config, membership globs, and every §5.2 finding class.

No backend: every input is a hand-written payload plus a hand-written config dict, matching the pure
contract of `c4_layers.parse_layers_config` / `c4_check.check_layers`.

The tests are grouped by the property at risk rather than by function. Two groups carry more weight
than the rest and are worth reading first:

* `test_a_single_star_does_not_cross_a_path_separator` and its neighbours pin the trap the design
  names explicitly — `fnmatch` on a whole path silently widens every pattern an author writes.
* the allowlist group pins the one rule separating an allowlist from a mute button: an entry without
  a `reason` is itself gating, and it does not excuse the violation it names.
"""
from __future__ import annotations

from codeintel.c4_check import PATHS_REPORT_CAP, check_layers, render_report
from codeintel.c4_layers import assign_declared_layers, glob_match, parse_layers_config


def _payload(elements, relations, *, depth=2, project="demo"):
    return {"project": project, "fit": {"depth": depth, "how": "auto-fit"},
            "elements": elements, "relations": relations}


def _el(eid, path):
    return {"id": eid, "path": path, "title": eid.rsplit(".", 1)[-1]}


def _imp(a, b, n=1):
    return {"from": a, "to": b, "n": n, "kind": "imports"}


def _cfg(order, members=None, **switches):
    block: dict = {"order": order}
    if members is not None:
        block["members"] = members
    block.update(switches)
    return parse_layers_config({"layers": block})


# ── the glob matcher (§3.3) ────────────────────────────────────────────────────────────────────────

def test_a_single_star_does_not_cross_a_path_separator():
    """The trap the design names: `fnmatch`'s `*` crosses `/`, so `src/*.py` would match
    `src/a/b.py` and silently widen every pattern an author writes — a check that quietly covers more
    than it claims."""
    assert glob_match("src/*.py", "src/a.py") is True
    assert glob_match("src/*.py", "src/a/b.py") is False


def test_a_double_star_crosses_separators_and_matches_zero_segments():
    assert glob_match("src/**/*.py", "src/a/b/c.py") is True
    assert glob_match("src/**", "src/a/b/c.py") is True
    # Zero segments — `a/**/b` must match `a/b`, which is what the inclusive upper bound buys.
    assert glob_match("a/**/b", "a/b") is True
    assert glob_match("a/**/b", "a/x/y/b") is True


def test_an_exact_path_matches_only_itself():
    assert glob_match("src/codeintel/gateway.py", "src/codeintel/gateway.py") is True
    assert glob_match("src/codeintel/gateway.py", "src/codeintel/gateway_extra.py") is False


def test_character_classes_work_inside_one_segment():
    """A free consequence of matching segment-by-segment rather than hand-rolling character logic."""
    assert glob_match("v[0-9]/a.py", "v2/a.py") is True
    assert glob_match("v[0-9]/a.py", "vx/a.py") is False


def test_leading_and_trailing_slashes_do_not_change_a_match():
    assert glob_match("/src/a.py", "src/a.py") is True
    assert glob_match("src/", "src") is True


# ── membership resolution ─────────────────────────────────────────────────────────────────────────

def test_the_most_specific_pattern_wins_regardless_of_declaration_order():
    """This is what lets a catch-all coexist with a specific claim without obliging the author to
    order them correctly — and getting that ordering wrong would be invisible."""
    parsed = _cfg(["gateway", "core"],
                  {"gateway": ["src/gateway.py"], "core": ["src/*.py"]})
    got = assign_declared_layers([_el("gateway", "src/gateway.py"), _el("misc", "src/misc.py")],
                                 parsed)
    assert got["layer_of"] == {"gateway": "gateway", "misc": "core"}


def test_the_catch_all_still_wins_when_it_is_declared_first():
    parsed = _cfg(["core", "gateway"],
                  {"core": ["src/*.py"], "gateway": ["src/gateway.py"]})
    got = assign_declared_layers([_el("gateway", "src/gateway.py")], parsed)
    assert got["layer_of"] == {"gateway": "gateway"}


def test_an_equally_specific_tie_across_layers_is_reported_not_silently_resolved():
    """Two patterns of identical specificity from different layers can only be separated by
    declaration order, which is arbitrary. The winner is the earlier layer, and the arbitrariness is
    recorded."""
    parsed = _cfg(["first", "second"],
                  {"first": ["src/a.py"], "second": ["src/a.py"]})
    got = assign_declared_layers([_el("a", "src/a.py")], parsed)
    assert got["layer_of"] == {"a": "first"}
    assert len(got["ambiguous"]) == 1
    amb = got["ambiguous"][0]
    assert amb["layer_a"] == "first" and amb["layer_b"] == "second"


def test_an_element_whose_files_span_layers_takes_the_highest_one():
    """Highest, not majority (§3.4): layering constrains what may depend on what, so a container
    holding one `cli` file must be treated as `cli` or edges into it are wrongly judged fine. When a
    check must guess, it guesses toward reporting more."""
    parsed = _cfg(["cli", "core"],
                  {"cli": ["pkg/cli.py"], "core": ["pkg/a.py", "pkg/b.py"]})
    got = assign_declared_layers([_el("pkg", "pkg/a.py, pkg/b.py, pkg/cli.py")], parsed)
    assert got["layer_of"] == {"pkg": "cli"}
    assert got["splits"] == [{"element": "pkg", "chosen": "cli",
                              "layer_split": {"cli": 1, "core": 2}}]


def test_an_element_matching_nothing_is_unassigned_not_dropped_and_not_bottom():
    parsed = _cfg(["only"], {"only": ["src/a.py"]})
    got = assign_declared_layers([_el("a", "src/a.py"), _el("z", "other/z.py")], parsed)
    assert got["unassigned"] == ["z"]
    assert "z" not in got["layer_of"]


# ── config validation (§3.1) ──────────────────────────────────────────────────────────────────────

def test_no_layers_block_is_absent_not_malformed():
    parsed = parse_layers_config({"engine": "auto"})
    assert parsed["present"] is False and parsed["problem"] == ""


def test_each_malformed_shape_yields_a_named_problem_rather_than_a_traceback():
    cases = {
        "layers-not-a-table": {"layers": "nope"},
        "layers-order-missing": {"layers": {}},
        "layers-order-not-strings": {"layers": {"order": ["a", 2]}},
        "layers-order-duplicate": {"layers": {"order": ["a", "a"]}},
        "layers-members-not-a-table": {"layers": {"order": ["a"], "members": []}},
        "layers-allow-not-a-list": {"layers": {"order": ["a"], "members": {}, "allow": 3}},
    }
    for expected, config in cases.items():
        assert parse_layers_config(config)["problem"] == expected, expected


def test_a_member_layer_missing_from_order_is_rejected():
    """A layer with members but no place in `order` has no rank, so an edge touching it could not be
    judged — accepting it silently would make the config cover less than it appears to."""
    parsed = parse_layers_config({"layers": {"order": ["a"], "members": {"a": ["x"], "ghost": ["y"]}}})
    assert parsed["problem"] == "layers-members-not-in-order:ghost"


def test_a_non_bool_switch_is_rejected_by_name():
    parsed = parse_layers_config({"layers": {"order": ["a"], "members": {}, "require_all": "yes"}})
    assert parsed["problem"] == "layers-require-all-not-a-bool"


def test_switch_defaults_follow_the_reasoning_not_the_stale_comment():
    """§5.2's table is the authority over §3.2's "both default false" comment: same-layer imports are
    normal, strict adjacency produces a wall of findings on real code."""
    parsed = _cfg(["a"], {"a": ["x"]})
    assert parsed["switches"] == {"strict_adjacent": False, "allow_same_layer": True,
                                 "require_all": False}


def test_a_string_member_pattern_is_accepted_as_a_one_element_list():
    parsed = parse_layers_config({"layers": {"order": ["a"], "members": {"a": "src/*.py"}}})
    assert parsed["problem"] == "" and parsed["members"] == {"a": ["src/*.py"]}


def test_an_allowlist_entry_without_a_reason_parses_so_the_check_can_report_it():
    """Rejecting it here would deny the check the chance to raise `allow-no-reason`, which is the
    rule that makes the allowlist trustworthy."""
    parsed = parse_layers_config({"layers": {"order": ["a"], "members": {"a": ["x"]},
                                             "allow": [{"from": "p", "to": "q"}]}})
    assert parsed["problem"] == ""
    assert parsed["allow"] == [{"index": 0, "from": "p", "to": "q", "reason": ""}]


# ── violations (§5.1) ─────────────────────────────────────────────────────────────────────────────

def _one_violation_setup(**switches):
    parsed = _cfg(["top", "bottom"], {"top": ["src/top.py"], "bottom": ["src/bot.py"]},
                  **switches)
    payload = _payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                       [_imp("bot", "top", 3)])
    return check_layers(payload, parsed)


def test_an_upward_import_between_two_declared_layers_gates():
    result = _one_violation_setup()
    violations = [f for f in result["findings"] if f["kind"] == "violation"]
    assert len(violations) == 1
    finding = violations[0]
    assert finding["rule"] == "layer-order"
    assert finding["severity"] == "gating"
    assert finding["direction"] == "up"
    assert finding["from_layer"] == "bottom" and finding["to_layer"] == "top"
    assert finding["edge_source"] == "imports"
    assert finding["witness"] == {"from": "src/bot.py", "to": "src/top.py", "n": 3}
    assert result["gating"] == 1


def test_an_edge_with_one_undeclared_end_is_never_a_violation():
    """You cannot violate an order you did not declare (§3.5)."""
    parsed = _cfg(["top"], {"top": ["src/top.py"]})
    payload = _payload([_el("top", "src/top.py"), _el("free", "src/free.py")],
                       [_imp("top", "free"), _imp("free", "top")])
    result = check_layers(payload, parsed)
    assert [f for f in result["findings"] if f["kind"] == "violation"] == []


def test_a_downward_import_is_not_a_violation():
    parsed = _cfg(["top", "bottom"], {"top": ["src/top.py"], "bottom": ["src/bot.py"]})
    payload = _payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                       [_imp("top", "bot")])
    assert check_layers(payload, parsed)["gating"] == 0


def test_a_same_layer_import_is_allowed_by_default_and_gates_when_switched_off():
    members = {"one": ["src/a.py", "src/b.py"]}
    payload = _payload([_el("a", "src/a.py"), _el("b", "src/b.py")], [_imp("a", "b")])
    assert check_layers(payload, _cfg(["one"], members))["gating"] == 0

    strict = check_layers(payload, _cfg(["one"], members, allow_same_layer=False))
    assert strict["gating"] == 1
    assert [f["direction"] for f in strict["findings"] if f["kind"] == "violation"] == ["same"]


def test_skipping_a_layer_is_allowed_by_default_and_gates_under_strict_adjacent():
    """Half the layered-architecture literature means strict adjacency and half does not; strict
    produces a wall of findings on real code, so it is opt-in."""
    members = {"a": ["src/a.py"], "b": ["src/b.py"], "c": ["src/c.py"]}
    payload = _payload([_el("a", "src/a.py"), _el("b", "src/b.py"), _el("c", "src/c.py")],
                       [_imp("a", "c")])
    assert check_layers(payload, _cfg(["a", "b", "c"], members))["gating"] == 0

    strict = check_layers(payload, _cfg(["a", "b", "c"], members, strict_adjacent=True))
    assert strict["gating"] == 1
    finding = next(f for f in strict["findings"] if f["kind"] == "violation")
    assert finding["direction"] == "skip" and finding["layers_skipped"] == 1


def test_a_calls_usage_edge_pointing_up_is_advisory_and_never_gates():
    """The union fabricates edges by bare symbol name and is not evidence of a dependency (§1)."""
    parsed = _cfg(["top", "bottom"], {"top": ["src/top.py"], "bottom": ["src/bot.py"]})
    payload = _payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                       [{"from": "bot", "to": "top", "n": 9, "kind": "calls_usage"}])
    result = check_layers(payload, parsed)
    assert result["gating"] == 0
    advisory = [f for f in result["findings"] if f["kind"] == "advisory"]
    assert len(advisory) == 1
    assert advisory[0]["severity"] == "advisory"
    assert advisory[0]["edge_source"] == "calls_usage"


# ── the allowlist (§5.4) ──────────────────────────────────────────────────────────────────────────

def test_an_allowlisted_violation_stays_in_the_list_demoted_to_info():
    """"Visible, not gating" is a property of the record, not of one serializer's formatting."""
    parsed = parse_layers_config({"layers": {
        "order": ["top", "bottom"],
        "members": {"top": ["src/top.py"], "bottom": ["src/bot.py"]},
        "allow": [{"from": "src/bot.py", "to": "src/top.py", "reason": "tracked in #1"}]}})
    result = check_layers(_payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                                   [_imp("bot", "top")]), parsed)
    finding = next(f for f in result["findings"] if f["kind"] == "violation")
    assert finding["allowlisted"] is True
    assert finding["severity"] == "info"
    assert finding["allow_reason"] == "tracked in #1"
    assert finding["allow_index"] == 0
    assert result["gating"] == 0


def test_an_allowlist_entry_without_a_reason_gates_and_excuses_nothing():
    """The single rule that separates an allowlist from a mute button. Two gating findings, not zero:
    the entry itself, and the violation it failed to excuse."""
    parsed = parse_layers_config({"layers": {
        "order": ["top", "bottom"],
        "members": {"top": ["src/top.py"], "bottom": ["src/bot.py"]},
        "allow": [{"from": "src/bot.py", "to": "src/top.py"}]}})
    result = check_layers(_payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                                   [_imp("bot", "top")]), parsed)
    kinds = sorted(f["kind"] for f in result["findings"] if f["severity"] == "gating")
    assert kinds == ["allow-no-reason", "violation"]
    assert result["gating"] == 2


def test_a_stale_allowlist_entry_is_reported_and_never_gates():
    """Without staleness reporting an allowlist only ever grows, and in three years nobody knows
    which entries are load-bearing."""
    parsed = parse_layers_config({"layers": {
        "order": ["top", "bottom"],
        "members": {"top": ["src/top.py"], "bottom": ["src/bot.py"]},
        "allow": [{"from": "src/gone.py", "to": "src/nowhere.py", "reason": "fixed long ago"}]}})
    result = check_layers(_payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")], []), parsed)
    stale = [f for f in result["findings"] if f["kind"] == "stale-allow"]
    assert len(stale) == 1 and stale[0]["severity"] == "info"
    assert result["gating"] == 0


def test_an_allowlist_entry_may_use_a_glob_and_an_element_id():
    parsed = parse_layers_config({"layers": {
        "order": ["top", "bottom"],
        "members": {"top": ["src/top.py"], "bottom": ["src/sub/bot.py"]},
        "allow": [{"from": "src/sub/**", "to": "top", "reason": "by glob and by element id"}]}})
    result = check_layers(_payload([_el("top", "src/top.py"), _el("bot", "src/sub/bot.py")],
                                   [_imp("bot", "top")]), parsed)
    assert result["gating"] == 0
    assert next(f for f in result["findings"] if f["kind"] == "violation")["allowlisted"] is True


# ── the other classes ─────────────────────────────────────────────────────────────────────────────

def test_a_cycle_gates_because_it_is_a_source_confirmed_structural_fact():
    parsed = _cfg(["one"], {"one": ["src/a.py", "src/b.py"]})
    payload = _payload([_el("a", "src/a.py"), _el("b", "src/b.py")],
                       [_imp("a", "b"), _imp("b", "a")])
    result = check_layers(payload, parsed)
    cycles = [f for f in result["findings"] if f["kind"] == "cycle"]
    assert len(cycles) == 1
    assert cycles[0]["severity"] == "gating"
    assert cycles[0]["cycle_members"] == ["a", "b"]
    assert cycles[0]["edge_source"] == "cycle"


def test_unassigned_is_info_by_default_and_gating_under_require_all():
    members = {"only": ["src/a.py"]}
    elements = [_el("a", "src/a.py"), _el("z", "other/z.py")]
    default = check_layers(_payload(elements, []), _cfg(["only"], members))
    assert [f["severity"] for f in default["findings"] if f["kind"] == "unassigned"] == ["info"]
    assert default["gating"] == 0

    strict = check_layers(_payload(elements, []), _cfg(["only"], members, require_all=True))
    assert strict["gating"] == 1


def test_a_wide_declared_layer_is_reported_as_spread_and_never_gates():
    """A declared layer whose members span many inferred ranks is probably two layers wearing one
    name (§3.7). Informational — and the threshold is a stated guess."""
    members = {"everything": ["src/a.py", "src/b.py", "src/c.py"]}
    payload = _payload([_el("a", "src/a.py"), _el("b", "src/b.py"), _el("c", "src/c.py")],
                       [_imp("a", "b"), _imp("b", "c")])
    result = check_layers(payload, _cfg(["everything"], members, allow_same_layer=True))
    spread = [f for f in result["findings"] if f["kind"] == "spread"]
    assert len(spread) == 1 and spread[0]["severity"] == "info"
    assert "0..2" in spread[0]["message"]


def test_shorthand_config_produces_findings_but_zero_gating():
    """§3.6 — guessing which layer a file belongs to and then failing someone's build on the guess is
    the worst outcome this feature could produce."""
    parsed = parse_layers_config({"layers": {"order": ["providers", "commands"]}})
    assert parsed["shorthand"] is True
    payload = _payload([_el("p", "src/providers/graph.py"), _el("c", "src/commands/query.py")],
                       [_imp("p", "c")])
    result = check_layers(payload, parsed)
    assert result["shorthand"] is True
    assert result["gating"] == 0


# ── record shape and determinism (§5.5) ───────────────────────────────────────────────────────────

def test_every_record_carries_every_field_so_no_consumer_branches_on_key_existence():
    result = _one_violation_setup()
    expected = {
        "rule", "kind", "severity", "message", "from_element", "to_element", "from_paths",
        "to_paths", "witness", "witnesses_total", "weight", "from_layer", "from_layer_index",
        "to_layer", "to_layer_index", "direction", "layers_skipped", "edge_source", "confirmed_by",
        "cycle_members", "allowlisted", "allow_reason", "allow_index", "depth",
    }
    for finding in result["findings"]:
        assert set(finding) == expected, finding["kind"]


def test_a_records_depth_is_the_roll_up_it_was_checked_at():
    parsed = _cfg(["top", "bottom"], {"top": ["src/top.py"], "bottom": ["src/bot.py"]})
    payload = _payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                       [_imp("bot", "top")], depth=7)
    assert all(f["depth"] == 7 for f in check_layers(payload, parsed)["findings"])


def test_findings_are_ordered_deterministically():
    """Both the text output and the JSON must diff cleanly between runs — the same requirement the
    emitted `.c4` has."""
    members = {"top": ["src/top.py"], "bottom": ["src/b1.py", "src/b2.py"]}
    elements = [_el("top", "src/top.py"), _el("b1", "src/b1.py"), _el("b2", "src/b2.py")]
    rels = [_imp("b2", "top"), _imp("b1", "top")]
    first = check_layers(_payload(elements, rels), _cfg(["top", "bottom"], members))
    second = check_layers(_payload(list(reversed(elements)), list(reversed(rels))),
                          _cfg(["top", "bottom"], members))
    assert first["findings"] == second["findings"]


def test_from_paths_is_capped_but_the_witness_total_is_not():
    paths = ", ".join(f"src/sub/f{i}.py" for i in range(12))
    parsed = _cfg(["top", "bottom"], {"top": ["src/top.py"], "bottom": ["src/sub/**"]})
    payload = _payload([_el("top", "src/top.py"), _el("many", paths)], [_imp("many", "top")])
    finding = next(f for f in check_layers(payload, parsed)["findings"]
                   if f["kind"] == "violation")
    assert len(finding["from_paths"]) == PATHS_REPORT_CAP
    assert finding["witnesses_total"] >= 12


def test_a_malformed_config_yields_a_problem_and_no_findings():
    parsed = parse_layers_config({"layers": {"order": []}})
    result = check_layers(_payload([_el("a", "src/a.py")], []), parsed)
    assert result["problem"] == "layers-order-missing"
    assert result["findings"] == [] and result["gating"] == 0


# ── the text serializer (§5.6) ────────────────────────────────────────────────────────────────────

def test_the_report_puts_the_gating_count_and_exit_code_on_the_last_line():
    """Whoever is reading a failed CI step is looking at the bottom."""
    result = _one_violation_setup()
    text = render_report(_payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")],
                                  [_imp("bot", "top", 3)]), result)
    assert text.strip().splitlines()[-1] == "1 gating finding(s) — exit 2"


def test_the_report_collapses_advisory_to_a_count_and_never_enumerates_it():
    """It is the largest and least trustworthy class, and a report leading with fourteen advisories
    teaches people to ignore the whole thing. Collapsing is a correctness property of the report."""
    parsed = _cfg(["top", "bottom"], {"top": ["src/top.py"], "bottom": ["src/bot.py"]})
    rels = [{"from": "bot", "to": "top", "n": i, "kind": "calls_usage"} for i in range(1, 4)]
    payload = _payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")], rels)
    text = render_report(payload, check_layers(payload, parsed))
    assert "ADVISORY (3 — never gating)" in text
    # The individual advisory messages must not appear.
    assert "points up, but only via CALLS/USAGE" not in text


def test_the_report_names_the_allow_reason_instead_of_the_violation_message():
    parsed = parse_layers_config({"layers": {
        "order": ["top", "bottom"],
        "members": {"top": ["src/top.py"], "bottom": ["src/bot.py"]},
        "allow": [{"from": "src/bot.py", "to": "src/top.py", "reason": "known: #7"}]}})
    payload = _payload([_el("top", "src/top.py"), _el("bot", "src/bot.py")], [_imp("bot", "top")])
    text = render_report(payload, check_layers(payload, parsed))
    assert "ALLOWED: known: #7" in text
    assert "0 gating finding(s) — exit 0" in text.strip().splitlines()[-1]


def test_the_report_warns_that_a_shorthand_config_cannot_gate():
    parsed = parse_layers_config({"layers": {"order": ["providers", "commands"]}})
    payload = _payload([_el("p", "src/providers/g.py"), _el("c", "src/commands/q.py")],
                       [_imp("p", "c")])
    text = render_report(payload, check_layers(payload, parsed))
    assert "shorthand config" in text
    assert "exit 0" in text.strip().splitlines()[-1]


def test_the_report_states_coverage_because_it_measures_how_much_of_the_check_is_real():
    parsed = _cfg(["only"], {"only": ["src/a.py"]})
    payload = _payload([_el("a", "src/a.py"), _el("z", "other/z.py")], [])
    text = render_report(payload, check_layers(payload, parsed))
    assert "coverage: 1 of 2 elements assigned; 1 unassigned" in text
