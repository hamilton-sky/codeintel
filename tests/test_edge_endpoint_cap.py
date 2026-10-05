"""The cap on `callers` / `callees` bounds DISTINCT CALLERS of the symbol asked about — not raw edges.

The defect this file exists against: `codeintel query --op callers --target run@bench/score.py` said
"Truncated: yes — 5 shown, total unknown … the graph returned the maximum 50 rows" about a symbol
with six callers. The first query asks for the edges of every symbol named `run`, `LIMIT 50` counts
all of them, and forty-five of the fifty belonged to a different `run` (`server.run`, ninety-eight
callers of its own) that the target hint then threw away. The cap was applied before the selection,
so it bounded the wrong thing, and which rows survived it was whichever the backend returned first —
on `GraphProvider.build_result` twenty rows, every one a test function.

The backend here is a small in-memory stand-in that answers the three query shapes the ops send and
OBEYS `LIMIT` and `IN [...]`. A stub that returns the same rows to every query cannot show this
defect: the whole point is that the first answer is cut and a later question recovers what it lost.
"""
from __future__ import annotations

import re

from codeintel.graph_edges import _EDGE_FETCH_CEILING, _EDGE_ROW_LIMIT
from codeintel.providers.graph import GraphProvider

ROOT = "/Users/x/Documents/project/codeintel"
LIST_PROJECTS = {"projects": [{"name": "codeintel", "root_path": ROOT}]}


def _edge(caller: str, *, target: str = "target", target_file: str = "src/t.py",
          target_qn: str | None = None, caller_file: str | None = None, kind: str = "CALLS",
          strategy: str = "import_map", conf: str = "0.95") -> dict:
    """One edge, with every column either op's query asks for."""
    return {
        "a.name": caller, "a.qualified_name": f"pkg.{caller}",
        "a.file_path": caller_file or f"src/pkg/{caller}.py", "labels(a)": "Function",
        "type(c)": kind, "c.confidence": conf, "strategy": strategy, "callee": target,
        "b.name": target, "b.qualified_name": target_qn or f"pkg.{target}",
        "b.file_path": target_file, "labels(b)": "Function",
    }


class _EdgeBackend:
    """Answers the edge ops' three query shapes over a list of edges, in insertion order.

    Insertion order IS the backend's return order, which is what lets a test place the rows that
    matter beyond a cap — the thing the real backend does by accident."""

    def __init__(self, edges: list[dict]) -> None:
        self.edges = list(edges)
        self.queries: list[str] = []

    def __call__(self, cypher: str, project: str, timeout_ms: int) -> list[dict]:
        self.queries.append(cypher)
        fixed = "b" if 'WHERE b.name="' in cypher else "a"
        name = re.search(rf'WHERE {fixed}\.name="([^"]*)"', cypher).group(1)
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
        limit = int(re.search(r"LIMIT (\d+)\s*$", cypher).group(1))
        return [dict(e) for e in hits[:limit]]


def _ask(monkeypatch, edges: list[dict], op: str, target: str) -> tuple[dict, _EdgeBackend]:
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    backend = _EdgeBackend(edges)
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", backend)
    return p.build_result(op, target, [], 30000, ROOT), backend


def _row_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


def _cap_gaps(env: dict) -> list[dict]:
    return [g for g in env.get("gaps", []) if g["kind"] == "row-cap-reached"]


def test_sixty_edges_from_three_callers_are_three_callers_and_the_answer_is_not_truncated(
        monkeypatch):
    """Twenty edges each — the shape a backend that records one edge per call site produces, and the
    reproduction in the brief for this fix — so the probe is full after fifty edges and has seen
    only part of the third caller. Three callers is what there are, and the answer has to say
    exactly that, not "the maximum 50 rows were returned" about a symbol the backend knows has three."""
    edges = [_edge(f"caller{c}") for c in range(3) for _ in range(20)]
    assert len(edges) == 60 > _EDGE_ROW_LIMIT, "the fixture must overflow the probe"

    env, _ = _ask(monkeypatch, edges, "callers", "target")

    ev = env["evidence"]
    assert ev["returned"] == 3 and ev["total"] == 3 and ev["truncated"] is False, ev
    assert len(_row_lines(env["result"])) == 3, env["result"]
    assert not _cap_gaps(env), env["gaps"]
    assert "Truncated" not in env["result"], env["result"]
    assert env["confidence"] == "complete" and ev["safe_for_destructive"] is True, env


def test_a_namesake_with_many_callers_cannot_use_up_the_cap_of_the_symbol_asked_about(monkeypatch):
    """The reproduction. `a.target` has six callers; `b.target` has ninety-eight; the backend returns
    five of the first and then the second's before it reaches the sixth. The probe is full of the
    wrong symbol's edges, and the sixth caller is past it."""
    mine = {"target_qn": "pkg.a.target", "target_file": "src/a.py"}
    edges = ([_edge(f"mine{i}", **mine) for i in range(5)]
             + [_edge(f"theirs{i}", target_qn="pkg.b.target", target_file="src/b.py")
                for i in range(98)]
             + [_edge("mine5", **mine)])

    env, backend = _ask(monkeypatch, edges, "callers", "target@src/a.py")

    ev = env["evidence"]
    assert ev["returned"] == 6 and ev["total"] == 6 and ev["truncated"] is False, ev
    assert sorted(r["name"] for r in env["rows"]) == [f"mine{i}" for i in range(6)], env["rows"]
    assert not _cap_gaps(env), env["gaps"]
    assert "Truncated" not in env["result"] and "total unknown" not in env["result"]
    # What was asked, in order: the probe, what the backend holds, then just that symbol's edges.
    assert len(backend.queries) == 3, backend.queries
    assert "count(*) AS edge_count" in backend.queries[1]
    assert 'file_path IN ["src/a.py"]' in backend.queries[2], backend.queries[2]


def test_more_distinct_callers_than_the_cap_is_truncated_with_an_exact_total(monkeypatch):
    """The other direction, and the reason the cap still exists. Sixty-three callers is more than a
    reader will read, so fifty are printed — and the answer knows the total, because it fetched every
    edge before dropping any. "50 shown, total unknown" was a statement about our own query."""
    edges = [_edge(f"caller{i}") for i in range(63)]

    env, _ = _ask(monkeypatch, edges, "callers", "target")

    ev = env["evidence"]
    assert ev["returned"] == 50 and ev["total"] == 63 and ev["truncated"] is True, ev
    assert ev["safe_for_destructive"] is False
    assert env["confidence"] == "partial", env
    assert "> Truncated: yes — 50 shown, 63 in total" in env["result"], env["result"][:300]
    (gap,) = _cap_gaps(env)
    assert "63 distinct callers exist and 50 are shown" in gap["detail"], gap
    assert "_Truncated: 50 of 63 distinct callers are shown" in env["result"], env["result"]
    # The note, the envelope and the printed list are one count.
    assert len(_row_lines(env["result"])) == ev["returned"] == len(env["rows"])


def test_production_callers_survive_truncation_ahead_of_sixty_test_callers(monkeypatch):
    """The heavily-tested symbol. The backend returns its sixty test callers first and its three
    production callers last, so a cap that keeps "whichever came first" keeps no production code at
    all — which is exactly what `GraphProvider.build_result` answered. Ranked, the three are on the
    first screen and the tests are what gets left out."""
    edges = ([_edge(f"t{i}", caller_file=f"tests/test_mod{i}.py") for i in range(60)]
             + [_edge(f"prod{i}", caller_file=f"src/codeintel/app{i}.py") for i in range(3)])

    env, _ = _ask(monkeypatch, edges, "callers", "target")

    names = [r["name"] for r in env["rows"]]
    assert len(names) == 50 and {"prod0", "prod1", "prod2"} <= set(names), names
    assert names[:3] == ["prod0", "prod1", "prod2"], "production code must be listed first"
    printed = _row_lines(env["result"])
    assert all("prod" in ln for ln in printed[:3]), printed[:4]
    ev = env["evidence"]
    assert ev["returned"] == 50 and ev["total"] == 63 and ev["truncated"] is True, ev


def test_the_truncation_says_how_many_missing_callers_are_tests_and_how_many_production(
        monkeypatch):
    """"13 more" is not the question. The question is whether what is missing is the part that
    matters, and the answer to it is already known: the omitted endpoints are classified, not
    guessed. Here fifteen are missing — ten tests and five production callers."""
    edges = ([_edge(f"p{i}", caller_file=f"src/codeintel/mod{i}.py") for i in range(55)]
             + [_edge(f"t{i}", caller_file=f"tests/test_mod{i}.py") for i in range(10)])

    env, _ = _ask(monkeypatch, edges, "callers", "target")

    (gap,) = _cap_gaps(env)
    assert "65 distinct callers exist and 50 are shown" in gap["detail"], gap
    assert "(10 in test files, 5 in production code)" in gap["detail"], gap
    assert "(10 in test files, 5 in production code)" in env["result"], env["result"]
    # Production is ranked ahead of tests, so every test caller is among the omitted.
    assert not [r for r in env["rows"] if r["file"].startswith("tests/")], env["rows"]


def test_callees_are_capped_by_distinct_callee_not_by_edge(monkeypatch):
    """The mirror. One function with three callees and twenty call sites of each is three callees."""
    edges = [_edge("wide", target=f"callee{c}") for c in range(3) for _ in range(20)]

    env, backend = _ask(monkeypatch, edges, "callees", "wide")

    ev = env["evidence"]
    assert ev["returned"] == 3 and ev["total"] == 3 and ev["truncated"] is False, ev
    assert not _cap_gaps(env), env["gaps"]
    assert len(backend.queries) == 3, "the probe was full, so it had to be followed up"


def test_two_edges_between_the_same_pair_are_one_caller_and_the_stronger_one_wins(monkeypatch):
    """A backend that records more than one edge per pair (0.10.8 does not — its maximum over this
    repository's index is one — but the cap's unit is the caller on any backend) would otherwise
    print the pair twice and count it twice. The weaker edge is folded into the stronger, not listed
    beside it — a caller with a bound edge and a guess to the same symbol has a bound edge — and
    when the probe is short the common case still costs exactly one query."""
    edges = ([_edge("both", strategy="suffix_match", conf="0.04"),
              _edge("both", strategy="import_map", conf="0.95")]
             + [_edge(f"other{i}") for i in range(3)])

    env, backend = _ask(monkeypatch, edges, "callers", "target")

    rows = {r["name"]: r for r in env["rows"]}
    assert len(env["rows"]) == 4 and rows["both"]["verified"] is True, env["rows"]
    assert rows["both"]["strategy"] == "import_map", rows["both"]
    assert env["evidence"]["possible"] == 0
    assert len(backend.queries) == 1, backend.queries


def test_a_follow_up_that_is_not_an_answer_leaves_the_conservative_cap_note(monkeypatch):
    """Every way the follow-up can fail returns to what a full probe has always meant: a list that
    may be missing rows, with the total unknown. Here the backend answers the aggregate with edge
    rows — a reply this code was not written to read — and nothing is guessed from it."""
    edges = [_edge(f"caller{i}") for i in range(_EDGE_ROW_LIMIT + 10)]
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    seen: list[str] = []
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: (
        seen.append(cypher), [dict(e) for e in edges[:_EDGE_ROW_LIMIT]])[1])

    env = p.build_result("callers", "target", [], 30000, ROOT)

    assert len(seen) == 2 and "count(*) AS edge_count" in seen[1], seen
    ev = env["evidence"]
    assert ev["truncated"] is True and ev["total"] is None and ev["returned"] == 50, ev
    assert "> Truncated: yes — 50 shown, total unknown" in env["result"], env["result"][:300]
    assert f"the graph returned the maximum {_EDGE_ROW_LIMIT} rows" in env["result"]
    assert len(_cap_gaps(env)) == 1, env["gaps"]


def test_a_symbol_past_the_fetch_ceiling_is_reported_as_an_unknown_total(monkeypatch):
    """The ceiling exists so one pathological name cannot ask the backend for everything. Reaching
    it is reported the way the probe's own cap always was — total unknown, and the number in the
    sentence is the one that was actually the limit."""
    edges = [_edge(f"caller{i}") for i in range(_EDGE_FETCH_CEILING + 100)]

    env, _ = _ask(monkeypatch, edges, "callers", "target")

    ev = env["evidence"]
    assert ev["truncated"] is True and ev["total"] is None, ev
    assert f"the graph returned the maximum {_EDGE_FETCH_CEILING} rows" in env["result"]
    assert env["evidence"]["safe_for_destructive"] is False and env["confidence"] == "partial"
