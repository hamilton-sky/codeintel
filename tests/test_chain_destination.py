"""`chain A->B` honours B: it resolves B, walks outbound from A, and answers one of three things.

Red-team findings `chain-ignores-destination` and `chain-no-resolution-labels` (T3). Before this,
`_op_chain` did `target.split("->")[0]` and dropped B, so `chain "A -> B"` printed A's whole
neighbourhood under a heading that read like the path that was asked for, and a hop the backend
gave no `strategy` for printed no label at all, indistinguishable from a resolved one.

Every stub here answers the way codebase-memory-mcp 0.9.0 does: `trace_path` hops carry `name`,
`qualified_name` and `hop` and nothing else (no `strategy`, no parent link), which is what the
backend in this container returns.
"""
from __future__ import annotations

import re

import pytest

from codeintel.engines.graph.provider import GraphProvider

ROOT = "/repos/my-app"
P = "my-app"  # the backend's project id; it prefixes every qualified name

LIST_PROJECTS = {"projects": [{"name": P, "root_path": ROOT}]}

# The call graph the stub backend holds. `main` reaches `validate` in three hops through `load` and
# `parse`; `orphan` is indexed but nothing `main` reaches calls it.
EDGES = [
    (f"{P}.src.app.main", f"{P}.src.app.load"),
    (f"{P}.src.app.main", f"{P}.src.app.helper"),
    (f"{P}.src.app.load", f"{P}.src.app.parse"),
    (f"{P}.src.app.helper", f"{P}.src.app.log"),
    (f"{P}.src.app.parse", f"{P}.src.app.validate"),
    # A decoy edge into `validate` from a node `main` never reaches: a path rebuilt from it would
    # not start at A.
    (f"{P}.src.app.orphan", f"{P}.src.app.validate"),
]

NODES = {
    name: {"name": name, "qualified_name": f"{P}.src.app.{name}", "label": "Function",
           "file_path": "src/app.py"}
    for name in ("main", "load", "helper", "parse", "log", "validate", "orphan")
}

# 0.9.0's outbound `trace_path` from `main`: hop numbers, no strategy, no parent links.
TRACE_FROM_MAIN = {
    "function": "main", "direction": "outbound", "mode": "calls",
    "callees": [
        {"name": "load", "qualified_name": f"{P}.src.app.load", "hop": 1},
        {"name": "helper", "qualified_name": f"{P}.src.app.helper", "hop": 1},
        {"name": "parse", "qualified_name": f"{P}.src.app.parse", "hop": 2},
        {"name": "log", "qualified_name": f"{P}.src.app.log", "hop": 2},
        {"name": "validate", "qualified_name": f"{P}.src.app.validate", "hop": 3},
    ],
}

# 0.9.0's `trace_path` for a bare `chain main` (direction both): still no strategy.
TRACE_BOTH = {
    "function": "main", "direction": "both", "mode": "calls",
    "callees": [{"name": "load", "qualified_name": f"{P}.src.app.load", "hop": 1}],
    "callers": [{"name": "cli", "qualified_name": f"{P}.src.cli.cli", "hop": 1}],
}


def _search(payload: dict) -> dict:
    pattern = str(payload.get("name_pattern") or "")
    wanted = pattern[1:-1] if pattern.startswith("^") and pattern.endswith("$") else pattern
    wanted = re.sub(r"\\(.)", r"\1", wanted)
    return {"total": 0, "results": [dict(n) for k, n in NODES.items() if k == wanted],
            "has_more": False}


def _query(payload: dict) -> dict:
    """The edges whose callee is in the query's `b.qualified_name IN [...]` list, like the real
    backend would answer; every edge if the query names no such list."""
    q = str(payload.get("query") or "")
    m = re.search(r"b\.qualified_name IN \[([^\]]*)\]", q)
    callees = set(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))) if m else None
    rows = [[a, b] for a, b in EDGES if callees is None or b in callees]
    return {"columns": ["a.qualified_name", "b.qualified_name"], "rows": rows, "total": len(rows)}


def _provider(monkeypatch, trace=TRACE_FROM_MAIN):
    """A GraphProvider whose `_run` answers like 0.9.0 and records every call it was sent."""
    monkeypatch.setattr("codeintel.engines.graph.provider.shutil.which",
                        lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    calls: list[tuple[str, dict]] = []

    def _run(method, payload, timeout_ms):
        calls.append((method, payload))
        if method == "list_projects":
            return LIST_PROJECTS
        if method == "search_graph":
            return _search(payload)
        if method == "trace_path":
            return trace
        if method == "query_graph":
            return _query(payload)
        return None

    monkeypatch.setattr(p, "_run", _run)
    return p, calls


def _gap_kinds(res: dict) -> list[str]:
    return [g["kind"] for g in res.get("gaps") or []]


def _path_hops(text: str) -> list[tuple[str, str]]:
    """The `caller → callee` pairs listed under `## Path`, in order."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## Path "))
    hops = []
    for ln in lines[start + 1:]:
        if not ln.startswith("- "):
            break
        m = re.match(r"- (\S+) → (\S+) \[hop (\d+)\]", ln)
        assert m, f"a path line is not `caller → callee [hop N]`: {ln!r}"
        hops.append((m.group(1), m.group(2)))
    return hops


def test_a_reachable_destination_gives_the_path_with_its_hops_in_order(monkeypatch):
    p, _ = _provider(monkeypatch)
    res = p.build_result("chain", "main -> validate", [], 5000, ROOT)
    text = res["result"]
    assert text is not None, res
    assert "## Path main → validate" in text
    assert _path_hops(text) == [
        ("src.app.main", "src.app.load"),
        ("src.app.load", "src.app.parse"),
        ("src.app.parse", "src.app.validate"),
    ]
    # The bare-symbol neighbourhood is not what was asked for and must not be dressed as the path.
    assert "Callers (upstream)" not in text
    assert "src.app.helper" not in text and "src.app.orphan" not in text


def test_an_unreachable_destination_is_no_path_found_and_partial(monkeypatch):
    p, _ = _provider(monkeypatch)
    res = p.build_result("chain", "main->orphan", [], 5000, ROOT)
    text = res["result"]
    assert text is not None, res
    assert ("No call path from main to orphan within 5 hops "
            "(this is not proof none exists)") in text
    assert "## Path" not in text
    assert "no-path-found" in _gap_kinds(res)
    assert res["confidence"] == "partial"


def test_a_missing_destination_is_target_not_found(monkeypatch):
    p, calls = _provider(monkeypatch)
    res = p.build_result("chain", "main->nope", [], 5000, ROOT)
    assert res["result"] is None
    assert res["reason"] == "target-not-found"
    assert res["outcome"] == "not_found"
    assert "nope" in res["hint"]
    # Nothing was traced for a destination that does not exist.
    assert "trace_path" not in [m for m, _ in calls]


def test_a_missing_source_is_target_not_found_too(monkeypatch):
    p, calls = _provider(monkeypatch)
    res = p.build_result("chain", "nope->validate", [], 5000, ROOT)
    assert res["result"] is None
    assert res["reason"] == "target-not-found"
    assert "nope" in res["hint"]
    assert "trace_path" not in [m for m, _ in calls]


def test_a_destination_hint_narrows_which_symbol_b_is(monkeypatch):
    """`name@file` on B is codeintel's own disambiguator, exactly as it is on A."""
    p, _ = _provider(monkeypatch)
    res = p.build_result("chain", "main->validate@src/other.py", [], 5000, ROOT)
    assert res["result"] is None
    assert res["reason"] == "target-not-found"


def test_an_ambiguous_destination_lists_its_candidates_and_claims_no_path(monkeypatch):
    p, calls = _provider(monkeypatch)
    twin = {"name": "validate", "qualified_name": f"{P}.src.lib.validate", "label": "Function",
            "file_path": "src/lib.py"}
    real_search = _search

    def _two(payload):
        out = real_search(payload)
        if out["results"] and out["results"][0]["name"] == "validate":
            out["results"].append(dict(twin))
        return out

    monkeypatch.setattr(f"{__name__}._search", _two)
    res = p.build_result("chain", "main->validate", [], 5000, ROOT)
    text = res["result"]
    assert text is not None, res
    assert "Ambiguous" in text
    assert "src.app.validate" in text and "src.lib.validate" in text
    assert "## Path" not in text
    assert "trace_path" not in [m for m, _ in calls]


def test_hops_without_a_strategy_print_unstated_and_add_one_gap(monkeypatch):
    p, _ = _provider(monkeypatch)
    res = p.build_result("chain", "main->validate", [], 5000, ROOT)
    text = res["result"]
    path_lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert path_lines and all("[unstated]" in ln for ln in path_lines), text
    assert _gap_kinds(res).count("hop-resolution-unstated") == 1
    assert res["confidence"] == "partial"


def test_a_bare_chain_also_labels_unstated_hops(monkeypatch):
    p, _ = _provider(monkeypatch, trace=TRACE_BOTH)
    res = p.build_result("chain", "main", [], 5000, ROOT)
    text = res["result"]
    assert "src.app.load [hop 1] [unstated]" in text
    assert "src.cli.cli [hop 1] [unstated]" in text
    assert _gap_kinds(res).count("hop-resolution-unstated") == 1
    assert res["confidence"] == "partial"


def test_a_hop_with_a_strategy_is_not_unstated(monkeypatch):
    """0.10.x hops carry `strategy`; those keep their own label and raise no unstated gap."""
    labelled = {
        **TRACE_FROM_MAIN,
        "callees": [{**h, "strategy": "lsp", "confidence": 0.9}
                    for h in TRACE_FROM_MAIN["callees"]],
    }
    p, _ = _provider(monkeypatch, trace=labelled)
    res = p.build_result("chain", "main->validate", [], 5000, ROOT)
    text = res["result"]
    assert "[unstated]" not in text
    assert "[lsp]" in text
    assert "hop-resolution-unstated" not in _gap_kinds(res)


def test_the_path_hops_form_a_connected_chain_and_n_is_the_depth_sent(monkeypatch):
    """The verifier for the `## Path A → B` heading and the "within N hops" tally.

    Checked against the stub's own graph and the request it captured, not against the text alone:
    every listed hop is an edge the backend holds, each hop's callee is the next hop's caller, the
    chain runs from A's qualified name to B's, and N is the `depth` sent to `trace_path`."""
    p, calls = _provider(monkeypatch)
    res = p.build_result("chain", "main->validate", [], 5000, ROOT)
    hops = _path_hops(res["result"])
    assert hops
    edges = {(a.removeprefix(f"{P}."), b.removeprefix(f"{P}.")) for a, b in EDGES}
    assert all(h in edges for h in hops), hops
    assert all(hops[i][1] == hops[i + 1][0] for i in range(len(hops) - 1)), hops
    assert hops[0][0] == NODES["main"]["qualified_name"].removeprefix(f"{P}.")
    assert hops[-1][1] == NODES["validate"]["qualified_name"].removeprefix(f"{P}.")

    traced = [payload for m, payload in calls if m == "trace_path"]
    assert len(traced) == 1
    assert traced[0]["direction"] == "outbound"
    assert 1 <= traced[0]["depth"] <= 5
    assert len(hops) <= traced[0]["depth"]

    p, calls = _provider(monkeypatch)
    res = p.build_result("chain", "main->orphan", [], 5000, ROOT)
    sent = next(payload for m, payload in calls if m == "trace_path")["depth"]
    m = re.search(r"within (\d+) hops", res["result"])
    assert m and int(m.group(1)) == sent


def test_a_path_the_hops_cannot_rebuild_is_no_path(monkeypatch):
    """0.9.x hops have no parent links. When the edges between them do not connect A to B, the
    answer is no path, never a guessed chain."""
    p, _ = _provider(monkeypatch)
    monkeypatch.setattr(f"{__name__}.EDGES", [e for e in EDGES if e[1] != f"{P}.src.app.parse"])
    res = p.build_result("chain", "main->validate", [], 5000, ROOT)
    text = res["result"]
    assert "## Path" not in text
    assert "No call path from main to validate within 5 hops" in text
    assert "no-path-found" in _gap_kinds(res)


@pytest.mark.parametrize("target", ["main->", "->validate"])
def test_an_empty_side_of_the_arrow_is_refused_and_says_which(monkeypatch, target):
    p, calls = _provider(monkeypatch)
    res = p.build_result("chain", target, [], 5000, ROOT)
    assert res["result"] is None
    assert res["reason"] == "no-target"
    assert "->" in res["hint"]
    assert "trace_path" not in [m for m, _ in calls]


def test_a_path_hop_is_labelled_by_its_own_edges_strategy(monkeypatch):
    """`query_graph` edges carry a `strategy` even on 0.9.x, and it is the resolution of THAT hop;
    the traced node's (absent here) is only the fallback."""
    p, _ = _provider(monkeypatch)
    real_query = _query

    def _labelled(payload):
        out = real_query(payload)
        out["columns"] = [*out["columns"], "strategy"]
        out["rows"] = [[*r, "lsp_callable_alias"] for r in out["rows"]]
        return out

    monkeypatch.setattr(f"{__name__}._query", _labelled)
    res = p.build_result("chain", "main->validate", [], 5000, ROOT)
    path_lines = [ln for ln in res["result"].splitlines() if ln.startswith("- ")]
    assert len(path_lines) == 3 and all(ln.endswith("[lsp]") for ln in path_lines), res["result"]
    assert "hop-resolution-unstated" not in _gap_kinds(res)
