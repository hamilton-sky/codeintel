"""Three fixes that each turn a confidently wrong answer into an honest one.

* `no-edges` vs `not-in-graph` — "the symbol is not indexed" and "the symbol is indexed and nothing
  calls it" were reported identically, with a re-index hint that cannot help the second. An agent
  reading it about `forward_released_item` (defined at proxy.py:392, registered as a callback) would
  conclude the method is unused and delete a live one.
* `changed` ripple — the op reported the symbols DEFINED in a touched file and called that impact.
  Editing one function in `budget.ts` listed all seven symbols of that file and never mentioned
  `runAlerts`, the caller in another file that the edit actually reaches.
* the gateway cross-check — `_AUTO_ENGINE` is a static map, and `callers` had no fallback of any
  kind, so when the graph answered entirely with name guesses the LSP that held the right answer
  was never asked.
"""
from __future__ import annotations

from codeintel.gateway import Gateway
from codeintel.providers.graph import GraphProvider

ROOT = "/Users/x/Documents/project/codeintel"
LIST_PROJECTS = {"projects": [{"name": "codeintel", "root_path": ROOT}]}


def _provider(monkeypatch, *, edge_rows, node_rows):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    # The node probe is the only query whose Cypher has no relationship pattern.
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: (
        list(node_rows) if "-[" not in cypher else list(edge_rows)))
    return p


def test_an_indexed_symbol_with_no_callers_is_not_reported_as_absent(monkeypatch):
    node = [{"n.qualified_name": "codeintel.proxy.DataPlaneApp.forward_released_item",
             "n.file_path": "src/proxy.py"}]
    env = _provider(monkeypatch, edge_rows=[], node_rows=node).build_result(
        "callers", "forward_released_item", [], 30000, ROOT)

    assert env["result"] is None                       # still a safe null — nothing was found
    assert env["reason"] == "no-edges", env
    hint = env["hint"]
    assert "IS indexed" in hint and "src/proxy.py" in hint
    # The two claims that made the old answer dangerous must both be gone.
    assert "codeintel index" not in hint, hint
    assert "not in the graph index" not in hint, hint
    assert "dead code" in hint                          # names the misreading it exists to block


def test_a_genuinely_absent_symbol_still_says_so_and_still_offers_the_reindex(monkeypatch):
    """The negative control. Widening `no-edges` to cover real misses would destroy the very
    distinction the branch was added to draw."""
    env = _provider(monkeypatch, edge_rows=[], node_rows=[]).build_result(
        "callers", "nope", [], 30000, ROOT)
    assert env["reason"] == "not-in-graph", env
    assert "codeintel index" in env["hint"]


def test_changed_reports_callers_outside_the_edit_not_just_symbols_inside_it(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    detect = {"changed_files": ["src/domain/budget.ts"],
              "impacted_symbols": [{"name": "evaluate", "qualified_name": "domain.budget.evaluate",
                                    "file_path": "src/domain/budget.ts"}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects"
        else detect if method == "detect_changes" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: [
        {"a.qualified_name": "codeintel.src.app.alert.runAlerts",
         "a.file_path": "src/app/alert.ts", "c.confidence": "0.95"},
        {"a.qualified_name": "codeintel.src.app.alert.runAlerts",
         "a.file_path": "src/app/alert.ts", "c.confidence": "0.75"},   # same caller, weaker edge
        {"a.qualified_name": "codeintel.test.unit.budget.test",
         "a.file_path": "test/unit/budget.test.ts", "c.confidence": "0.38"},
    ])
    body = p.build_result("changed", "", [], 30000, ROOT)["result"]

    assert "runAlerts" in body, body                      # the row that used to be missing entirely
    assert "Callers elsewhere" in body
    # The containment list is no longer called "impacted" — it never was impact.
    assert "Symbols defined in the changed files" in body
    # One caller reaching two changed symbols is one thing to review, at its best score.
    assert body.count("runAlerts") == 1, body
    assert "[?0.75]" not in body, "a caller's best edge should win, not its worst"
    assert "[!0.38]" in body                              # and a weak caller still says so


def test_changed_says_zero_ripple_out_loud_rather_than_omitting_the_section(monkeypatch):
    """An absent section and an empty one read the same to a model. Only one of them is a claim."""
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    detect = {"changed_files": ["src/leaf.py"], "impacted_symbols": []}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects"
        else detect if method == "detect_changes" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: [])
    body = p.build_result("changed", "", [], 30000, ROOT)["result"]
    assert "Callers elsewhere that reach into them (0)" in body
    assert "not proof that nothing else is affected" in body


class _StubLsp:
    available = True

    def __init__(self, result):
        self._result = result

    def build_result(self, op, target, files, budget, project_root, **kw):
        return dict(self._result)


class _StubGraph:
    available = True

    def __init__(self, env):
        self._env = env

    def build_result(self, op, target, files, budget, project_root, **kw):
        return dict(self._env)


def _graph_env(gaps):
    return {"ok": True, "op": "callers", "target": "describe", "engine": "graph",
            "result": "## Callers of describe (6)\n- a\n- b", "confidence": "partial",
            "gaps": gaps}


def test_an_all_guesses_answer_is_cross_checked_against_the_lsp():
    lsp = {"ok": True, "result": "## Symbol: describe\n**Function** — src/domain/budget.ts:99\n"
                                 "```\nbody\n```\n\n## References (2)\n"
                                 "- src/app/alert.ts:121  (runAlerts)\n"
                                 "- test/unit/budget.test.ts:101  (t)\n",
           "engine": "lsp", "confidence": "complete"}
    gw = Gateway(graph=_StubGraph(_graph_env([{"section": "callers",
                                               "kind": "all-rows-name-resolved",
                                               "detail": "d"}])),
                 lsp=_StubLsp(lsp))
    env = gw.query("callers", "describe", engine="auto", project_root=ROOT, budget=30000)
    body = env["result"]

    assert "## Cross-check — LSP references to `describe` (2)" in body, body
    # The caller the graph could not bind (aliased import) is what the section exists to surface.
    assert "src/app/alert.ts:121" in body
    # The definition body is NOT repeated — only the reference rows.
    assert "```" not in body and "**Function**" not in body, body
    assert any(g["kind"] == "cross-checked-with-lsp" for g in env["gaps"]), env["gaps"]


def test_a_healthy_graph_answer_is_never_sent_to_the_lsp():
    """The escalation is for the collision signature only. Firing it on ordinary answers would
    double every callers query's cost for nothing."""
    calls: list[str] = []

    class _Counting(_StubLsp):
        def build_result(self, op, target, files, budget, project_root, **kw):
            calls.append(op)
            return super().build_result(op, target, files, budget, project_root, **kw)

    gw = Gateway(graph=_StubGraph(_graph_env([])), lsp=_Counting({"ok": True, "result": "x"}))
    gw.query("callers", "describe", engine="auto", project_root=ROOT, budget=30000)
    assert calls == [], calls


def test_a_pinned_engine_is_never_second_guessed():
    """`--engine graph` is the caller saying which engine they want. Appending another engine's
    answer to it would make the flag mean something other than what it says."""
    calls: list[str] = []

    class _Counting(_StubLsp):
        def build_result(self, op, target, files, budget, project_root, **kw):
            calls.append(op)
            return super().build_result(op, target, files, budget, project_root, **kw)

    gw = Gateway(graph=_StubGraph(_graph_env([{"section": "callers",
                                               "kind": "all-rows-name-resolved", "detail": "d"}])),
                 lsp=_Counting({"ok": True, "result": "x"}))
    gw.query("callers", "describe", engine="graph", project_root=ROOT, budget=30000)
    assert calls == [], calls


def test_a_warming_language_server_is_reported_as_retryable_not_as_agreement():
    """Silence from the LSP is not confirmation, and "not booted yet" is the one cause that asking
    again actually fixes — so it must not be flattened into "had nothing"."""
    gw = Gateway(graph=_StubGraph(_graph_env([{"section": "callers",
                                               "kind": "all-rows-name-resolved", "detail": "d"}])),
                 lsp=_StubLsp({"ok": True, "result": None, "reason": "warming"}))
    env = gw.query("callers", "describe", engine="auto", project_root=ROOT, budget=30000)
    gap = next(g for g in env["gaps"] if g["kind"] == "cross-check-unavailable")
    assert gap["reason"] == "warming"
    assert gap.get("retry_after_s")
    assert "had not finished booting" in env["result"]


# --------------------------------------------------------------------------- #
# relationship KIND, which is a different axis from confidence
# --------------------------------------------------------------------------- #

def _kinded(monkeypatch, rows, target="target"):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: list(rows))
    return p.build_result("callers", target, [], 30000, ROOT)


def _row(kind: str, i: int = 0) -> dict:
    return {"a.name": f"c{i}", "a.qualified_name": f"pkg.c{i}", "a.file_path": f"src/c{i}.py",
            "labels(a)": "Function", "type(c)": kind,
            "b.name": "target", "b.qualified_name": "pkg.target", "b.file_path": "src/t.py"}


def test_the_query_asks_for_the_relationship_that_records_a_callback(monkeypatch):
    """The whole defect in one assertion. `set_forward_fn(app.forward_released_item)` is stored by
    the backend as CALL_REFERENCE; codeintel matched `[:CALLS|USAGE]` only, so a method registered
    at two real sites came back as having no callers — the reading that deletes live code."""
    seen: list[str] = []
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda m, pay, t: LIST_PROJECTS if m == "list_projects" else None)
    monkeypatch.setattr(p, "_query_rows",
                        lambda cypher, project, t: (seen.append(cypher), [_row("CALLS")])[1])
    p.build_result("callers", "target", [], 30000, ROOT)
    assert seen and "CALL_REFERENCE" in seen[0], seen[0]


def test_a_registration_is_not_counted_as_a_caller(monkeypatch):
    env = _kinded(monkeypatch, [_row("CALL_REFERENCE", 0), _row("CALL_REFERENCE", 1)])
    body = env["result"]
    # The count that matters: zero things call it.
    assert "(0 direct, 2 other reference(s))" in body, body
    assert "[CALL_REFERENCE]" in body
    assert "passed as a value or registered as a callback" in body
    assert any(g["kind"] == "non-call-relationships" for g in env["gaps"]), env["gaps"]


def test_calls_are_listed_before_the_rows_that_are_not_calls(monkeypatch):
    """An agent reads top-down. The answer to the question asked goes first."""
    body = _kinded(monkeypatch, [_row("CALL_REFERENCE", 0), _row("CALLS", 1)])["result"]
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert "[CALLS]" in lines[0] and "[CALL_REFERENCE]" in lines[1], lines


def test_an_all_calls_answer_keeps_its_plain_count(monkeypatch):
    """The counterweight: splitting a heading that has nothing to split is noise, and the common
    case must read exactly as it did before."""
    env = _kinded(monkeypatch, [_row("CALLS", 0), _row("CALLS", 1)])
    assert "Callers of target (2)" in env["result"]
    assert "Not calls" not in env["result"]
    # No gaps at all — `attach_confidence` omits the key entirely when nothing is missing, which is
    # the shape a fully-answered query has to keep.
    assert env["confidence"] == "complete"
    assert not any(g["kind"] == "non-call-relationships" for g in env.get("gaps", []))


def test_no_edges_names_the_relationships_that_do_point_at_the_symbol(monkeypatch):
    """"No callers" plus silence reads as "unused". Naming what DOES reference it is the difference
    between an answer and a dead end."""
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda m, pay, t: LIST_PROJECTS if m == "list_projects" else None)

    def rows(cypher, project, timeout_ms):
        if "type(c) AS kind" in cypher:                       # the dependency census
            return [{"kind": "DECORATES", "n": "3"}, {"kind": "DEFINES", "n": "1"},
                    {"kind": "TESTS", "n": "2"}]
        if "-[" not in cypher:                                # the node-existence probe
            return [{"n.qualified_name": "pkg.route", "n.file_path": "src/api.py"}]
        return []                                             # no CALLS/USAGE/CALL_REFERENCE
    monkeypatch.setattr(p, "_query_rows", rows)

    env = p.build_result("callers", "route", [], 30000, ROOT)
    assert env["reason"] == "no-edges"
    hint = env["hint"]
    assert "3 DECORATES" in hint and "2 TESTS" in hint, hint
    # Structural edges say where a symbol lives, not what depends on it.
    assert "DEFINES" not in hint, hint


def test_a_zero_count_is_never_rendered_as_a_relationship(monkeypatch):
    """The backend names an unaliased aggregate column `COUNT(*)`, uppercased, so a lookup by the
    written `count(*)` missed and every kind printed as "0 x KIND" — a fact invented by a parse
    miss. The query is aliased now; this pins that a zero can never reach the text."""
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda m, pay, t: LIST_PROJECTS if m == "list_projects" else None)

    def rows(cypher, project, timeout_ms):
        if "type(c) AS kind" in cypher:
            return [{"kind": "DATA_FLOWS", "n": "0"}, {"kind": "HANDLES"}]   # zero, and missing
        if "-[" not in cypher:
            return [{"n.qualified_name": "pkg.x", "n.file_path": "src/x.py"}]
        return []
    monkeypatch.setattr(p, "_query_rows", rows)

    hint = p.build_result("callers", "x", [], 30000, ROOT)["hint"]
    assert "0 DATA_FLOWS" not in hint, hint
    assert "Other relationships DO point at it" not in hint, hint
