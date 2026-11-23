"""Known graph blind spots add a gap, so `safe_for_destructive` is never stamped on what the graph cannot see.

The findings behind this file (`.feature-flow/state/redblue-findings.md`):

* `complete-callees-miss-aliased-import`: `from pkg import helper as h` then `h(2)` gives no edge to
  `helper`, and `callees entry` / `callers helper` still said complete and safe.
* `nested-fn-false-verified-caller`: `outer` defines its own nested `inner` and calls it; the index
  merged the two `inner`s, so `outer` came back as a verified (`lsp_direct`) caller of the module
  `inner`, and the footer said it was "the only symbol named `inner`".
* `ts-safe-for-destructive-wrong-half`: the same in TypeScript, where the false row carried
  `lsp_ts_local`, which was taken as a language-server resolution on its own.

The rows below are shaped like the 0.9.0 (and, where they differ, 0.10.x) rows those repros
returned. The source the rules read is the fixture tree under `tests/fixtures/blindspots/`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from codeintel.engines.graph import qualifier_scan
from codeintel.engines.graph.provider import GraphProvider

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "blindspots")
PY_ROOT = os.path.realpath(os.path.join(FIXTURES, "py"))
TS_ROOT = os.path.realpath(os.path.join(FIXTURES, "ts"))
PROJECT = "blindspots"


def _row(caller: str, caller_file: str, callee: str, callee_file: str, *, edge: str = "CALLS",
         strategy: str | None = None, conf: str | None = None, labels: str = "Function",
         caller_name: str | None = None) -> dict:
    row = {
        "a.name": caller_name or caller.rsplit(".", 1)[-1],
        "a.qualified_name": f"{PROJECT}.{caller}",
        "a.file_path": caller_file,
        "labels(a)": labels,
        "type(c)": edge,
        "b.name": callee.rsplit(".", 1)[-1],
        "b.qualified_name": f"{PROJECT}.{callee}",
        "b.file_path": callee_file,
        "labels(b)": "Function",
    }
    if strategy is not None:
        row["strategy"] = strategy
    if conf is not None:
        row["c.confidence"] = conf
    return row


def _module_scope(file: str, callee: str, callee_file: str) -> dict:
    """A 0.9.0 USAGE row from a file's module scope: the import line, hung off the file node."""
    stem = file.rsplit("/", 1)[-1]
    module = file[:-3].replace("/", ".")
    return _row(f"{module}.py.__file__", file, callee, callee_file, edge="USAGE",
                labels='["File"]', caller_name=stem)


def _ask(monkeypatch, root: str, op: str, target: str, rows: list[dict]) -> dict:
    monkeypatch.setattr(
        "codeintel.engines.graph.provider.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    listing = {"projects": [{"name": PROJECT, "root_path": root}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))
    # Only the edge query answers with the rows; every lookup beyond it (bases, owners, `self.`
    # hierarchy) finds nothing, as it would for these plain functions.
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: (
        list(rows) if "-[c:" in cypher and "count(*)" not in cypher else []))
    return p.build_result(op, target, [], 30000, root)


def _gaps(env: dict, kind: str) -> list[dict]:
    return [g for g in env.get("gaps") or [] if g["kind"] == kind]


def _row_named(env: dict, name: str) -> dict:
    return next(r for r in env["rows"] if r["name"] == name)


# ---- rows as 0.9.0 returned them for the Python repro ---------------------------------------

HELPER_CALLERS_09 = [
    _row("pkg.sub.impl.process", "pkg/sub/impl.py", "pkg.sub.impl.helper", "pkg/sub/impl.py",
         strategy="lsp_direct", conf="0.95"),
    _module_scope("main.py", "pkg.sub.impl.helper", "pkg/sub/impl.py"),
    _module_scope("alias_direct.py", "pkg.sub.impl.helper", "pkg/sub/impl.py"),
    _module_scope("pkg/__init__.py", "pkg.sub.impl.helper", "pkg/sub/impl.py"),
]

# 0.10.x binds the direct alias and returns no module-scope rows at all, so on main this answer
# was complete and safe while `main.entry`'s `h(2)` was missing.
HELPER_CALLERS_010 = [
    _row("pkg.sub.impl.process", "pkg/sub/impl.py", "pkg.sub.impl.helper", "pkg/sub/impl.py",
         strategy="lsp_direct", conf="0.95"),
    _row("alias_direct.via_alias_direct", "alias_direct.py", "pkg.sub.impl.helper",
         "pkg/sub/impl.py", strategy="lsp_callable_alias", conf="0.95"),
]

ENTRY_CALLEES = [
    _row("main.entry", "main.py", "pkg.sub.impl.process", "pkg/sub/impl.py",
         strategy="import_map_suffix", conf="0.95"),
]

INNER_CALLERS_PY = [
    _row("pkg.shapes.outer", "pkg/shapes.py", "pkg.shapes.inner", "pkg/shapes.py",
         strategy="lsp_direct", conf="0.95"),
    _row("pkg.shapes.calls_module_inner", "pkg/shapes.py", "pkg.shapes.inner", "pkg/shapes.py",
         strategy="lsp_direct", conf="0.95"),
]

INNER_CALLERS_TS = [
    _row("src.shapes.outer", "src/shapes.ts", "src.shapes.inner", "src/shapes.ts",
         strategy="lsp_ts_local", conf="0.95"),
]

HELPER_CALLERS_TS = [
    _row("src.lib.impl.process", "src/lib/impl.ts", "src.lib.impl.helper", "src/lib/impl.ts",
         strategy="lsp_ts_local", conf="0.95"),
]


# ---- aliased imports ------------------------------------------------------------------------

@pytest.mark.parametrize("rows", [HELPER_CALLERS_09, HELPER_CALLERS_010], ids=["0.9.0", "0.10.x"])
def test_callers_of_a_symbol_imported_under_an_alias_is_not_safe(monkeypatch, rows):
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", rows)

    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    detail = " ".join(g["detail"] for g in gaps)
    assert "alias_direct.py" in detail and "`h2`" in detail, detail
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False
    # Said in the body too, where a reader looks.
    assert "h2" in env["result"], env["result"]


def test_the_alias_gap_names_the_file_and_the_alias_of_a_module_scope_row(monkeypatch):
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", HELPER_CALLERS_09)
    detail = " ".join(g["detail"] for g in _gaps(env, "aliased-import-unbound"))
    assert "main.py" in detail and "`h`" in detail, detail


def test_callees_of_a_body_calling_an_import_alias_with_no_row_is_not_complete(monkeypatch):
    env = _ask(monkeypatch, PY_ROOT, "callees", "entry", ENTRY_CALLEES)

    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    assert "`h`" in gaps[0]["detail"] and "main.py" in gaps[0]["detail"], gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


def test_callees_whose_alias_call_has_its_row_raises_no_alias_gap(monkeypatch):
    """`via_alias_direct` calls `h2(1)` and the backend bound it to `helper`: nothing is missing."""
    rows = [_row("alias_direct.via_alias_direct", "alias_direct.py", "pkg.sub.impl.helper",
                 "pkg/sub/impl.py", strategy="lsp_callable_alias", conf="0.95")]
    env = _ask(monkeypatch, PY_ROOT, "callees", "via_alias_direct", rows)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["confidence"] == "complete", env.get("gaps")


def test_a_file_that_does_not_exist_adds_nothing(monkeypatch):
    """Positive evidence only: the rule fires on text it read."""
    rows = [_row("pkg.gone.caller", "pkg/gone.py", "pkg.sub.impl.helper", "pkg/sub/impl.py",
                 strategy="lsp_direct", conf="0.95")]
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", rows)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_root_that_does_not_exist_adds_nothing(monkeypatch):
    env = _ask(monkeypatch, "/nonexistent/blindspots/root", "callers", "helper", HELPER_CALLERS_010)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_file_the_budget_could_not_read_adds_the_gap_as_could_not_check(monkeypatch):
    monkeypatch.setattr(qualifier_scan, "_MAX_FILES", 0)
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", HELPER_CALLERS_010)

    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    assert "could not check" in " ".join(g["detail"] for g in gaps), gaps
    assert env["evidence"]["safe_for_destructive"] is False


# ---- nested shadowing -----------------------------------------------------------------------

@pytest.mark.parametrize(("root", "rows"), [(PY_ROOT, INNER_CALLERS_PY), (TS_ROOT, INNER_CALLERS_TS)],
                         ids=["python", "typescript"])
def test_a_caller_that_defines_its_own_nested_namesake_is_not_resolved(monkeypatch, root, rows):
    env = _ask(monkeypatch, root, "callers", "inner", rows)

    outer = _row_named(env, "outer")
    assert outer["verified"] is False, outer
    assert outer["evidence"] != "resolved", outer
    ambiguous = _gaps(env, "target-ambiguous")
    assert ambiguous, env.get("gaps")
    assert "outer" in ambiguous[0]["detail"] and "inner" in ambiguous[0]["detail"], ambiguous
    assert "only symbol named" not in env["result"], env["result"]
    assert env["evidence"]["safe_for_destructive"] is False


def test_the_caller_without_a_nested_namesake_stays_resolved(monkeypatch):
    env = _ask(monkeypatch, PY_ROOT, "callers", "inner", INNER_CALLERS_PY)
    assert _row_named(env, "calls_module_inner")["verified"] is True


# ---- lsp_ts_local ---------------------------------------------------------------------------

def test_an_lsp_ts_local_row_is_possible_not_verified(monkeypatch):
    env = _ask(monkeypatch, TS_ROOT, "callers", "helper", HELPER_CALLERS_TS)

    row = _row_named(env, "process")
    assert row["verified"] is False, row
    assert row["evidence"] == "name-matched", row
    assert "lsp_ts_local" in row["why"] and "not" in row["why"], row["why"]
    assert env["evidence"]["possible"] == 1 and env["evidence"]["verified"] == 0, env["evidence"]
    assert env["evidence"]["safe_for_destructive"] is False


def test_lsp_ts_local_is_name_matched_wherever_it_is_fake_rooted(monkeypatch):
    """The strategy alone decides, with no file to read: the made-up root of most graph tests."""
    env = _ask(monkeypatch, "/nonexistent/ts/root", "callers", "helper", HELPER_CALLERS_TS)
    assert _row_named(env, "process")["verified"] is False
    assert env["evidence"]["safe_for_destructive"] is False


# ---- sibling and interface dispatch ----------------------------------------------------------
#
# `dynamic-dispatch-asymmetric`: the backend binds the duck-typed `s.area()` in `total_area` to
# `Circle.area` by suffix match, so `callers Square.area` used to answer complete and safe without it.
# `ts-safe-for-destructive-wrong-half`: in TypeScript `Square implements Shape`, and a call written
# against `Shape` reaches `Square.area` with no edge to it at all.

def _method_row(caller: str, caller_file: str, callee: str, callee_file: str, *, strategy: str,
                written: str, conf: str | None = None, caller_labels: str = "Function") -> dict:
    row = _row(caller, caller_file, callee, callee_file, strategy=strategy, conf=conf,
               labels=caller_labels)
    row["labels(b)"] = "Method"
    row["callee"] = written
    return row


def _definer(cls: str, file: str, method: str, *, bases: str = "[]") -> dict:
    return {"c.qualified_name": f"{PROJECT}.{cls}", "c.name": cls.rsplit(".", 1)[-1],
            "c.file_path": file, "c.base_classes": bases,
            "m.qualified_name": f"{PROJECT}.{cls}.{method}", "m.file_path": file}


def _interface(qn: str, file: str) -> dict:
    return {"i.name": qn.rsplit(".", 1)[-1], "i.qualified_name": f"{PROJECT}.{qn}",
            "i.file_path": file, "labels(i)": '["Interface"]'}


def _ask_dispatch(monkeypatch, root: str, target: str, edges: list[dict], definers: list[dict],
                  interfaces: list[dict] | None = None, *, fail_edge_call: int = 0) -> dict:
    """Like `_ask`, with a router that answers the way the backend does for a method: every edge row
    of the bare name for an edge query, the classes defining the name, and the interfaces."""
    monkeypatch.setattr(
        "codeintel.engines.graph.provider.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    listing = {"projects": [{"name": PROJECT, "root_path": root}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))
    edge_calls = [0]

    def route(cypher: str, project: str, timeout_ms: int) -> list[dict]:
        if "count(*)" in cypher:
            return []
        if "-[c:" in cypher:
            edge_calls[0] += 1
            if fail_edge_call and edge_calls[0] >= fail_edge_call:
                raise RuntimeError("backend went away")
            return list(edges)
        if ":Interface" in cypher:
            return list(interfaces or [])
        if "DEFINES_METHOD" in cypher and "m.name=" in cypher:
            return list(definers)
        return []

    monkeypatch.setattr(p, "_query_rows", route)
    return p.build_result("callers", target, [], 30000, root)


PY_AREA_EDGES = [
    _method_row("pkg.shapes.Square.describe", "pkg/shapes.py", "pkg.shapes.Square.area",
                "pkg/shapes.py", strategy="lsp_method", written="self.area", conf="0.90",
                caller_labels="Method"),
    _method_row("pkg.shapes.only_square", "pkg/shapes.py", "pkg.shapes.Square.area",
                "pkg/shapes.py", strategy="lsp_method", written="sq.area", conf="0.90"),
    _method_row("pkg.shapes.total_area", "pkg/shapes.py", "pkg.shapes.Circle.area",
                "pkg/shapes.py", strategy="suffix_match", written="s.area", conf="0.55"),
]
PY_AREA_DEFINERS = [_definer("pkg.shapes.Circle", "pkg/shapes.py", "area"),
                    _definer("pkg.shapes.Square", "pkg/shapes.py", "area")]

TS_AREA_EDGES = [
    _method_row("src.shapes.Square.describe", "src/shapes.ts", "src.shapes.Square.area",
                "src/shapes.ts", strategy="lsp_ts_method", written="this.area", conf="0.90",
                caller_labels="Method"),
    _method_row("src.shapes.onlySquare", "src/shapes.ts", "src.shapes.Square.area",
                "src/shapes.ts", strategy="lsp_ts_method", written="sq.area", conf="0.90"),
]
TS_AREA_DEFINERS = [_definer("src.shapes.Circle", "src/shapes.ts", "area", bases='["Shape"]'),
                    _definer("src.shapes.Square", "src/shapes.ts", "area", bases='["Shape"]')]
TS_INTERFACES = [_interface("src.shapes.Shape", "src/shapes.ts")]


def test_callers_of_a_method_lists_the_call_a_sibling_was_bound_to_by_name(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_AREA_EDGES, PY_AREA_DEFINERS)

    total = _row_named(env, "total_area")
    assert total["evidence"] == "name-matched" and total["verified"] is False, total
    assert "Circle.area" in total["why"], total["why"]
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "Circle.area" in gaps[0]["detail"] and "total_area" in gaps[0]["detail"], gaps
    assert gaps[0]["section"] != "callees", gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False
    assert "total_area" in env["result"] and "dispatch-sibling-unbound" in env["result"]
    # The two callers the backend did bind to `Square.area` are still there, as they were.
    assert _row_named(env, "only_square")["verified"] is True


def test_callers_of_the_method_the_call_was_bound_to_is_unchanged(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Circle.area", PY_AREA_EDGES, PY_AREA_DEFINERS)

    assert [r["name"] for r in env["rows"]] == ["total_area"], env["rows"]
    assert env["confidence"] == "partial"
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_sibling_lookup_that_finds_only_the_target_adds_nothing(monkeypatch):
    edges = [r for r in PY_AREA_EDGES if r["a.name"] != "total_area"]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", edges, PY_AREA_DEFINERS[1:])
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["confidence"] == "complete", env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_sibling_with_only_bound_callers_adds_nothing(monkeypatch):
    """`Circle.area`'s callers here are all language-server bindings: no call was guessed by name."""
    edges = [dict(r, strategy="lsp_method") if r["a.name"] == "total_area" else r
             for r in PY_AREA_EDGES]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", edges, PY_AREA_DEFINERS)
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_sibling_whose_callers_could_not_be_fetched_says_could_not_check(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_AREA_EDGES, PY_AREA_DEFINERS,
                        fail_edge_call=2)
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "could not check" in gaps[0]["detail"] and "Circle.area" in gaps[0]["detail"], gaps
    assert env["evidence"]["safe_for_destructive"] is False


def test_ts_callers_of_a_method_an_interface_declares_has_the_gap(monkeypatch):
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.area", TS_AREA_EDGES, TS_AREA_DEFINERS,
                        TS_INTERFACES)

    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "`Shape`" in gaps[0]["detail"] and "area" in gaps[0]["detail"], gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


def test_ts_an_interface_that_does_not_declare_the_method_adds_nothing(monkeypatch):
    edges = [dict(r, **{"b.name": "describe",
                        "b.qualified_name": f"{PROJECT}.src.shapes.Square.describe"})
             for r in TS_AREA_EDGES if r["a.name"] == "onlySquare"]
    definers = [_definer("src.shapes.Square", "src/shapes.ts", "describe", bases='["Shape"]')]
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.describe", edges, definers, TS_INTERFACES)
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")


def test_ts_an_interface_file_the_budget_could_not_read_says_could_not_check(monkeypatch):
    monkeypatch.setattr(qualifier_scan, "_MAX_FILES", 0)
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.area", TS_AREA_EDGES, TS_AREA_DEFINERS,
                        TS_INTERFACES)
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "could not check" in " ".join(g["detail"] for g in gaps), gaps
    assert env["evidence"]["safe_for_destructive"] is False


# ---- the pure functions ---------------------------------------------------------------------

def test_alias_imports_finds_every_alias_form():
    from codeintel.engines.graph.blindspots import alias_imports

    assert alias_imports("from pkg import run_process, helper as h\n", "helper") == ["h"]
    assert alias_imports("from pkg.sub.impl import (\n    helper as h2,\n)\n", "helper") == ["h2"]
    assert alias_imports("import pkg.helper as hh\n", "helper") == ["hh"]
    assert alias_imports('import { runProcess, helper as h } from "./lib";\n', "helper") == ["h"]
    assert alias_imports('export { process as runProcess, helper } from "./impl";\n',
                         "process") == ["runProcess"]
    assert alias_imports('export { helper as X };\n', "helper") == ["X"]
    assert alias_imports("from pkg import helper\n", "helper") == []
    assert alias_imports("from pkg import other as helper\n", "helper") == []


def test_alias_imports_reads_python_that_does_not_parse_by_regex():
    from codeintel.engines.graph.blindspots import alias_imports

    assert alias_imports("from pkg import helper as h\ndef broken(:\n", "helper") == ["h"]


def test_nested_shadow_in_python_uses_the_ast():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    with open(os.path.join(PY_ROOT, "pkg", "shapes.py"), encoding="utf-8") as fh:
        text = fh.read()
    outer = function_span(text, "outer", "pkg/shapes.py")
    module_caller = function_span(text, "calls_module_inner", "pkg/shapes.py")
    assert outer is not None and module_caller is not None
    assert nested_shadow(text, outer, "inner") is True
    assert nested_shadow(text, module_caller, "inner") is False


def test_nested_shadow_falls_back_to_the_regex_when_python_does_not_parse():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    text = ("def outer():\n    def inner():\n        return 1\n    return inner()\n\n"
            "def broken(:\n    pass\n")
    span = function_span(text, "outer", "x.py")
    assert span is not None
    assert nested_shadow(text, span, "inner") is True


def test_nested_shadow_in_typescript_uses_the_regex():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    with open(os.path.join(TS_ROOT, "src", "shapes.ts"), encoding="utf-8") as fh:
        text = fh.read()
    outer = function_span(text, "outer", "src/shapes.ts")
    total = function_span(text, "totalArea", "src/shapes.ts")
    assert outer is not None and total is not None
    assert nested_shadow(text, outer, "inner") is True
    assert nested_shadow(text, total, "inner") is False


def test_a_call_before_the_nested_definition_is_not_shadowed():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    text = "def outer():\n    x = inner()\n    def inner():\n        return 1\n    return x\n"
    span = function_span(text, "outer", "x.py")
    assert span is not None
    assert nested_shadow(text, span, "inner") is False


def test_interface_declares_reads_the_interface_body():
    from codeintel.engines.graph.blindspots import interface_declares

    with open(os.path.join(TS_ROOT, "src", "shapes.ts"), encoding="utf-8") as fh:
        text = fh.read()
    assert interface_declares(text, "Shape", "area") is True
    assert interface_declares(text, "Shape", "describe") is False
    assert interface_declares(text, "Missing", "area") is False
    assert interface_declares("interface A<T> extends B {\n  readonly area?: () => T;\n}\n",
                              "A", "area") is True


# ---- live -----------------------------------------------------------------------------------

@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None,
    reason="codebase-memory-mcp backend not installed",
)
def test_live_index_and_query_on_the_python_fixture(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(PY_ROOT, repo, ignore=shutil.ignore_patterns("__pycache__"))
    # The semantic half of `index` needs a model download that a sandbox cannot make; offline,
    # it fails at once instead of retrying, and the graph half is what this test reads.
    env = dict(os.environ, HF_HUB_OFFLINE="1")
    cli = [sys.executable, "-m", "codeintel"]
    subprocess.run([*cli, "index", "-q", str(repo)], cwd=repo, env=env, capture_output=True,
                   timeout=300, check=False)

    for target in ("helper", "inner"):
        done = subprocess.run(
            [*cli, "query", "--op", "callers", "--target", target, "--json", "--engine", "graph"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=120, check=False)
        answer = json.loads(done.stdout)
        assert answer.get("result") is not None, (
            f"codebase-memory-mcp is on PATH but the graph backend did not answer "
            f"`callers {target}`: {answer.get('reason')}; stderr: {done.stderr[-2000:]}")
        assert answer["evidence"]["safe_for_destructive"] is False, answer
        kinds = {g["kind"] for g in answer.get("gaps") or []}
        expected = "aliased-import-unbound" if target == "helper" else "target-ambiguous"
        assert expected in kinds, answer
