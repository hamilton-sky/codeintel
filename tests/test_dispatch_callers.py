"""`callers` of a method that is only ever CALLED through the type that declares it.

The defect this file exists against: `codeintel query --op callers --target GraphProvider.build_result`
listed the tests that construct a `GraphProvider` and none of the three production call sites. Those
three — `Gateway._dispatch_single`, `Gateway._query`, `MapGenerator.generate` — are in the graph, but
on a DIFFERENT node: they call `provider.build_result` where `provider: CodeProvider`, so the backend
bound them to the Protocol's declaration, and no provider inherits the Protocol (it is structural
typing), so no INHERITS edge joined the two. `callers LspProvider.build_result` was worse: its answer
was `confidence: complete` and `safe_for_destructive: true` over forty-four tests, while every
production caller of the method sat on the other node — the reading that ends in a deletion.

The backend here is a small in-memory graph that answers every query shape the edge ops and the
dispatch lookup send, and OBEYS `LIMIT` and `IN [...]`. A stub that returns the same rows to every
question cannot show this defect: the whole point is that the extra questions are asked, and what
they are answered decides what the answer claims.

Where a test says CONTROL it is the same graph with the one fact that should make no difference
changed, asserted in the same test as the positive half — a control that cannot fail before the fix
proves nothing on its own, so each is paired with an assertion that can.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from codeintel.graph_dispatch import (
    _MAX_BASES,
    _MAX_DEPTH,
    _base_names,
    _leaf,
)
from codeintel.outcome import Missing
from codeintel.providers.graph import GraphProvider
from tests.test_summary_integrity import _headline_disagreements

ROOT = "/Users/x/Documents/project/codeintel"
PROJECT = "tmp-proj"       # hyphenated, like a path-slug registration, so the prefix is stripped


# ------------------------------------------------------------------------------------ the backend

class _Graph:
    """Classes, methods and call edges in memory, answering the queries `graph_dispatch.py` sends."""

    def __init__(self) -> None:
        self.classes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.fail_when: Callable[[str], bool] | None = None
        self.raise_when: Callable[[str], bool] | None = None
        self.on_query: Callable[[str, int], None] | None = None
        self.timeouts: list[int] = []
        self.provider: GraphProvider | None = None

    # -- building

    def add_class(self, name: str, *, file: str, bases: tuple[str, ...] | list[str] = (),
                  methods: tuple[str, ...] | list[str] = (),
                  inherits: tuple[str, ...] | list[str] = (),
                  decorators: dict[str, list[str]] | None = None) -> str:
        qn = f"{PROJECT}.{file[:-3].replace('/', '.')}.{name}"
        self.classes[qn] = {"name": name, "file": file, "bases": list(bases),
                            "methods": list(methods), "inherits": list(inherits),
                            "decorators": dict(decorators or {})}
        return qn

    def call(self, caller: str, caller_file: str, callee: str, callee_file: str, *,
             caller_label: str = "Function", callee_label: str = "Method",
             strategy: str = "import_map", conf: str = "0.95", text: str | None = None,
             kind: str = "CALLS") -> None:
        """One edge. `caller` is a dotted path WITHOUT the project prefix; `callee` is a full
        qualified name as `add_class` returns it plus the method."""
        name = callee.rsplit(".", 1)[-1]
        self.edges.append({
            "a.name": caller.rsplit(".", 1)[-1], "a.qualified_name": f"{PROJECT}.{caller}",
            "a.file_path": caller_file, "labels(a)": json.dumps([caller_label]),
            "type(c)": kind, "c.confidence": conf, "strategy": strategy,
            "callee": text if text is not None else name,
            "b.name": name, "b.qualified_name": callee, "b.file_path": callee_file,
            "labels(b)": json.dumps([callee_label]),
        })

    # -- answering

    def __call__(self, cypher: str, project: str, timeout_ms: int) -> list[dict]:
        self.queries.append(cypher)
        self.timeouts.append(timeout_ms)
        if self.on_query is not None:
            self.on_query(cypher, timeout_ms)
        if self.raise_when is not None and self.raise_when(cypher):
            raise RuntimeError("the stub backend blew up")
        if self.fail_when is not None and self.fail_when(cypher):
            assert self.provider is not None
            self.provider._backend._last_failure = Missing(  # type: ignore[attr-defined]
                "timeout", "the graph backend did not respond within the time budget")
            return []
        rows = self._answer(cypher)
        match = re.search(r"LIMIT (\d+)\s*$", cypher)
        return rows[: int(match.group(1))] if match else rows

    def _ids(self, cypher: str) -> list[str]:
        listed = re.search(r"IN \[([^\]]*)\]", cypher)
        return re.findall(r'"([^"]*)"', listed.group(1)) if listed else []

    def _class_row(self, qn: str, prefix: str) -> dict[str, Any]:
        c = self.classes[qn]
        return {f"{prefix}.qualified_name": qn, f"{prefix}.name": c["name"],
                f"{prefix}.file_path": c["file"],
                f"{prefix}.base_classes": json.dumps(c["bases"]) if c["bases"] else None}

    def _answer(self, cypher: str) -> list[dict]:
        if "-[c:" in cypher:
            return self._edge_rows(cypher)
        if "[:DEFINES_METHOD]" in cypher:
            by_name = re.search(r'WHERE m\.name="([^"]*)"', cypher)
            if by_name:
                return [{**self._class_row(qn, "c"), "m.qualified_name": f"{qn}.{m}",
                         "m.file_path": c["file"]}
                        for qn, c in self.classes.items() for m in c["methods"]
                        if m == by_name.group(1)]
            if "c.qualified_name IN" in cypher:
                ids = self._ids(cypher)
                return [{"c.qualified_name": qn, "m.name": m,
                         "m.decorators": (json.dumps(c["decorators"][m]) if m in c["decorators"]
                                          else None)}
                        for qn, c in self.classes.items() if qn in ids for m in c["methods"]]
            if "m.qualified_name IN" in cypher:
                ids = self._ids(cypher)
                return [{**self._class_row(qn, "c"), "m.qualified_name": f"{qn}.{m}"}
                        for qn, c in self.classes.items() for m in c["methods"]
                        if f"{qn}.{m}" in ids]
        if "[:INHERITS]" in cypher:
            ids = self._ids(cypher)
            return [{"c.qualified_name": child, **self._class_row(parent, "p")}
                    for child in ids if child in self.classes
                    for parent in self.classes[child]["inherits"]]
        if "MATCH (p:Class) WHERE p.name IN" in cypher:
            names = self._ids(cypher)
            return [self._class_row(qn, "p") for qn, c in self.classes.items() if c["name"] in names]
        if cypher.startswith("MATCH (n) WHERE n.name="):
            name = re.search(r'n\.name="([^"]*)"', cypher).group(1)  # type: ignore[union-attr]
            return [{"n.qualified_name": f"{qn}.{m}", "n.file_path": c["file"]}
                    for qn, c in self.classes.items() for m in c["methods"] if m == name]
        return []

    def _edge_rows(self, cypher: str) -> list[dict]:
        fixed = "b" if 'WHERE b.name="' in cypher else "a"
        name = re.search(rf'WHERE {fixed}\.name="([^"]*)"', cypher).group(1)  # type: ignore[union-attr]
        hits = [e for e in self.edges if e[f"{fixed}.name"] == name]
        files = re.search(rf"AND {fixed}\.file_path IN \[([^\]]*)\]", cypher)
        if files:
            allowed = set(re.findall(r'"([^"]*)"', files.group(1)))
            hits = [e for e in hits if e[f"{fixed}.file_path"] in allowed]
        if "count(*) AS edge_count" in cypher:
            counts: dict[tuple[str, str, str], int] = {}
            for e in hits:
                key = (e[f"{fixed}.name"], e[f"{fixed}.qualified_name"], e[f"{fixed}.file_path"])
                counts[key] = counts.get(key, 0) + 1
            return [{f"{fixed}.name": n, f"{fixed}.qualified_name": q, f"{fixed}.file_path": f,
                     "edge_count": str(c)} for (n, q, f), c in counts.items()]
        return [dict(e) for e in hits]


def _provider(monkeypatch, graph: _Graph, root: str = ROOT) -> GraphProvider:
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    graph.provider = p
    listing = {"projects": [{"name": PROJECT, "root_path": root}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", graph)
    return p


def _ask(monkeypatch, graph: _Graph, op: str, target: str) -> dict[str, Any]:
    return _provider(monkeypatch, graph).build_result(op, target, [], 30000, ROOT)


def _row_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


def _gap_kinds(env: dict) -> list[str]:
    return [g["kind"] for g in env.get("gaps", [])]


def _gap(env: dict, kind: str) -> dict:
    return next(g for g in env["gaps"] if g["kind"] == kind)


def _via_section(body: str) -> str:
    start = body.index("## Callers through")
    nxt = body.find("\n## ", start + 1)
    return body[start:] if nxt == -1 else body[start:nxt]


# ------------------------------------------------------------------------------------- the shapes

def _protocol_graph(*, conforms: bool = True) -> tuple[_Graph, str, str]:
    """`GraphProvider.build_result`, as this repository has it: a Protocol that declares it, a class
    that satisfies the Protocol without inheriting it, two tests that call the class directly, and
    two production callers plus one test that call it through the Protocol."""
    g = _Graph()
    proto = g.add_class("CodeProvider", file="app/provider.py", bases=["Protocol"],
                        methods=["build_result", "probe"])
    impl = g.add_class("GraphProvider", file="app/graph.py",
                       methods=["build_result", "probe", "extra"] if conforms
                       else ["build_result", "extra"])
    # Tests first, then the production callers: the order a backend happens to return them in, so
    # the ranking this asserts is the ranking and not the input order.
    for i in range(2):
        g.call(f"tests.test_graph.test_{i}", "tests/test_graph.py", f"{impl}.build_result",
               "app/graph.py")
    g.call("tests.test_proto.test_through_the_protocol", "tests/test_proto.py",
           f"{proto}.build_result", "app/provider.py", strategy="field_type_hint", conf="0.85",
           text="p.build_result")
    g.call("app.gateway.Gateway.query", "app/gateway.py", f"{proto}.build_result",
           "app/provider.py", caller_label="Method", strategy="field_type_hint", conf="0.85",
           text="provider.build_result")
    g.call("app.mapper.MapGenerator.generate", "app/mapper.py", f"{proto}.build_result",
           "app/provider.py", caller_label="Method", strategy="field_type_hint", conf="0.85",
           text="provider.build_result")
    return g, proto, impl


def _nominal_graph(*, inherits: bool = True) -> tuple[_Graph, str, str]:
    """`Child.run` overriding `Base.run`: one test calls the override, one production function and
    one test call the base."""
    g = _Graph()
    base = g.add_class("Base", file="app/base.py", methods=["run"])
    child = g.add_class("Child", file="app/child.py", bases=["Base"], methods=["run"],
                        inherits=[base] if inherits else [])
    g.call("tests.test_child.test_run", "tests/test_child.py", f"{child}.run", "app/child.py")
    g.call("tests.test_base.test_run", "tests/test_base.py", f"{base}.run", "app/base.py",
           strategy="field_type_hint", conf="0.85")
    g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
           strategy="field_type_hint", conf="0.85", text="runner.run")
    return g, base, child


# ===================================================================== callers through a Protocol

def test_a_concrete_override_lists_the_callers_of_the_protocol_it_satisfies_in_their_own_section(
        monkeypatch):
    """The reproduction. Three call sites are on `CodeProvider.build_result`, and the answer about
    `GraphProvider.build_result` has to put them in front of the reader, apart from the two tests
    that really do call the override, production ahead of the test."""
    graph, _, _ = _protocol_graph()
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    body = env["result"]

    assert ("## Callers through `CodeProvider.build_result` — they call the Protocol; at run time a "
            "call reaches this override only when the object is a `GraphProvider` (3)") in body, body
    lines = _row_lines(_via_section(body))
    assert [ln.split(" [")[0] for ln in lines] == [
        "- app.gateway.Gateway.query", "- app.mapper.MapGenerator.generate",
        "- tests.test_proto.test_through_the_protocol"], lines
    assert all("[?via protocol 0.85]" in ln for ln in lines), lines
    # The symbol's own callers keep their own section and their own count.
    assert "## Callers of GraphProvider.build_result (2)" in body, body
    assert "does not inherit `CodeProvider`" in body and "checked by method NAME and not by signature" in body


def test_the_via_rows_say_which_base_they_called_and_are_never_verified(monkeypatch):
    graph, _, _ = _protocol_graph()
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    rows = env["rows"]
    via = [r for r in rows if r.get("via")]
    direct = [r for r in rows if not r.get("via")]

    assert len(via) == 3 and len(direct) == 2, rows
    for r in via:
        assert r["via"] == "app.provider.CodeProvider.build_result", r
        assert r["via_kind"] == "protocol", r
        assert r["verified"] is False and r["evidence"] == "name-matched", r
        assert r["strategy"] == "field_type_hint" and r["relation"] == "caller", r
        assert "the Protocol method `app.provider.CodeProvider.build_result`" in r["why"], r
        assert "the receiver's declared type (`field_type_hint`)" in r["why"], r
        assert "only by dispatch" in r["why"], r
    assert all(r["verified"] is True and "via" not in r for r in direct), direct


def test_a_via_row_is_never_verified_whatever_strength_of_edge_it_arrived_on(monkeypatch):
    """An `lsp_direct` edge at 0.95 is the strongest claim the backend makes — and it is a claim that
    the call binds to the PROTOCOL. It cannot become a claim about the override, and `why` keeps the
    strategy as the account of how the call reached the base."""
    graph, proto, _ = _protocol_graph()
    for e in graph.edges:
        if e["b.qualified_name"] == f"{proto}.build_result":
            e["strategy"], e["c.confidence"] = "lsp_direct", "0.95"
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    via = [r for r in env["rows"] if r.get("via")]

    assert len(via) == 3
    assert not [r for r in via if r["verified"]], via
    assert all("`lsp_direct`" in r["why"] for r in via), via
    assert env["evidence"]["verified"] == 2, env["evidence"]


def test_a_class_missing_one_protocol_method_contributes_nothing(monkeypatch):
    """Conformance is checked by method NAMES: the Protocol declares `build_result` and `probe`, and a
    class that defines only the first is not a thing a `CodeProvider` can be. CONTROL: the same graph
    with the class defining both."""
    conforming, _, _ = _protocol_graph(conforms=True)
    assert "## Callers through" in _ask(
        monkeypatch, conforming, "callers", "GraphProvider.build_result")["result"]

    graph, _, _ = _protocol_graph(conforms=False)
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    assert "## Callers through" not in env["result"], env["result"]
    assert env["confidence"] == "complete" and env["evidence"]["safe_for_destructive"] is True, env
    assert not [r for r in env["rows"] if r.get("via")]


def test_a_missing_protocol_method_is_undecided_not_ruled_out_when_a_base_cannot_be_resolved(
        monkeypatch):
    """The class defines no `probe`, but it inherits from a class this index does not hold — and an
    unindexed base is exactly where `probe` could come from. "Does not conform" would be a claim
    about the program; the honest sentence is "undecided", it is a gap, and the Protocol's callers —
    the ones that reach the method if the class does conform — are listed as undecided."""
    graph, _, impl = _protocol_graph(conforms=False)
    graph.classes[impl]["bases"] = ["ExternalBase"]
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    gap = _gap(env, "dispatch-bases-incomplete")
    assert "missing: probe" in gap["detail"] and "`ExternalBase`" in gap["detail"], gap
    assert "undecided" in gap["detail"], gap
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False
    assert "fell short" in env["result"] and "is unknown here, not absent" in env["result"]
    via = [r for r in env["rows"] if r.get("via")]
    assert len(via) == 3 and {r["via_kind"] for r in via} == {"protocol-undecided"}, via
    assert "UNDECIDED" in _via_section(env["result"])
    assert env["evidence"]["truncated"] is False, "the rows are listed: nothing was left uncounted"


def test_a_protocol_the_class_inherits_is_a_base_not_a_structural_match(monkeypatch):
    """`class Impl(CodeProvider)` is declared, not inferred, so it is found through the hierarchy and
    the answer says INHERITS — it does not claim the weaker structural reading."""
    graph, proto, impl = _protocol_graph()
    graph.classes[impl].update(bases=["CodeProvider"], inherits=[proto])
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    section = _via_section(env["result"])
    assert "`GraphProvider` inherits `CodeProvider` (INHERITS edges in the index)." in section
    assert "by structure" not in section
    assert {r["via_kind"] for r in env["rows"] if r.get("via")} == {"protocol"}


# ========================================================================== callers through a base

def test_a_base_class_is_found_through_inherits_and_its_callers_are_listed_apart(monkeypatch):
    graph, _, _ = _nominal_graph(inherits=True)
    env = _ask(monkeypatch, graph, "callers", "Child.run")
    body = env["result"]

    assert ("## Callers through `Base.run` — they call the base class; at run time a call reaches "
            "this override only when the object is a `Child` (2)") in body, body
    assert "`Child` inherits `Base` (INHERITS edges in the index)." in body
    via = [r for r in env["rows"] if r.get("via")]
    assert [r["name"] for r in via] == ["drive", "test_run"], via     # production first
    assert {r["via_kind"] for r in via} == {"base"} and not any(r["verified"] for r in via)


def test_a_base_is_resolved_through_the_name_its_statement_wrote_when_the_index_has_no_inherits_edge(
        monkeypatch):
    """The index recorded `base_classes: ["Base"]` and no INHERITS edge. Resolving the name is a
    weaker claim than an edge, and the answer says which it used."""
    graph, _, _ = _nominal_graph(inherits=False)
    env = _ask(monkeypatch, graph, "callers", "Child.run")
    body = env["result"]

    assert "## Callers through `Base.run` — they call the base class" in body, body
    assert ("found by resolving the base-class NAME its statement wrote — the index holds no "
            "INHERITS edge for it") in body, body
    assert "INHERITS edges in the index" not in body
    # The heading is three screens above a row an agent reads alone, so the row says it too.
    via = [r for r in env["rows"] if r.get("via")]
    assert via and all("resolving the base-class NAME its statement wrote" in r["why"] for r in via), via
    # CONTROL: with the edge, the row does not claim a name match.
    wired, _, _ = _nominal_graph(inherits=True)
    assert not any("base-class NAME" in r["why"] for r in _ask(
        monkeypatch, wired, "callers", "Child.run")["rows"]), "an INHERITS-backed link is not a name match"


def test_an_ambiguous_base_name_is_not_guessed_but_a_base_in_the_same_file_settles_it(monkeypatch):
    """Two classes are called `Base`. Binding the child to the wrong one would hand it the wrong
    class's callers, so a name that does not settle it stays unresolved — unless one of the two is
    defined in the child's OWN file, which is where a bare name in a class statement looks first."""
    def shaped(child_file: str) -> _Graph:
        g = _Graph()
        base = g.add_class("Base", file="app/base.py", methods=["run"])
        g.add_class("Base", file="app/other_base.py", methods=["run"])
        child = g.add_class("Child", file=child_file, bases=["Base"], methods=["run"])
        g.call("tests.t.test_child", "tests/t.py", f"{child}.run", child_file)
        g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
               strategy="field_type_hint", conf="0.85")
        return g

    same_file = _ask(monkeypatch, shaped("app/base.py"), "callers", "Child.run")
    assert "## Callers through `Base.run`" in same_file["result"], same_file["result"]
    assert [r["name"] for r in same_file["rows"] if r.get("via")] == ["drive"]

    ambiguous = _ask(monkeypatch, shaped("app/child.py"), "callers", "Child.run")
    assert "## Callers through" not in ambiguous["result"], ambiguous["result"]


def test_the_ancestry_is_followed_to_the_depth_cap_and_a_longer_one_is_a_gap(monkeypatch):
    """`m` is defined on the top of a chain of classes. Within `_MAX_DEPTH` levels it is found and
    nothing is said; one level past it the walk stops with classes still to expand, and the answer
    says the ancestry was not followed to the end — a base past the cap is unknown, not absent."""
    def chain(length: int, defines_at: int) -> _Graph:
        g = _Graph()
        names = [f"K{i}" for i in range(length + 1)]
        qns = [g.add_class(n, file=f"app/{n.lower()}.py", methods=["m"] if i in (0, defines_at) else [])
               for i, n in enumerate(names)]
        for i in range(length):
            g.classes[qns[i]]["bases"] = [names[i + 1]]
            g.classes[qns[i]]["inherits"] = [qns[i + 1]]
        g.call("tests.t.test_m", "tests/t.py", f"{qns[defines_at]}.m", f"app/{names[defines_at].lower()}.py")
        g.call("tests.t.test_k0", "tests/t.py", f"{qns[0]}.m", "app/k0.py")
        return g

    within = _ask(monkeypatch, chain(_MAX_DEPTH, _MAX_DEPTH), "callers", "K0.m")
    assert f"## Callers through `K{_MAX_DEPTH}.m`" in within["result"], within["result"]
    assert "dispatch-bases-incomplete" not in _gap_kinds(within), within.get("gaps")

    beyond = _ask(monkeypatch, chain(_MAX_DEPTH + 1, _MAX_DEPTH + 1), "callers", "K0.m")
    assert "## Callers through" not in beyond["result"], beyond["result"]
    gap = _gap(beyond, "dispatch-bases-incomplete")
    assert f"deeper than {_MAX_DEPTH} levels" in gap["detail"], gap
    assert beyond["confidence"] == "partial" and beyond["evidence"]["safe_for_destructive"] is False


def test_no_more_than_the_base_cap_is_asked_about_and_the_rest_are_counted(monkeypatch):
    extra = 3
    g = _Graph()
    own = g.add_class("Impl", file="app/impl.py", methods=["m"])
    for i in range(_MAX_BASES + extra):
        proto = g.add_class(f"P{i:02d}", file=f"app/p{i:02d}.py", bases=["Protocol"], methods=["m"])
        g.call(f"app.use{i}.use", f"app/use{i}.py", f"{proto}.m", f"app/p{i:02d}.py",
               strategy="field_type_hint", conf="0.85")
    g.call("tests.t.test_m", "tests/t.py", f"{own}.m", "app/impl.py")

    env = _ask(monkeypatch, g, "callers", "Impl.m")
    sections = re.findall(r"^## Callers through `([^`]+)`", env["result"], re.M)

    assert len(sections) == _MAX_BASES, sections
    assert f"{extra} further base method(s) were not asked about" in _gap(
        env, "dispatch-bases-incomplete")["detail"]
    assert env["confidence"] == "partial"
    assert env["evidence"]["truncated"] is True and env["evidence"]["total"] is None, env["evidence"]


# =================================================================== when the lookup cannot answer

_FAILURES = {
    "the lookup of the classes defining the method": lambda q: 'WHERE m.name="build_result"' in q,
    "the check of the Protocol's methods": lambda q: "c.qualified_name IN" in q and "m.name" in q,
}


@pytest.mark.parametrize("what", sorted(_FAILURES))
def test_a_backend_failure_on_a_dispatch_lookup_is_a_named_gap_and_not_a_silent_miss(
        monkeypatch, what):
    """A failed lookup answers `[]` exactly as an empty one does, and "this method has no bases" is
    the reading that deletes a method with production callers. So the failure is read, named, and
    makes the answer partial — while the answer about the symbol's OWN callers is still given."""
    graph, _, _ = _protocol_graph()
    graph.fail_when = _FAILURES[what]
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    assert "## Callers of GraphProvider.build_result (2)" in env["result"], env["result"]
    gap = _gap(env, "dispatch-bases-incomplete")
    assert "did not complete" in gap["detail"] or "could not be checked" in gap["detail"], gap
    assert "the graph backend did not respond" in gap["detail"], gap
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False


def test_a_failure_while_reading_the_class_hierarchy_is_a_named_gap(monkeypatch):
    graph, _, _ = _nominal_graph(inherits=True)
    graph.fail_when = lambda q: "[:INHERITS]" in q
    env = _ask(monkeypatch, graph, "callers", "Child.run")

    assert "## Callers through" not in env["result"]
    gap = _gap(env, "dispatch-bases-incomplete")
    assert "class hierarchy could not be read completely" in gap["detail"], gap
    assert "bases past the failure are unknown" in gap["detail"], gap
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False


def test_a_failure_fetching_the_callers_of_a_base_says_which_base(monkeypatch):
    """The lookup finds the base and then cannot ask who calls it. The base is named, because the
    callers of THAT method are what is unknown."""
    graph, _, _ = _protocol_graph()
    seen = {"edge_queries": 0}

    def fail_second_edge_query(cypher: str) -> bool:
        if "-[c:" in cypher:
            seen["edge_queries"] += 1
            return seen["edge_queries"] >= 2
        return False

    graph.fail_when = fail_second_edge_query
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    gap = _gap(env, "dispatch-bases-incomplete")
    assert "the callers of `CodeProvider.build_result` could not be fetched" in gap["detail"], gap
    assert "## Callers through" not in env["result"]
    assert env["confidence"] == "partial"
    # Callers that were never counted make the total unknown: an exact total beside a gap that says
    # some callers are missing is the envelope contradicting itself.
    assert env["evidence"]["truncated"] is True and env["evidence"]["total"] is None, env["evidence"]


def test_a_lookup_that_raises_is_a_named_gap_and_never_an_exception(monkeypatch):
    graph, _, _ = _protocol_graph()
    graph.raise_when = lambda q: 'WHERE m.name="build_result"' in q
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    assert env["ok"] is True and env["result"] is not None, env
    assert "the lookup raised unexpectedly" in _gap(env, "dispatch-bases-incomplete")["detail"]
    assert env["confidence"] == "partial"


def test_a_fault_in_the_dispatch_code_cannot_take_the_direct_answer_down(monkeypatch):
    """The lookup is an addition to an answer that is already whole. If the code that does it raises,
    the symbol's own callers are still the answer — and the reader is told the bases were not looked
    up, rather than shown a list that reads as complete. CONTROL: the same graph with the code intact."""
    graph, _, _ = _protocol_graph()
    assert "## Callers through" in _ask(
        monkeypatch, graph, "callers", "GraphProvider.build_result")["result"]

    def broken(self, *a, **k):
        raise RuntimeError("a bug in the dispatch lookup")

    monkeypatch.setattr("codeintel.graph_dispatch.DispatchCallers._dispatch_bases", broken)
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    assert env["ok"] is True and env.get("reason") is None, env
    assert "## Callers of GraphProvider.build_result (2)" in env["result"], env["result"]
    assert "the lookup of the bases raised unexpectedly" in _gap(env, "dispatch-bases-incomplete")["detail"]
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False


def test_a_fault_in_the_self_call_rule_leaves_the_rows_as_the_backend_labelled_them(monkeypatch):
    graph, _, _ = _self_graph()
    assert _ask(monkeypatch, graph, "callers", "Mixin.render")["rows"][0]["verified"] is True

    def broken(self, *a, **k):
        raise RuntimeError("a bug in the hierarchy walk")

    monkeypatch.setattr("codeintel.graph_dispatch.DispatchCallers._grow_hierarchy", broken)
    env = _ask(monkeypatch, graph, "callers", "Mixin.render")

    assert env["result"] is not None and env["rows"][0]["verified"] is False, env
    assert env["rows"][0]["strategy"] == "unique_name"


def test_a_lookup_cut_at_its_row_limit_is_a_gap_because_the_target_may_be_past_it(monkeypatch):
    from codeintel.graph_dispatch import _LOOKUP_ROWS

    graph, _, _ = _protocol_graph()
    for i in range(_LOOKUP_ROWS):
        graph.add_class(f"Other{i}", file=f"app/other{i}.py", methods=["build_result"])
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    gap = _gap(env, "dispatch-bases-incomplete")
    assert f"{_LOOKUP_ROWS} or more classes define a method called `build_result`" in gap["detail"], gap
    assert env["confidence"] == "partial"


def test_a_hierarchy_lookup_at_its_row_limit_names_the_limit_it_hit(monkeypatch):
    """`_LOOKUP_ROWS` is stated in the sentence because it is the number that decides whether the
    hierarchy can be trusted; a message that named another number would be a summary of nothing."""
    from codeintel.graph_dispatch import _LOOKUP_ROWS

    graph, _, child = _nominal_graph(inherits=True)
    real = graph.__class__._answer

    def flooded(self, cypher: str) -> list[dict]:
        if "[:INHERITS]" in cypher:
            return [{"c.qualified_name": child, **self._class_row(child, "p")}] * _LOOKUP_ROWS
        return real(self, cypher)

    monkeypatch.setattr(graph.__class__, "_answer", flooded)
    env = _ask(monkeypatch, graph, "callers", "Child.run")

    gap = _gap(env, "dispatch-bases-incomplete")
    assert f"the INHERITS lookup returned its maximum of {_LOOKUP_ROWS} rows" in gap["detail"], gap


def test_a_base_name_lookup_at_its_row_limit_names_the_limit_it_hit(monkeypatch):
    """The name lookup runs only for a base the INHERITS edges did not account for, so the graph here
    has none, and it is flooded: a name that may have resolved to the wrong class is not resolved."""
    from codeintel.graph_dispatch import _LOOKUP_ROWS

    graph, _, _ = _nominal_graph(inherits=False)
    real = graph.__class__._answer

    def flooded(self, cypher: str) -> list[dict]:
        if "MATCH (p:Class) WHERE p.name IN" in cypher:
            return [self._class_row(next(iter(self.classes)), "p")] * _LOOKUP_ROWS
        return real(self, cypher)

    monkeypatch.setattr(graph.__class__, "_answer", flooded)
    env = _ask(monkeypatch, graph, "callers", "Child.run")

    gap = _gap(env, "dispatch-bases-incomplete")
    assert f"the lookup of base-class names returned its maximum of {_LOOKUP_ROWS} rows" in gap["detail"], gap
    assert "## Callers through" not in env["result"]


def test_a_method_listing_at_its_row_limit_names_the_limit_it_hit(monkeypatch):
    from codeintel.graph_dispatch import _MEMBER_ROWS

    graph, _, _ = _protocol_graph()
    real = graph.__class__._answer

    def flooded(self, cypher: str) -> list[dict]:
        if "c.qualified_name IN" in cypher and "m.name" in cypher:
            return [{"c.qualified_name": "x", "m.name": f"m{i}"} for i in range(_MEMBER_ROWS)]
        return real(self, cypher)

    monkeypatch.setattr(graph.__class__, "_answer", flooded)
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    gap = _gap(env, "dispatch-bases-incomplete")
    assert f"the method listing reached its limit of {_MEMBER_ROWS} rows" in gap["detail"], gap


# ============================================================ what the envelope owes the reader

def test_the_envelope_counts_the_via_rows_with_the_possible_ones_and_is_partial_and_unsafe(
        monkeypatch):
    graph, _, _ = _protocol_graph()
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    ev, rows, body = env["evidence"], env["rows"], env["result"]

    assert (ev["verified"], ev["possible"], ev["unstated"]) == (2, 3, 0), ev
    assert ev["verified"] + ev["possible"] + ev["unstated"] == ev["returned"] == len(rows) == 5
    assert len(_row_lines(body)) == ev["returned"], "the body must print exactly the rows it publishes"
    assert ev["total"] == 5 and ev["truncated"] is False, ev
    assert ev["safe_for_destructive"] is False
    assert env["confidence"] == "partial" and env["evidence_class"] == "advisory"
    gap = _gap(env, "callers-via-base")
    assert "`CodeProvider.build_result` (protocol, 3)" in gap["detail"], gap
    assert "never as verified callers of this symbol" in gap["detail"], gap
    assert gap["section"] == "callers"
    # A caller may call the symbol directly AND a base; "only through a base" said it could not.
    assert "only through" not in gap["detail"], gap
    assert "also calls the symbol directly" in gap["detail"], gap


def test_the_first_screen_says_how_many_of_the_possible_callers_called_a_base(monkeypatch):
    graph, _, _ = _protocol_graph()
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    banner = [ln for ln in env["result"].splitlines() if ln.startswith("> ")]

    assert banner[0] == "> **Confidence: partial**", banner
    assert "> Verified callers: 2 · possible: 3 · unstated: 0" in banner, banner
    assert ("> Of the possible callers, 3 call a base type or Protocol rather than this symbol, "
            "and reach it only by dispatch") in banner, banner
    assert banner[-1] == "> Safe for destructive decisions: **no**", banner
    assert not [ln for ln in banner if ln.startswith("- ")]


def test_every_via_heading_counts_the_rows_beneath_it(monkeypatch):
    """The census's aggregate rule, applied to the new heading: `(3)` above three lines. Checked on a
    section that mixes a call with a registration, where the heading has to split the way the direct
    one does."""
    graph, proto, _ = _protocol_graph()
    graph.call("app.registry.register", "app/registry.py", f"{proto}.build_result",
               "app/provider.py", kind="CALL_REFERENCE", strategy="field_type_hint", conf="0.85")
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    body = env["result"]

    assert not _headline_disagreements(body), (_headline_disagreements(body), body)
    assert "(3 direct, 1 other reference(s))" in _via_section(body).splitlines()[0], body


def test_the_via_rows_do_not_feed_the_collision_signature_of_the_direct_answer(monkeypatch):
    """Six callers resolved by suffix match are a guess about the PROTOCOL's method. Counted into
    the direct list they would trip `all-rows-name-resolved` — the signature the gateway escalates
    on — and badge rows that have nothing to do with them."""
    graph, proto, impl = _protocol_graph()
    for i in range(6):
        graph.call(f"tests.t{i}.test", f"tests/t{i}.py", f"{proto}.build_result", "app/provider.py",
                   strategy="suffix_match", conf="0.30")
        graph.call(f"tests.d{i}.test", f"tests/d{i}.py", f"{impl}.build_result", "app/graph.py")
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    assert _gap_kinds(env) == ["callers-via-base"], env["gaps"]
    direct = env["result"][: env["result"].index("## Callers through")]
    assert "[?" not in direct, direct


def test_the_via_section_is_capped_and_ranked_like_every_other_caller_list(monkeypatch):
    """Sixty tests and three production callers, tests first on the way in: the three are on the first
    screen, fifty rows are printed, and what the cap dropped is counted with its test/production
    split and raised as the cap gap that names the base."""
    graph, proto, _ = _protocol_graph()
    graph.edges = [e for e in graph.edges if e["b.qualified_name"] != f"{proto}.build_result"]
    for i in range(60):
        graph.call(f"tests.t{i}.test", f"tests/t{i}.py", f"{proto}.build_result", "app/provider.py",
                   strategy="field_type_hint", conf="0.85")
    for i in range(3):
        graph.call(f"app.p{i}.use", f"app/p{i}.py", f"{proto}.build_result", "app/provider.py",
                   strategy="field_type_hint", conf="0.85")
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    section = _via_section(env["result"])
    lines = _row_lines(section)

    assert len(lines) == 50 and all("app.p" in ln for ln in lines[:3]), lines[:4]
    assert "(50)" in section.splitlines()[0]
    assert "_Truncated: 50 of 63 distinct callers are shown, and 13 are not (13 in test files, 0 in "\
           "production code)." in env["result"]
    cap = [g for g in env["gaps"] if g["kind"] == "row-cap-reached"]
    assert len(cap) == 1 and "not a complete answer for `CodeProvider.build_result`" in cap[0]["detail"], cap
    ev = env["evidence"]
    assert ev["returned"] == 52 and ev["total"] == 65 and ev["truncated"] is True, ev
    assert ev["verified"] + ev["possible"] + ev["unstated"] == ev["returned"] == len(env["rows"])


def test_impact_carries_the_via_section_in_its_callers_half_and_one_banner(monkeypatch):
    graph, _, _ = _protocol_graph()
    env = _ask(monkeypatch, graph, "impact", "GraphProvider.build_result")
    body, ev = env["result"], env["evidence"]

    assert body.count("## Callers through") == 1, body
    assert body.count("> Safe for destructive decisions:") == 1
    assert len([r for r in env["rows"] if r.get("via")]) == 3
    assert ev["returned"] == len(_row_lines(body)) == len(env["rows"]), ev
    assert env["confidence"] == "partial" and "callers-via-base" in _gap_kinds(env)

    context = _ask(monkeypatch, graph, "context", "GraphProvider.build_result")
    assert context["result"].count("## Callers through") == 1


# ===================================================== callers with no direct caller at all

def test_an_override_with_no_direct_callers_still_lists_the_callers_of_its_base(monkeypatch):
    """The canonical polymorphic case: a strategy only ever called through the interface it
    implements. "No edge — not proof of dead code" was the most this op could say about it, and the
    callers it needs to tell the reader about were on the Protocol the whole time."""
    graph, _, impl = _protocol_graph()
    graph.edges = [e for e in graph.edges if e["b.qualified_name"] != f"{impl}.build_result"]
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    body = env["result"]

    assert body is not None, env
    assert "## Callers of GraphProvider.build_result (0)" in body, body
    assert "every caller below was written against a base type" in body
    assert len([r for r in env["rows"] if r.get("via")]) == 3 and env["evidence"]["verified"] == 0
    assert not _headline_disagreements(body), _headline_disagreements(body)
    assert env["confidence"] == "partial" and "callers-via-base" in _gap_kinds(env)
    assert env["evidence"]["safe_for_destructive"] is False


def test_an_override_nothing_calls_directly_or_through_a_base_is_still_no_edges_with_no_extra_gap(
        monkeypatch):
    """CONTROL for the answer above: remove the Protocol's callers as well and the answer is exactly
    what it was before — a safe-null that says the symbol is indexed and has no edge — and the
    lookup that found nothing leaves nothing behind in the gap list."""
    graph, _, impl = _protocol_graph()
    graph.edges = [e for e in graph.edges if e["b.qualified_name"] != f"{impl}.build_result"]
    assert _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")["result"] is not None

    graph.edges = []
    p = _provider(monkeypatch, graph)
    env = p.build_result("callers", "GraphProvider.build_result", [], 30000, ROOT)

    assert env["result"] is None and env["reason"] == "no-edges", env
    assert p._pending_gaps == (), p._pending_gaps
    assert any("DEFINES_METHOD" in q for q in graph.queries), "the bases were never looked up"


def test_a_failed_direct_query_is_reported_as_the_failure_it_is_and_not_answered_from_a_base(
        monkeypatch):
    """If the query for the symbol's own callers did not return, "no direct callers" is not a fact
    and the answer must not be built on it. `build_result` reports the failure."""
    healthy, _, _ = _protocol_graph()
    assert "## Callers through" in _ask(
        monkeypatch, healthy, "callers", "GraphProvider.build_result")["result"]

    graph, _, _ = _protocol_graph()
    graph.fail_when = lambda q: "-[c:" in q
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    assert env["result"] is None and env["reason"] == "timeout", env
    assert not any("DEFINES_METHOD" in q for q in graph.queries)


# ============================================================ what must not change

def test_a_method_with_no_bases_is_asked_once_and_answers_exactly_as_it_did(monkeypatch):
    """No class above it, no Protocol that declares its name: one question (which classes define a
    method of this name), nothing added to the answer. CONTROL: the same edges with the target
    labelled a function never ask it, and the two answers are identical byte for byte."""
    def widget(label: str) -> _Graph:
        g = _Graph()
        w = g.add_class("Widget", file="app/widget.py", methods=["spin"])
        for i in range(3):
            g.call(f"app.use{i}.go", f"app/use{i}.py", f"{w}.spin", "app/widget.py",
                   callee_label=label)
        g.call("app.odd.go", "app/odd.py", f"{w}.spin", "app/widget.py", callee_label=label,
               strategy="unique_name", conf="0.75")
        return g

    method_graph, function_graph = widget("Method"), widget("Function")
    as_method = _ask(monkeypatch, method_graph, "callers", "Widget.spin")
    as_function = _ask(monkeypatch, function_graph, "callers", "Widget.spin")

    assert any("DEFINES_METHOD" in q for q in method_graph.queries), "a method's bases are asked about"
    assert not any("DEFINES_METHOD" in q for q in function_graph.queries), "a function has none"
    for key in ("result", "rows", "evidence", "gaps", "confidence", "evidence_class"):
        assert as_method.get(key) == as_function.get(key), key
    assert "## Callers through" not in as_method["result"]


def test_callers_of_the_protocol_method_itself_are_unchanged(monkeypatch):
    """Asked about the declaration, the answer is its own direct callers: nothing is "through" a
    base of the Protocol. The lookup runs (a method's bases are always asked about) and finds none."""
    graph, _, _ = _protocol_graph()
    env = _ask(monkeypatch, graph, "callers", "CodeProvider.build_result")

    assert any("DEFINES_METHOD" in q for q in graph.queries)
    assert "## Callers through" not in env["result"], env["result"]
    assert "## Callers of CodeProvider.build_result (3)" in env["result"]
    assert env["confidence"] == "complete" and env["evidence"]["safe_for_destructive"] is True, env
    assert sorted(r["name"] for r in env["rows"]) == ["generate", "query", "test_through_the_protocol"]


def test_a_constructor_is_never_looked_up_through_a_base(monkeypatch):
    """A constructor is chosen by naming the class, so a call to a base's `__init__` is never a call
    that could have reached the subclass's. CONTROL: the same shape under an ordinary name."""
    def shaped(method: str) -> _Graph:
        g = _Graph()
        base = g.add_class("Base", file="app/base.py", methods=[method])
        child = g.add_class("Child", file="app/child.py", bases=["Base"], methods=[method],
                            inherits=[base])
        g.call("tests.t.test_child", "tests/t.py", f"{child}.{method}", "app/child.py")
        g.call("app.make.make", "app/make.py", f"{base}.{method}", "app/base.py")
        return g

    assert "## Callers through `Base.run`" in _ask(
        monkeypatch, shaped("run"), "callers", "Child.run")["result"]
    graph = shaped("__init__")
    env = _ask(monkeypatch, graph, "callers", "Child.__init__")
    assert "## Callers through" not in env["result"]
    assert not any("DEFINES_METHOD" in q for q in graph.queries)


def test_a_bare_name_that_matches_several_symbols_is_not_looked_up_through_bases(monkeypatch):
    """Two classes define `build_result`, so the answer is already `target-ambiguous` and is narrowed
    by asking again. The lookup is per symbol: CONTROL, the narrowed ask, which has it."""
    graph, _, _ = _protocol_graph()
    other = graph.add_class("Other", file="app/other.py", methods=["build_result"])
    graph.call("tests.o.test", "tests/o.py", f"{other}.build_result", "app/other.py")

    env = _ask(monkeypatch, graph, "callers", "build_result")
    assert "target-ambiguous" in _gap_kinds(env), env["gaps"]
    assert "## Callers through" not in env["result"]
    assert not any("DEFINES_METHOD" in q for q in graph.queries)

    narrowed = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")
    assert "## Callers through" in narrowed["result"]


def test_bases_that_have_no_callers_are_named_in_one_line_and_raise_no_gap(monkeypatch):
    """The method is declared on a Protocol it satisfies and nothing calls that declaration either.
    One line says so; nothing is wrong with the answer, so nothing is a gap."""
    graph, proto, _ = _protocol_graph()
    graph.edges = [e for e in graph.edges if e["b.qualified_name"] != f"{proto}.build_result"]
    env = _ask(monkeypatch, graph, "callers", "GraphProvider.build_result")

    assert ("_`GraphProvider.build_result` is also declared on `CodeProvider.build_result` (a "
            "Protocol it satisfies by method names); the graph records no caller of it either._"
            ) in env["result"], env["result"]
    assert "## Callers through" not in env["result"]
    assert env["confidence"] == "complete" and not env.get("gaps"), env
    assert env["evidence"]["safe_for_destructive"] is True
    assert not [ln for ln in env["result"].splitlines() if ln.startswith("- ") and "via" in ln]


# ================================================================================= small readers

def test_base_classes_are_read_in_every_shape_the_backend_writes_them_and_never_as_none_when_unreadable():
    assert _base_names('["Protocol"]') == ("Protocol",)
    assert _base_names(["A", "pkg.B[T]"]) == ("A", "pkg.B[T]")
    assert _base_names(None) == () and _base_names("-") == () and _base_names("") == ()
    assert _base_names("[]") == ()
    # A value this release cannot read is NOT an empty hierarchy: it surfaces as an unresolved base.
    assert _base_names("not json") != () and _base_names("{}") != ()
    assert _leaf("typing.Protocol[T]") == "Protocol" and _leaf("asyncssh.SSHServer") == "SSHServer"


# =============================================================== the `self.m()` rule across classes

def _self_graph(*, mid_defines: bool = False, ext: bool = False, side: bool = False,
                texts: tuple[tuple[str, str], ...] = (("op", "self.render"),),
                strategy: str = "unique_name", conf: str = "0.75") -> tuple[_Graph, str, str]:
    """`Ops` inherits `Mixin` through `Mid`, and its methods call `self.render`, which `Mixin`
    defines — `GraphOps._op_callers` calling `self._render_edge_answer`, the case that motivated it."""
    g = _Graph()
    mixin = g.add_class("Mixin", file="app/mixin.py", methods=["render"])
    mid = g.add_class("Mid", file="app/mid.py", bases=["Mixin"],
                      methods=["render"] if mid_defines else ["other"], inherits=[mixin])
    bases, inherits = ["Mid"], [mid]
    if side:
        s = g.add_class("Side", file="app/side.py", methods=["render"])
        bases, inherits = ["Side", "Mid"], [s, mid]
    if ext:
        bases.append("ExternalBase")
    ops = g.add_class("Ops", file="app/ops.py", bases=bases, inherits=inherits,
                      methods=[m for m, _ in texts])
    for method, text in texts:
        g.call(f"app.ops.Ops.{method}", "app/ops.py", f"{mixin}.render", "app/mixin.py",
               caller_label="Method", strategy=strategy, conf=conf, text=text)
    return g, mixin, ops


def test_a_self_call_to_a_method_on_a_base_is_bound_by_the_class_hierarchy(monkeypatch):
    graph, _, _ = _self_graph()
    env = _ask(monkeypatch, graph, "callers", "Mixin.render")
    (row,) = env["rows"]

    assert row["verified"] is True and row["evidence"] == "resolved", row
    assert row["strategy"] == "self_mro" and row["confidence"] is None, row
    assert "`self.render` inside a method of `Ops`" in row["why"], row
    assert "`Ops` → `Mid` → `Mixin`" in row["why"] and "from INHERITS edges" in row["why"], row
    assert "backend scored this edge by name (unique_name)" in row["why"], row
    assert "does not claim" not in row["why"]
    # What the index cannot see is a limit of the rule, and is on the row it relabelled.
    assert "nor is a class that assigns `render`" in row["why"], row
    assert "which the index does not record" in row["why"], row
    assert "[?" not in env["result"], env["result"]
    assert env["confidence"] == "complete" and env["evidence"]["safe_for_destructive"] is True, env
    assert env["evidence"]["verified"] == 1 and env["evidence"]["possible"] == 0


def test_a_cls_receiver_is_the_enclosing_class_too_and_nothing_else_is(monkeypatch):
    """Only `self.m` and `cls.m`, spelled exactly, say the receiver is the enclosing object. A
    receiver that is some other value, an attribute of it, or `super()` is left as the backend labelled
    it — a rule that bound `ops.render` would be binding whatever `ops` is."""
    texts = (("a", "self.render"), ("b", "cls.render"), ("c", "ops.render"),
             ("d", "self.helper.render"), ("e", "super().render"))
    graph, _, _ = _self_graph(texts=texts)
    env = _ask(monkeypatch, graph, "callers", "Mixin.render")
    by_name = {r["name"]: r for r in env["rows"]}

    assert {n for n, r in by_name.items() if r["verified"]} == {"a", "b"}, by_name
    assert {n for n, r in by_name.items() if r["strategy"] == "self_mro"} == {"a", "b"}
    assert all(not by_name[n]["verified"] and by_name[n]["strategy"] == "unique_name"
               for n in "cde"), by_name


def test_an_intermediate_class_that_defines_the_method_blocks_the_upgrade(monkeypatch):
    """`Mid` overrides `render`, so `self.render` inside `Ops` runs `Mid.render` and the backend's
    edge to `Mixin.render` is a guess after all. CONTROL: the same hierarchy with `Mid` not defining
    it, which binds."""
    graph, _, _ = _self_graph(mid_defines=False)
    assert _ask(monkeypatch, graph, "callers", "Mixin.render")["rows"][0]["verified"] is True

    blocked, _, _ = _self_graph(mid_defines=True)
    env = _ask(monkeypatch, blocked, "callers", "Mixin.render")
    (row,) = env["rows"]
    assert row["verified"] is False and row["strategy"] == "unique_name", row
    assert "[?0.75]" in env["result"], env["result"]


def test_a_side_branch_of_a_multiple_inheritance_that_defines_the_method_blocks_it(monkeypatch):
    """`Ops(Side, Mid)` where `Side` defines `render` and does not lead to `Mixin`: the lookup finds
    `Side.render` before it finds `Mixin.render`, so only the classes that SHADOW the target's own
    ancestors may be ignored — not every class that is not on the way."""
    graph, _, _ = _self_graph(side=True)
    env = _ask(monkeypatch, graph, "callers", "Mixin.render")

    assert env["rows"][0]["verified"] is False, env["rows"]
    # CONTROL: the same side branch NOT defining the method leaves the binding intact.
    clean, _, _ = _self_graph(side=True)
    side = next(q for q, c in clean.classes.items() if c["name"] == "Side")
    clean.classes[side]["methods"] = ["unrelated"]
    assert _ask(monkeypatch, clean, "callers", "Mixin.render")["rows"][0]["verified"] is True


def test_a_base_the_index_cannot_resolve_blocks_the_upgrade(monkeypatch):
    """`Ops` also inherits a class this index does not hold, and that class could define `render`
    before `Mixin` is reached. The rule refuses rather than assume it does not."""
    graph, _, _ = _self_graph(ext=True)
    env = _ask(monkeypatch, graph, "callers", "Mixin.render")
    assert env["rows"][0]["verified"] is False, env["rows"]

    inert, _, ops = _self_graph(ext=False)
    inert.classes[ops]["bases"].append("Generic[T]")      # `Generic` defines nothing a call waits on
    assert _ask(monkeypatch, inert, "callers", "Mixin.render")["rows"][0]["verified"] is True


def test_a_self_call_to_a_method_a_subclass_defines_is_not_upgraded(monkeypatch):
    """`Mixin.helper` calls `self.render`, and `render` here is `Ops.render` — defined on a SUBCLASS,
    reached by the mixin contract and not by the hierarchy. The target's class is not in the caller's
    ancestry, so nothing binds it. CONTROL: the same call when `render` is `Mixin`'s own."""
    own = _Graph()
    mixin = own.add_class("Mixin", file="app/mixin.py", methods=["helper", "render"])
    own.call("app.mixin.Mixin.helper", "app/mixin.py", f"{mixin}.render", "app/mixin.py",
             caller_label="Method", strategy="unique_name", conf="0.75", text="self.render")
    assert _ask(monkeypatch, own, "callers", "Mixin.render")["rows"][0]["verified"] is True

    g = _Graph()
    mixin = g.add_class("Mixin", file="app/mixin.py", methods=["helper"])
    ops = g.add_class("Ops", file="app/ops.py", bases=["Mixin"], inherits=[mixin], methods=["render"])
    g.call("app.mixin.Mixin.helper", "app/mixin.py", f"{ops}.render", "app/ops.py",
           caller_label="Method", strategy="unique_name", conf="0.75", text="self.render")
    env = _ask(monkeypatch, g, "callers", "Ops.render")

    assert env["rows"][0]["verified"] is False and env["rows"][0]["strategy"] == "unique_name"


def test_a_self_call_to_a_method_the_class_defines_itself_is_resolved(monkeypatch):
    g = _Graph()
    ops = g.add_class("Ops", file="app/ops.py", methods=["render", "op"])
    g.call("app.ops.Ops.op", "app/ops.py", f"{ops}.render", "app/ops.py",
           caller_label="Method", strategy="unique_name", conf="0.75", text="self.render")
    env = _ask(monkeypatch, g, "callers", "Ops.render")

    row = env["rows"][0]
    assert row["verified"] is True and row["strategy"] == "self_mro", row
    assert "`Ops` defines `render` itself" in row["why"], row


def test_the_library_function_sentence_is_not_printed_when_every_row_is_an_in_hierarchy_self_call(
        monkeypatch):
    """"Which is also why any unresolved call to a `render` the index does not contain (a library
    function, a framework global, a builtin method) binds here" is a true warning about a name
    guess, and a false one about a `self.render()` call the hierarchy binds. CONTROL: add one call
    that really is a guess and the sentence is back."""
    sentence = "a library function, a framework global, a builtin method"
    graph, _, _ = _self_graph()
    env = _ask(monkeypatch, graph, "callers", "render")
    assert sentence not in env["result"], env["result"]
    assert "Rows badged" not in env["result"] and "[?" not in env["result"]

    guessy, mixin, _ = _self_graph()
    guessy.call("app.elsewhere.use", "app/elsewhere.py", f"{mixin}.render", "app/mixin.py",
                strategy="unique_name", conf="0.75", text="widget.render")
    both = _ask(monkeypatch, guessy, "callers", "render")
    assert sentence in both["result"], both["result"]
    assert [r["verified"] for r in both["rows"]] == [True, False], both["rows"]


def test_a_failed_lookup_leaves_a_self_call_name_matched_and_raises_no_gap_of_its_own(monkeypatch):
    """The rule is an improvement, not a claim: when its lookup fails the row stays exactly as the
    backend labelled it, which is already disclosed, and the failure is not reported as one of the
    answer's. CONTROL: the same graph with the lookup answering."""
    graph, _, _ = _self_graph()
    assert _ask(monkeypatch, graph, "callers", "Mixin.render")["rows"][0]["verified"] is True

    failing, _, _ = _self_graph()
    failing.fail_when = lambda q: "m.qualified_name IN" in q
    env = _ask(monkeypatch, failing, "callers", "Mixin.render")

    assert env["rows"][0]["verified"] is False and env["rows"][0]["strategy"] == "unique_name"
    assert _gap_kinds(env) == ["low-confidence-edges"], env["gaps"]
    assert not any(g["section"] == "backend" for g in env["gaps"]), env["gaps"]


def test_callees_relabels_the_same_edge_the_same_way_callers_does(monkeypatch):
    """One edge, two ops. `callers Mixin.render` calls it a resolution; `callees Ops.op` must not
    call it a name guess — an answer that disagreed with itself across ops would be the drift this
    project keeps guarding against."""
    graph, _, _ = _self_graph()
    callers = _ask(monkeypatch, graph, "callers", "Mixin.render")
    callees = _ask(monkeypatch, graph, "callees", "Ops.op")

    assert callers["rows"][0]["strategy"] == callees["rows"][0]["strategy"] == "self_mro"
    assert callees["rows"][0]["verified"] is True and callees["rows"][0]["relation"] == "callee"
    assert "[?" not in callees["result"], callees["result"]


def test_a_non_method_caller_is_left_alone(monkeypatch):
    """A function that happens to be passed an argument called `self` is not a method of anything.
    The row says the caller is a function, so the rule has no class to start from. CONTROL: the same
    edge from a METHOD, which binds."""
    graph, _, _ = _self_graph()
    assert _ask(monkeypatch, graph, "callers", "Mixin.render")["rows"][0]["verified"] is True

    graph.edges[0]["labels(a)"] = json.dumps(["Function"])
    env = _ask(monkeypatch, graph, "callers", "Mixin.render")

    assert env["rows"][0]["verified"] is False and env["rows"][0]["strategy"] == "unique_name"


# =================================================================== `changed <ref>` and the rows

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], capture_output=True,
                   text=True, check=True, env={**os.environ, **_GIT_ENV})


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def branch_repo(tmp_path) -> Path:
    """`main` has `Svc.run(self, x)` overriding `Base.run` and a driver; `feature` gives `run` a
    second parameter and rewrites the driver."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/core.py",
           "class Base:\n    def run(self, x):\n        return x\n\n\n"
           "class Svc(Base):\n    def run(self, x):\n        return x\n")
    _write(repo, "pkg/driver.py", "def drive():\n    return 1\n\n\ndef drive_direct():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "pkg/core.py",
           "class Base:\n    def run(self, x):\n        return x\n\n\n"
           "class Svc(Base):\n    def run(self, x, y=1):\n        return x\n")
    _write(repo, "pkg/driver.py", "def drive():\n    return 2\n\n\ndef drive_direct():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature")
    return repo


def _changed_graph() -> _Graph:
    g = _Graph()
    base = g.add_class("Base", file="pkg/core.py", methods=["run"])
    svc = g.add_class("Svc", file="pkg/core.py", bases=["Base"], methods=["run"], inherits=[base])
    g.call("pkg.legacy.old", "pkg/legacy.py", f"{svc}.run", "pkg/core.py")
    g.call("pkg.driver.drive_direct", "pkg/driver.py", f"{svc}.run", "pkg/core.py")
    g.call("pkg.driver.drive", "pkg/driver.py", f"{base}.run", "pkg/core.py",
           caller_label="Function", strategy="field_type_hint", conf="0.85", text="runner.run")
    g.call("pkg.other.other", "pkg/other.py", f"{base}.run", "pkg/core.py",
           strategy="field_type_hint", conf="0.85", text="runner.run")
    return g


def test_changed_lists_the_callers_through_a_base_apart_from_the_untouched_and_also_changed_ones(
        monkeypatch, tmp_path, branch_repo):
    """`changed main` asks `callers` about `Svc.run`, which this branch re-signed. A caller of the
    BASE is not sorted by whether the diff touched it: `drive` was rewritten, and that says nothing
    about whether its author considered `Svc.run`, so it is not "also changed — probably updated
    together". It gets a list of its own, named for the base, and the direct callers keep the split
    — each list with its own count."""
    cache = tmp_path / "graph-cache"
    cache.mkdir()
    db = cache / f"{PROJECT}.db"
    db.write_bytes(b"")
    os.utime(db, (time.time() + 3600, time.time() + 3600))
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    graph = _changed_graph()
    p = _provider(monkeypatch, graph, root=str(branch_repo))
    env = p.build_result("changed", "main", [], 30000, str(branch_repo))
    body = env["result"]

    assert env["result"] is not None, env
    group = body[body.index("#### `Svc.run`"):]
    group = group[: group.index("\n###")]
    assert "Callers this diff did NOT touch — these may break (1):" in group, group
    assert "Callers also changed in this diff — probably updated together (1):" in group, group
    assert "Callers through `Base.run` — they call the base class, not this symbol" in group, group
    untouched, rest = group.split("Callers also changed")
    also, through = rest.split("Callers through")
    assert "- pkg.legacy.old [CALLS] (pkg/legacy.py)" in untouched, untouched
    assert "?via" not in untouched, "a caller of the base is not an untouched caller of this symbol"
    assert "- pkg.driver.drive_direct [CALLS] (pkg/driver.py)" in also, also
    assert "drive [" not in also and "?via" not in also, "a rewritten caller of the BASE is not 'also changed'"
    assert "(2):**" in through and "pkg.driver.drive [CALLS] [?via base 0.85]" in through, through
    assert "- pkg.other.other [CALLS] [?via base 0.85] (pkg/other.py)" in through, through
    assert "[advisory]" in group.splitlines()[0]

    rows = {r["name"]: r for r in env["rows"]}
    assert rows["old"]["verified"] is True and "via" not in rows["old"], rows["old"]
    assert rows["drive_direct"]["caller_status"] == "also-changed", rows["drive_direct"]
    for name in ("other", "drive"):
        assert rows[name]["via"] == "pkg.core.Base.run" and rows[name]["via_kind"] == "base", rows[name]
        assert rows[name]["verified"] is False and rows[name]["caller_status"] == "through-base", rows[name]
    gap = _gap(env, "callers-via-base")
    assert "`Svc.run`" in gap["detail"] and "`Base.run` (base, 2)" in gap["detail"], gap
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False
    assert env["evidence"]["returned"] == len(env["rows"]) == len(_row_lines(body)) == 4


# ============================================ a name-resolved hierarchy link is not an edge

def _serializer_graph(*, inherits: bool) -> _Graph:
    """One project `Serializer`, and a class whose statement writes `Serializer` as a base. With no
    INHERITS edge the base may be the library's `Serializer` that the child imports instead — the one
    project class that carries the name is not evidence that it is the one the child inherits."""
    g = _Graph()
    ser = g.add_class("Serializer", file="app/serializer.py", methods=["validate"])
    g.add_class("MySer", file="app/my.py", bases=["Serializer"], methods=["save"],
                inherits=[ser] if inherits else [])
    g.call("app.my.MySer.save", "app/my.py", f"{ser}.validate", "app/serializer.py",
           caller_label="Method", strategy="unique_name", conf="0.75", text="self.validate")
    return g


def test_a_self_call_through_a_base_found_only_by_its_name_is_not_verified(monkeypatch):
    """`self.validate()` in `MySer.save` carries a `unique_name` edge to the project's `Serializer`.
    Relabelling it `self_mro` — verified, no gap, `safe_for_destructive: true` — would be the
    library-collision failure promoted to a verdict: the link `MySer` → `Serializer` came from a
    NAME. CONTROL: the same graph with the INHERITS edge, which does bind."""
    env = _ask(monkeypatch, _serializer_graph(inherits=False), "callers", "Serializer.validate")
    (row,) = env["rows"]

    assert row["verified"] is False and row["strategy"] == "unique_name", row
    assert "the class hierarchy would bind it (`MySer` → `Serializer`)" in row["why"], row
    assert "found by resolving the base-class NAME its statement wrote, not from an INHERITS edge" in row["why"]
    assert env["evidence"]["safe_for_destructive"] is False and env["confidence"] == "partial", env
    assert "low-confidence-edges" in _gap_kinds(env), env["gaps"]

    wired = _ask(monkeypatch, _serializer_graph(inherits=True), "callers", "Serializer.validate")
    assert wired["rows"][0]["verified"] is True and wired["rows"][0]["strategy"] == "self_mro"
    assert wired["confidence"] == "complete" and wired["evidence"]["safe_for_destructive"] is True


def test_a_dotted_base_that_matches_no_indexed_class_is_unresolved_whatever_shares_its_last_name(
        monkeypatch):
    """`class K(torch.nn.Module)` names a class this index does not hold. A project `Module` is not
    it, and falling back to "the one class with that name" handed K a hierarchy it never had — in the
    callers through a base and in the `self.m()` rule alike. CONTROL: the bare spelling resolves."""
    def shaped(written: str) -> _Graph:
        g = _Graph()
        base = g.add_class("Base", file="app/base.py", methods=["run"])
        g.add_class("Child", file="app/child.py", bases=[written], methods=["run"])
        g.call("tests.t.test_child", "tests/t.py", "tmp-proj.app.child.Child.run", "app/child.py")
        g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
               strategy="field_type_hint", conf="0.85")
        return g

    dotted = _ask(monkeypatch, shaped("torch.nn.Base"), "callers", "Child.run")
    assert "## Callers through" not in dotted["result"], dotted["result"]
    assert "## Callers through `Base.run`" in _ask(
        monkeypatch, shaped("Base"), "callers", "Child.run")["result"]

    g = _Graph()
    module = g.add_class("Module", file="app/module.py", methods=["forward"])
    g.add_class("K", file="app/k.py", bases=["torch.nn.Module"], methods=["go"])
    g.call("app.k.K.go", "app/k.py", f"{module}.forward", "app/module.py", caller_label="Method",
           strategy="unique_name", conf="0.75", text="self.forward")
    row = _ask(monkeypatch, g, "callers", "Module.forward")["rows"][0]
    assert row["verified"] is False and row["strategy"] == "unique_name", row


def test_a_self_call_inside_a_metaclass_is_not_upgraded(monkeypatch):
    """In a metaclass method `self` is a CLASS, whose attribute lookup is not the instance MRO the
    rule reasons about. `ABCMeta` is among the bases the walk ignores, so nothing else would stop it.
    CONTROL: the same hierarchy without the metaclass base binds."""
    clean, _, _ = _self_graph()
    assert _ask(monkeypatch, clean, "callers", "Mixin.render")["rows"][0]["verified"] is True

    meta, _, ops = _self_graph()
    meta.classes[ops]["bases"].append("ABCMeta")
    row = _ask(monkeypatch, meta, "callers", "Mixin.render")["rows"][0]
    assert row["verified"] is False and row["strategy"] == "unique_name", row


# ============================================== the via section lists only what can reach the target

def _super_graph(*, with_driver: bool = True) -> tuple[_Graph, str, str]:
    """`Child.run` overrides `Base.run` and calls `super().run()`, as every override does. The real
    backend records that call as an edge `Child.run -> Base.run` (strategy `lsp_super`, call text
    `super().run`), and so does it for a sibling override and for another method of the class."""
    g = _Graph()
    base = g.add_class("Base", file="app/base.py", methods=["run"])
    child = g.add_class("Child", file="app/child.py", bases=["Base"], inherits=[base],
                        methods=["run", "helper"])
    g.add_class("Other", file="app/other.py", bases=["Base"], inherits=[base], methods=["run"])
    g.call("tests.test_child.test_run", "tests/test_child.py", f"{child}.run", "app/child.py")
    g.call("app.child.Child.run", "app/child.py", f"{base}.run", "app/base.py", caller_label="Method",
           strategy="lsp_super", conf="0.88", text="super().run")
    g.call("app.other.Other.run", "app/other.py", f"{base}.run", "app/base.py", caller_label="Method",
           strategy="lsp_super", conf="0.88", text="super(Other, self).run")
    g.call("app.child.Child.helper", "app/child.py", f"{base}.run", "app/base.py", caller_label="Method",
           strategy="lsp_super", conf="0.88", text="super().run")
    if with_driver:
        g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
               strategy="field_type_hint", conf="0.85", text="runner.run")
    return g, base, child


def test_the_targets_own_super_call_and_a_siblings_are_not_callers_of_it_and_are_counted(monkeypatch):
    """`Child.run` calling `super().run()` is not `Child.run` being called, and `Other.run` calling
    `super(Other, self).run()` runs `Base.run` — it can never reach a sibling's override. Neither is
    listed; both are COUNTED in one line, so a row that is not there has not vanished unexplained.
    A `super()` call from another method of the target's own class skips the class too."""
    graph, _, _ = _super_graph()
    env = _ask(monkeypatch, graph, "callers", "Child.run")
    body = env["result"]

    assert [r["name"] for r in env["rows"] if r.get("via")] == ["drive"], env["rows"]
    assert "Child.run [" not in _via_section(body), "the method is listed as its own caller"
    assert ("_Not listed, because a call to `Base.run` from them cannot reach `Child.run`: "
            "`Child.run` itself, which calls the method it overrides and 2 `super()` call(s) from "
            "classes that do not descend from `Child` (other overrides of `Base.run`)._") in body, body
    assert "## Callers through `Base.run` — they call the base class; at run time a call reaches this " \
           "override only when the object is a `Child` (1)" in body, body
    # What is left is what the answer still stands behind: one caller of the base.
    assert "`Base.run` (base, 1)" in _gap(env, "callers-via-base")["detail"]


def test_the_targets_call_to_the_base_through_a_delegate_is_kept_because_it_can_reach_it(
        monkeypatch):
    """`Child.run` calling `self.inner.run()` on a `Base`-typed delegate reaches `Child.run` again
    whenever the delegate is itself a `Child` — a doubt, not a proof, so it is listed, not dropped.
    Only the target's call to the method it OVERRIDES (`super().run()`, `Base.run(self)`) cannot."""
    g = _Graph()
    base = g.add_class("Base", file="app/base.py", methods=["run"])
    g.add_class("Child", file="app/child.py", bases=["Base"], inherits=[base], methods=["run"])
    g.call("app.child.Child.run", "app/child.py", f"{base}.run", "app/base.py", caller_label="Method",
           strategy="field_type_hint", conf="0.85", text="self.inner.run")
    env = _ask(monkeypatch, g, "callers", "Child.run")

    assert [r["qualified_name"] for r in env["rows"] if r.get("via")] == ["app.child.Child.run"], \
        env["rows"]
    assert "cannot reach" not in env["result"], "a call that can reach the target was counted as one that cannot"


def test_the_targets_unbound_call_to_the_method_it_overrides_is_not_a_caller_of_it(monkeypatch):
    """`Base.run(self)` inside `Child.run` is the pre-`super()` spelling of the same thing: it runs the
    base and cannot come back to the override. Like `super().run()`, it is left out and counted."""
    g = _Graph()
    base = g.add_class("Base", file="app/base.py", methods=["run"])
    g.add_class("Child", file="app/child.py", bases=["Base"], inherits=[base], methods=["run"])
    g.call("app.child.Child.run", "app/child.py", f"{base}.run", "app/base.py", caller_label="Method",
           strategy="import_map", conf="0.95", text="Base.run")
    g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
           strategy="field_type_hint", conf="0.85", text="runner.run")
    env = _ask(monkeypatch, g, "callers", "Child.run")

    assert [r["name"] for r in env["rows"] if r.get("via")] == ["drive"], env["rows"]
    assert "`Child.run` itself, which calls the method it overrides" in env["result"], env["result"]


def test_an_override_whose_only_base_callers_are_super_calls_that_cannot_reach_it_is_not_partial(
        monkeypatch):
    """Every override that calls `super()` used to become permanently partial, unsafe and advisory —
    cry-wolf on the commonest pattern in object-oriented code. Nothing here can reach `Child.run`
    through `Base.run`, so the answer is whole, and says what it left out."""
    graph, _, _ = _super_graph(with_driver=False)
    env = _ask(monkeypatch, graph, "callers", "Child.run")
    body = env["result"]

    assert "## Callers through" not in body, body
    assert _gap_kinds(env) == [], env["gaps"]
    assert env["confidence"] == "complete" and env["evidence"]["safe_for_destructive"] is True, env
    assert "cannot reach `Child.run`" in body and "the graph records no caller of" not in body, body
    assert env["evidence"]["returned"] == len(env["rows"]) == 1 and env["rows"][0]["name"] == "test_run"


def test_a_super_call_from_a_subclass_of_the_target_can_reach_it_and_is_kept_and_labelled(monkeypatch):
    """`GrandChild(Child).run` calling `super().run()` runs whatever comes next in its MRO — which can
    be `Child.run`. That one stays, with the reason on the row and on the section. CONTROL: the same
    shape for a class that is NOT a subclass is dropped, in the same answer."""
    graph, _, child = _super_graph()
    graph.add_class("GrandChild", file="app/grand.py", bases=["Child"], inherits=[child], methods=["run"])
    graph.call("app.grand.GrandChild.run", "app/grand.py", "tmp-proj.app.base.Base.run", "app/base.py",
               caller_label="Method", strategy="lsp_super", conf="0.88", text="super().run")
    env = _ask(monkeypatch, graph, "callers", "Child.run")
    via = {r["name"]: r for r in env["rows"] if r.get("via")}

    assert sorted(via) == ["drive", "run"], via
    run = via["run"]
    assert run["qualified_name"] == "app.grand.GrandChild.run" and run["verified"] is False, run
    assert "a `super()` call inside `GrandChild`, a class that descends from this symbol's class" in run["why"]
    assert "1 of them are `super()` calls from a class that descends from `Child`" in env["result"]
    assert "2 `super()` call(s) from classes that do not descend from `Child`" in env["result"]


def test_a_super_call_whose_class_cannot_be_placed_is_labelled_not_dropped(monkeypatch):
    """The lookup of which class a `super()` caller belongs to fails. That is a doubt and not a
    proof, so the row is kept — and says its class was not placed. The failure belongs to an
    improvement, not to the answer, so it raises no gap of its own."""
    graph, _, _ = _super_graph(with_driver=False)
    graph.fail_when = lambda q: "m.qualified_name IN" in q
    env = _ask(monkeypatch, graph, "callers", "Child.run")

    via = {r["qualified_name"]: r for r in env["rows"] if r.get("via")}
    assert sorted(via) == ["app.child.Child.helper", "app.other.Other.run"], via
    assert all("could not place in this symbol's hierarchy" in r["why"] for r in via.values()), via
    assert not any(g["section"] == "backend" for g in env["gaps"]), env["gaps"]
    assert "cannot reach `Child.run`: `Child.run` itself" in env["result"]


# ====================================== a Protocol member that could be an attribute is undecided

def _reader_graph(*, property_member: bool = True, extra: tuple[str, ...] = ()) -> tuple[_Graph, str, str]:
    """`Reader(Protocol)` declares `name` and `read`; `FileReader` defines `read` and sets
    `name = "f"` in its class body — which the graph records NOWHERE (no node, no edge), so as far as
    the index can tell it defines `read` and nothing else."""
    g = _Graph()
    reader = g.add_class("Reader", file="app/reader.py", bases=["Protocol"],
                         methods=["name", "read", *extra],
                         decorators={"name": ["@property"]} if property_member else {})
    impl = g.add_class("FileReader", file="app/file_reader.py", methods=["read"])
    g.call("tests.t.test_read", "tests/t.py", f"{impl}.read", "app/file_reader.py")
    g.call("app.use.use", "app/use.py", f"{reader}.read", "app/reader.py", caller_label="Method",
           strategy="field_type_hint", conf="0.85", text="reader.read")
    return g, reader, impl


def test_a_protocol_property_the_class_may_satisfy_by_an_attribute_is_undecided_and_listed(monkeypatch):
    """The class has `name = "f"`; the Protocol declares `@property name`. "Does not conform" was a
    claim the index cannot make — and the answer said `complete`, `safe_for_destructive: true` while
    the production caller sat on `Reader.read`. Now: the caller is listed as undecided, and a gap
    names the Protocol and the member."""
    graph, _, _ = _reader_graph()
    env = _ask(monkeypatch, graph, "callers", "FileReader.read")

    via = [r for r in env["rows"] if r.get("via")]
    assert [r["name"] for r in via] == ["use"] and via[0]["via_kind"] == "protocol-undecided", via
    assert via[0]["verified"] is False and "undecided" in via[0]["why"], via
    gap = _gap(env, "dispatch-bases-incomplete")
    assert "`Reader` declares `read`" in gap["detail"], gap
    assert "an attribute or property could satisfy `name`" in gap["detail"], gap
    assert "missing: name" in gap["detail"] and "undecided" in gap["detail"], gap
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False
    assert "[?via protocol-undecided 0.85]" in env["result"], env["result"]
    assert "UNDECIDED" in _via_section(env["result"])


def test_a_class_that_provably_lacks_a_protocol_method_contributes_nothing_as_before(monkeypatch):
    """CONTROL for the answer above: the same graph with `name` declared as a plain method. The class
    has no method of that name and there is no reason an attribute would stand in for one — it does
    not conform, and the answer is the one it always was."""
    graph, _, _ = _reader_graph(property_member=False)
    env = _ask(monkeypatch, graph, "callers", "FileReader.read")

    assert "## Callers through" not in env["result"], env["result"]
    assert env["confidence"] == "complete" and not env.get("gaps"), env
    assert env["evidence"]["safe_for_destructive"] is True


def test_the_members_a_protocol_inherits_from_another_protocol_are_required_too(monkeypatch):
    """`Reader(Base1, Protocol)` is satisfied by what it declares AND by what `Base1` declares. A class
    that defines `read` but not `Base1.extra` does not satisfy it — judged on `read` alone it would."""
    def shaped(impl_methods: list[str], decorators: dict[str, list[str]] | None = None) -> _Graph:
        g = _Graph()
        base1 = g.add_class("Base1", file="app/base1.py", bases=["Protocol"], methods=["extra"],
                            decorators=decorators)
        reader = g.add_class("Reader", file="app/reader.py", bases=["Base1", "Protocol"],
                             inherits=[base1], methods=["read"])
        impl = g.add_class("FileReader", file="app/file_reader.py", methods=impl_methods)
        g.call("tests.t.test_read", "tests/t.py", f"{impl}.read", "app/file_reader.py")
        g.call("app.use.use", "app/use.py", f"{reader}.read", "app/reader.py", caller_label="Method",
               strategy="field_type_hint", conf="0.85", text="reader.read")
        return g

    missing = _ask(monkeypatch, shaped(["read"]), "callers", "FileReader.read")
    assert "## Callers through" not in missing["result"], missing["result"]
    assert missing["confidence"] == "complete", missing.get("gaps")

    whole = _ask(monkeypatch, shaped(["read", "extra"]), "callers", "FileReader.read")
    assert "## Callers through `Reader.read`" in whole["result"], whole["result"]
    assert {r["via_kind"] for r in whole["rows"] if r.get("via")} == {"protocol"}

    undecided = _ask(monkeypatch, shaped(["read"], {"extra": ["@property"]}), "callers",
                     "FileReader.read")
    assert {r["via_kind"] for r in undecided["rows"] if r.get("via")} == {"protocol-undecided"}
    assert "missing: extra" in _gap(undecided, "dispatch-bases-incomplete")["detail"]


# ===================================================== the extra lookups are bounded

def test_the_first_failed_base_fetch_ends_the_loop_and_names_what_was_not_asked(monkeypatch):
    """A backend that did not answer once is not asked again for each base that is left — a wedged one
    used to cost a timeout per base. The bases that were not looked up are named."""
    g = _Graph()
    own = g.add_class("Impl", file="app/impl.py", methods=["m"])
    for i in range(4):
        proto = g.add_class(f"P{i}", file=f"app/p{i}.py", bases=["Protocol"], methods=["m"])
        g.call(f"app.use{i}.use", f"app/use{i}.py", f"{proto}.m", f"app/p{i}.py",
               strategy="field_type_hint", conf="0.85")
    g.call("tests.t.test_m", "tests/t.py", f"{own}.m", "app/impl.py")
    edge_queries = {"n": 0}

    def fail_every_base(cypher: str) -> bool:
        if "-[c:" in cypher:
            edge_queries["n"] += 1
            return edge_queries["n"] >= 2
        return False

    g.fail_when = fail_every_base
    env = _ask(monkeypatch, g, "callers", "Impl.m")

    assert edge_queries["n"] == 2, f"{edge_queries['n']} edge queries: the loop went on after a failure"
    detail = _gap(env, "dispatch-bases-incomplete")["detail"]
    assert "the callers of `P0.m` could not be fetched" in detail, detail
    assert "the callers of `P1.m`, `P2.m`, `P3.m` were not looked up" in detail, detail
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False


def test_all_the_extra_lookups_of_one_request_share_one_allowance_of_time(monkeypatch):
    """A backend that answers, slowly, is not asked indefinitely either: the lookups beyond the direct
    query share an allowance derived from the request's budget, each call is capped to what is left,
    and what the allowance did not reach is named. The clock is driven by the stub, one lookup
    taking most of the per-call budget."""
    g = _Graph()
    own = g.add_class("Impl", file="app/impl.py", methods=["m"])
    for i in range(_MAX_BASES):
        proto = g.add_class(f"P{i:02d}", file=f"app/p{i:02d}.py", bases=["Protocol"], methods=["m"])
        g.call(f"app.use{i}.use", f"app/use{i}.py", f"{proto}.m", f"app/p{i:02d}.py",
               strategy="field_type_hint", conf="0.85")
    g.call("tests.t.test_m", "tests/t.py", f"{own}.m", "app/impl.py")
    now = {"t": 1000.0}
    monkeypatch.setattr("codeintel.graph_dispatch._clock", lambda: now["t"], raising=False)
    g.on_query = lambda cypher, timeout_ms: now.__setitem__("t", now["t"] + 25.0)   # of a 30 s budget
    env = _ask(monkeypatch, g, "callers", "Impl.m")

    edge_queries = [q for q in g.queries if "-[c:" in q]
    assert len(edge_queries) < 1 + _MAX_BASES, f"{len(edge_queries)} edge queries: no allowance applied"
    assert max(g.timeouts) <= 30000 and min(g.timeouts[-3:]) < 30000, g.timeouts
    detail = _gap(env, "dispatch-bases-incomplete")["detail"]
    assert "the time allowed for these lookups" in detail and "ran out" in detail, detail
    assert env["confidence"] == "partial" and env["evidence"]["total"] is None, env["evidence"]


@pytest.mark.parametrize("op", ["callers", "impact"])
def test_the_class_hierarchy_is_read_once_for_the_dispatch_lookup_and_the_self_call_rule(
        monkeypatch, op):
    """`callers` runs the lookup of the target's bases and the `self.m()` rule, and `impact` runs them
    for both of its halves. All of them want the ancestry of `Child`; it is read once."""
    g = _Graph()
    base = g.add_class("Base", file="app/base.py", methods=["run", "helper"])
    child = g.add_class("Child", file="app/child.py", bases=["Base"], inherits=[base],
                        methods=["run", "other"])
    g.call("tests.t.test_child", "tests/t.py", f"{child}.run", "app/child.py")
    g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
           strategy="field_type_hint", conf="0.85", text="runner.run")
    g.call("app.child.Child.other", "app/child.py", f"{child}.run", "app/child.py", caller_label="Method",
           strategy="unique_name", conf="0.75", text="self.run")           # the rule asks, callers half
    g.call("app.child.Child.run", "app/child.py", f"{base}.helper", "app/base.py", caller_label="Method",
           strategy="unique_name", conf="0.75", text="self.helper")        # and the callees half
    env = _ask(monkeypatch, g, op, "Child.run")

    walks = [q for q in g.queries if "[:INHERITS]" in q and child in q]
    assert len(walks) == 1, f"the ancestry of Child was read {len(walks)} times: {walks}"
    assert env["result"] is not None and "## Callers through `Base.run`" in env["result"]


# ================================================================ small corrections

def test_a_cut_direct_lookup_does_not_claim_that_nothing_calls_the_symbol(monkeypatch):
    """The direct query was cut short and could not select the target, so "no caller is recorded
    against this symbol itself" is not a thing the answer knows. It says what was seen, carries the
    cap gap, and the total is unknown. CONTROL: a short probe keeps the sentence it always had."""
    from codeintel.graph_edges import _EDGE_ROW_LIMIT

    def shaped(namesake_callers: int) -> _Graph:
        g = _Graph()
        base = g.add_class("Base", file="app/base.py", methods=["run"])
        g.add_class("Child", file="app/child.py", bases=["Base"], inherits=[base], methods=["run"])
        other = g.add_class("Other", file="app/other.py", methods=["run"])
        for i in range(namesake_callers):
            g.call(f"app.u{i}.use", f"app/u{i}.py", f"{other}.run", "app/other.py")
        g.call("app.driver.drive", "app/driver.py", f"{base}.run", "app/base.py",
               strategy="field_type_hint", conf="0.85", text="runner.run")
        return g

    short = _ask(monkeypatch, shaped(5), "callers", "Child.run")
    assert "(no caller is recorded against this symbol itself" in short["result"], short["result"]
    assert "row-cap-reached" not in _gap_kinds(short) and short["evidence"]["truncated"] is False

    env = _ask(monkeypatch, shaped(_EDGE_ROW_LIMIT + 10), "callers", "Child.run")
    body = env["result"]
    assert "## Callers of Child.run (0)" in body, body
    assert "(no caller is recorded against this symbol itself" not in body, body
    assert f"no caller of this symbol itself was found in the {_EDGE_ROW_LIMIT} rows the graph returned" in body
    assert "not proof there is none" in body and "## Callers through `Base.run`" in body, body
    assert "row-cap-reached" in _gap_kinds(env), env["gaps"]
    assert env["evidence"]["truncated"] is True and env["evidence"]["total"] is None, env["evidence"]


def test_a_base_fetch_cut_short_whose_rows_were_all_filtered_still_makes_the_total_unknown(monkeypatch):
    """The base's callers came back cut at the row limit, and every row that did was dropped as a
    cross-language collision, so nothing from that section is shown. The gap says the list was
    truncated; the envelope must say so too, instead of an exact total backed only by the gap."""
    from codeintel.graph_edges import _EDGE_ROW_LIMIT

    g = _Graph()
    base = g.add_class("Base", file="app/base.py", methods=["run"])
    child = g.add_class("Child", file="app/child.py", bases=["Base"], inherits=[base], methods=["run"])
    for i in range(_EDGE_ROW_LIMIT + 10):         # first in the backend's order, so the probe is all of these
        g.call(f"web.c{i}.call", f"web/c{i}.ts", f"{base}.run", "app/base.py", caller_label="Function")
    g.call("tests.t.test_child", "tests/t.py", f"{child}.run", "app/child.py")
    real = g.__class__._answer
    aggregates = {"n": 0}

    def second_aggregate_is_unreadable(self, cypher: str) -> list[dict]:
        if "count(*) AS edge_count" in cypher:
            aggregates["n"] += 1
            if aggregates["n"] >= 2:
                return [{"b.name": "run", "b.qualified_name": base, "b.file_path": "app/base.py"}]
        return real(self, cypher)

    monkeypatch.setattr(g.__class__, "_answer", second_aggregate_is_unreadable)
    env = _ask(monkeypatch, g, "callers", "Child.run")

    assert "## Callers through" not in env["result"], env["result"]
    assert "row-cap-reached" in _gap_kinds(env), env["gaps"]
    assert env["evidence"]["truncated"] is True and env["evidence"]["total"] is None, env["evidence"]
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_class_a_shared_walk_stopped_at_is_not_reported_cut_once_a_later_walk_goes_on(monkeypatch):
    """The hierarchy is shared by everything an op asks of it. A class the depth cap stopped one walk
    at is expanded when another walk starts from it, and its ancestry is then known — it must not stay
    marked as cut, or every later question about it would refuse for a limit that no longer applies."""
    g = _Graph()
    qns = [g.add_class(f"K{i}", file=f"app/k{i}.py", methods=["m"]) for i in range(_MAX_DEPTH + 3)]
    for i in range(len(qns) - 1):
        g.classes[qns[i]]["bases"] = [f"K{i + 1}"]
        g.classes[qns[i]]["inherits"] = [qns[i + 1]]
    p = _provider(monkeypatch, g)
    from codeintel.graph_dispatch import _ClassNode

    def node(qn: str) -> _ClassNode:
        c = g.classes[qn]
        return _ClassNode(qn, c["name"], c["file"], tuple(c["bases"]))

    with p._lookup_scope(30000):
        first = p._grow_hierarchy([node(qns[0])], PROJECT, 30000)
        assert first.cut_above(qns[0]), "the walk from K0 should stop at the depth cap"
        cut_class = qns[_MAX_DEPTH]
        assert cut_class in first.cut_at
        later = p._grow_hierarchy([node(cut_class)], PROJECT, 30000)
        assert later is first and not later.cut_above(cut_class), later.cut_at


def test_a_method_listed_twice_is_still_one_method(monkeypatch):
    """A method with overloads, or a property with a setter, can be listed once per definition. It
    is ONE method, so its bases are looked up — they used to be skipped without a word."""
    graph, _, _ = _nominal_graph(inherits=True)
    real = graph.__class__._answer

    def listed_twice(self, cypher: str) -> list[dict]:
        rows = real(self, cypher)
        return rows * 2 if 'WHERE m.name="run"' in cypher else rows

    monkeypatch.setattr(graph.__class__, "_answer", listed_twice)
    env = _ask(monkeypatch, graph, "callers", "Child.run")

    assert "## Callers through `Base.run`" in env["result"], env["result"]


def test_two_different_methods_matching_the_target_are_a_named_shortfall_and_not_a_silent_skip(
        monkeypatch):
    """Two classes called `Child` define `run` and nothing narrows the target to one. Which one's
    bases to look up is not settled, which is a gap — not a lookup that quietly did not happen."""
    from codeintel.graph_dispatch import _DirectTarget
    from codeintel.graph_targets import _parse_symbol_target

    g = _Graph()
    g.add_class("Child", file="app/child.py", methods=["run"])
    g.add_class("Child", file="app/more/child.py", methods=["run"])
    out = _provider(monkeypatch, g)._dispatch_bases(
        _parse_symbol_target("run"), _DirectTarget(1, "", True), PROJECT, 30000)

    assert out.target is None and out.bases == [], out
    assert len(out.shortfalls) == 1 and "2 methods named `run` match this target" in out.shortfalls[0]
    assert out.unknown is True


def test_the_headline_names_the_class_hierarchy_among_the_ways_a_row_is_bound(monkeypatch):
    """The first line of an answer that mixes resolved and name-matched rows says which mechanisms
    count as resolved. A `self_mro` row is resolved by a fourth, and a reader told only about three
    would doubt it."""
    graph, _, _ = _self_graph(texts=(("a", "self.render"), ("c", "ops.render")))
    body = _ask(monkeypatch, graph, "callers", "Mixin.render")["result"]

    assert "1 resolved · 1 name-matched" in body, body
    assert "the caller's own class hierarchy (`self_mro`, over INHERITS edges)" in body, body
