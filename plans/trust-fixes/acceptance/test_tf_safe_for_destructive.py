"""Story: an agent deciding whether a change is safe.

`safe_for_destructive: true` only when the graph really saw every caller; when it cannot see
something (an aliased import, a sibling/interface dispatch, a nested shadow), a named gap says so.

Driven through `GraphProvider.build_result`, with the backend faked at its transport (`_run`).
"""
from __future__ import annotations

from _fakegraph import Repo, gap_kinds, safe_for_destructive


def _row(env: dict, name: str) -> dict | None:
    for r in env.get("rows") or []:
        if r.get("name") == name or str(r.get("qualified_name") or "").endswith("." + name):
            return r
    return None


# --------------------------------------------------------------------------- aliased imports

def _py_helper_repo(tmp_path, monkeypatch, alias_line: str, alias_file: str = "b.py") -> Repo:
    repo = Repo(tmp_path, monkeypatch)
    repo.write("core.py", "def helper():\n    return 1\n")
    repo.write("a.py", "from core import helper\n\n\ndef use_a():\n    return helper()\n")
    repo.write(alias_file, alias_line)
    repo.commit()
    g = repo.graph
    helper = g.node("core.helper", "core.py")
    use_a = g.node("a.use_a", "a.py")
    g.node(alias_file[:-3].replace("/", ".") + ".use_b", alias_file)
    g.edge(use_a, helper, strategy="import_map", confidence="0.95")
    repo.fresh_index()
    return repo


def _assert_alias_gap(env: dict) -> None:
    assert env.get("confidence") == "partial", env.get("confidence")
    assert safe_for_destructive(env) is False
    assert "aliased-import-unbound" in gap_kinds(env), gap_kinds(env)


# Spec: `callers helper` where `helper` is imported as `h` somewhere (`helper as h`, ...) is
# `confidence: partial`, `safe_for_destructive: false`, and has an `aliased-import-unbound` gap.
def test_callers_of_a_python_symbol_imported_as_an_alias_is_partial_with_aliased_import_gap(
        tmp_path, monkeypatch):
    repo = _py_helper_repo(
        tmp_path, monkeypatch,
        "from core import helper as h\n\n\ndef use_b():\n    return h()\n")
    _assert_alias_gap(repo.ask("callers", "helper"))


# Spec: `callers helper` where `helper` is imported as `h` somewhere (`{ helper as h }`) is
# `confidence: partial`, `safe_for_destructive: false`, and has an `aliased-import-unbound` gap.
def test_callers_of_a_ts_symbol_imported_as_an_alias_is_partial_with_aliased_import_gap(
        tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("src/lib/impl.ts",
               "export function helper(x: number) { return x; }\n"
               "export function process(x: number) { return helper(x); }\n")
    repo.write("src/main.ts",
               'import { helper as h } from "./lib/impl";\n'
               "export function entry() { return h(2); }\n")
    repo.commit()
    g = repo.graph
    helper = g.node("src.lib.impl.helper", "src/lib/impl.ts")
    process = g.node("src.lib.impl.process", "src/lib/impl.ts")
    g.node("src.main.entry", "src/main.ts")
    g.edge(process, helper, strategy="lsp_ts_direct", confidence="0.95")
    repo.fresh_index()
    _assert_alias_gap(repo.ask("callers", "helper"))


# Spec: `callers helper` where `helper` is re-exported under another name somewhere
# (`export { helper as X }`) is `confidence: partial`, `safe_for_destructive: false`, and has an
# `aliased-import-unbound` gap.
def test_callers_of_a_ts_symbol_reexported_under_an_alias_is_partial_with_aliased_import_gap(
        tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("src/lib/impl.ts",
               "export function helper(x: number) { return x; }\n"
               "export function process(x: number) { return helper(x); }\n")
    repo.write("src/lib/index.ts", 'export { helper as runHelper } from "./impl";\n')
    repo.write("src/main.ts",
               'import { runHelper } from "./lib";\n'
               "export function entry() { return runHelper(2); }\n")
    repo.commit()
    g = repo.graph
    helper = g.node("src.lib.impl.helper", "src/lib/impl.ts")
    process = g.node("src.lib.impl.process", "src/lib/impl.ts")
    g.node("src.main.entry", "src/main.ts")
    g.edge(process, helper, strategy="lsp_ts_direct", confidence="0.95")
    repo.fresh_index()
    _assert_alias_gap(repo.ask("callers", "helper"))


# Spec: `callees entry`, where `entry`'s body calls an import alias that has no callee row, is
# partial and has an `aliased-import-unbound` gap.
def test_callees_of_a_function_calling_an_unbound_import_alias_is_partial_with_aliased_import_gap(
        tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/impl.py",
               "def run_process(x):\n    return x\n\n\ndef helper(x):\n    return x\n")
    repo.write("main.py",
               "from pkg.impl import run_process, helper as h\n\n\n"
               "def entry():\n    run_process(1)\n    h(2)\n")
    repo.commit()
    g = repo.graph
    entry = g.node("main.entry", "main.py")
    run_process = g.node("pkg.impl.run_process", "pkg/impl.py")
    g.node("pkg.impl.helper", "pkg/impl.py")
    g.edge(entry, run_process, strategy="import_map", confidence="0.95")
    repo.fresh_index()
    env = repo.ask("callees", "entry")
    assert env.get("confidence") == "partial", env.get("confidence")
    assert safe_for_destructive(env) is False
    assert "aliased-import-unbound" in gap_kinds(env), gap_kinds(env)


# --------------------------------------------------------------------------- sibling dispatch

def _shapes_repo(tmp_path, monkeypatch) -> Repo:
    repo = Repo(tmp_path, monkeypatch)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/shapes.py",
               "class Circle:\n"
               "    def area(self):\n        return 3.14\n\n\n"
               "class Square:\n"
               "    def area(self):\n        return 1.0\n\n"
               "    def describe(self):\n        return self.area()\n\n\n"
               "def only_square():\n    return Square().area()\n\n\n"
               "def total_area(shapes):\n    return sum(s.area() for s in shapes)\n")
    repo.commit()
    return repo


# Spec: `callers Square.area`, where a same-named sibling method (`Circle.area`) has a
# name-matched receiver call (`total_area`'s `s.area()`), lists that call as a name-matched row, is
# partial, and has a `dispatch-sibling-unbound` gap.
def test_callers_of_a_method_whose_sibling_has_a_name_matched_call_lists_it_and_has_dispatch_gap(
        tmp_path, monkeypatch):
    repo = _shapes_repo(tmp_path, monkeypatch)
    g = repo.graph
    circle_area = g.node("pkg.shapes.Circle.area", "pkg/shapes.py", "Method")
    square_area = g.node("pkg.shapes.Square.area", "pkg/shapes.py", "Method")
    describe = g.node("pkg.shapes.Square.describe", "pkg/shapes.py", "Method")
    only_square = g.node("pkg.shapes.only_square", "pkg/shapes.py")
    total_area = g.node("pkg.shapes.total_area", "pkg/shapes.py")
    g.edge(describe, square_area, strategy="lsp_method", confidence="0.95")
    g.edge(only_square, square_area, strategy="lsp_method", confidence="0.95")
    g.edge(total_area, circle_area, strategy="suffix_match", confidence="0.55")
    repo.fresh_index()

    env = repo.ask("callers", "Square.area")
    row = _row(env, "total_area")
    assert row is not None, f"total_area's s.area() is not listed: {env.get('result')}"
    assert row.get("evidence") == "name-matched"
    assert row.get("verified") is False
    assert env.get("confidence") == "partial"
    assert safe_for_destructive(env) is False
    assert "dispatch-sibling-unbound" in gap_kinds(env), gap_kinds(env)


# Spec: `callers Square.area`, where an interface/Protocol member `area` has a name-matched
# receiver call (`total_area`'s `s.area()`), lists that call as a name-matched row, is partial, and
# has a `dispatch-sibling-unbound` gap.
def test_callers_of_a_method_whose_protocol_member_has_a_name_matched_call_has_dispatch_gap(
        tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("src/shapes.ts",
               "export interface Shape { area(): number }\n"
               "export class Square { area(): number { return 1; } "
               "describe() { return this.area(); } }\n"
               "export function onlySquare() { return new Square().area(); }\n"
               "export function totalArea(shapes: Shape[]) "
               "{ let t = 0; for (const s of shapes) { t += s.area(); } return t; }\n")
    repo.commit()
    g = repo.graph
    shape_area = g.node("src.shapes.Shape.area", "src/shapes.ts", "Method")
    square_area = g.node("src.shapes.Square.area", "src/shapes.ts", "Method")
    describe = g.node("src.shapes.Square.describe", "src/shapes.ts", "Method")
    only_square = g.node("src.shapes.onlySquare", "src/shapes.ts")
    total_area = g.node("src.shapes.totalArea", "src/shapes.ts")
    g.edge(describe, square_area, strategy="lsp_ts_method", confidence="0.95")
    g.edge(only_square, square_area, strategy="lsp_ts_method", confidence="0.95")
    g.edge(total_area, shape_area, strategy="suffix_match", confidence="0.55")
    repo.fresh_index()

    env = repo.ask("callers", "Square.area")
    row = _row(env, "totalArea")
    assert row is not None, f"totalArea's s.area() is not listed: {env.get('result')}"
    assert row.get("evidence") == "name-matched"
    assert env.get("confidence") == "partial"
    assert safe_for_destructive(env) is False
    assert "dispatch-sibling-unbound" in gap_kinds(env), gap_kinds(env)


# --------------------------------------------------------------------------- nested shadowing

_NESTED_PY = ("def outer():\n"
              "    def inner():\n"
              "        return 1\n"
              "    return inner()\n\n\n"
              "def inner():\n"
              "    return 2\n\n\n"
              "def calls_module_inner():\n"
              "    return inner()\n")


def _nested_repo(tmp_path, monkeypatch, source: str) -> Repo:
    repo = Repo(tmp_path, monkeypatch)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/shapes.py", source)
    repo.commit()
    g = repo.graph
    # The backend merges the nested def into the module-level one: ONE node named `inner`.
    inner = g.node("pkg.shapes.inner", "pkg/shapes.py")
    outer = g.node("pkg.shapes.outer", "pkg/shapes.py")
    cmi = g.node("pkg.shapes.calls_module_inner", "pkg/shapes.py")
    g.edge(outer, inner, strategy="lsp_direct", confidence="0.95")
    g.edge(cmi, inner, strategy="lsp_direct", confidence="0.95")
    repo.fresh_index()
    return repo


def _assert_nested_shadow_handled(env: dict) -> None:
    outer = _row(env, "outer")
    assert outer is None or (outer.get("verified") is False and outer.get("evidence") != "resolved"), \
        f"outer is listed as a resolved caller of the module-level inner: {outer}"
    assert "target-ambiguous" in gap_kinds(env), gap_kinds(env)
    assert "the only symbol named `inner`" not in str(env.get("result") or "")
    assert safe_for_destructive(env) is False


# Spec: `callers inner`, where another function defines a nested `inner` before its call, does not
# list that function as a resolved caller. The answer has a `target-ambiguous` gap and never says
# "the only symbol named `inner`".
def test_callers_of_a_name_shadowed_by_a_nested_def_does_not_list_the_shadowing_function_as_resolved(
        tmp_path, monkeypatch):
    repo = _nested_repo(tmp_path, monkeypatch, _NESTED_PY)
    _assert_nested_shadow_handled(repo.ask("callers", "inner"))


# Edge case: Nested def with the target's name in a `.py` file that does not parse -> fall back to
# the regex check.
def test_nested_def_shadow_is_still_detected_in_a_python_file_that_does_not_parse(
        tmp_path, monkeypatch):
    broken = _NESTED_PY + "\n\ndef broken(:\n    pass\n"
    repo = _nested_repo(tmp_path, monkeypatch, broken)
    _assert_nested_shadow_handled(repo.ask("callers", "inner"))


# Spec: `GraphProvider.build_result("callers", "inner", [], 0, root)`, where the backend's only
# caller row carries the strategy `lsp_ts_local` (the TS `outer`/nested `inner` repro): that row has
# `verified: false`, the envelope's `evidence` counts it under `possible` and not `verified`, and
# `safe_for_destructive` is false.
def test_an_lsp_ts_local_caller_row_is_possible_not_verified_and_not_safe(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.write("src/shapes.ts",
               "function outer() {\n"
               "  function inner() { return 1; }\n"
               "  return inner();\n"
               "}\n"
               "export function inner() { return 2; }\n")
    repo.commit()
    g = repo.graph
    inner = g.node("src.shapes.inner", "src/shapes.ts")
    outer = g.node("src.shapes.outer", "src/shapes.ts")
    g.edge(outer, inner, strategy="lsp_ts_local", confidence="0.95")
    repo.fresh_index()

    env = repo.provider().build_result("callers", "inner", [], 0, repo.root)
    row = _row(env, "outer")
    assert row is not None, env.get("result")
    assert row.get("verified") is False
    evidence = env.get("evidence") or {}
    assert evidence.get("verified") == 0, evidence
    assert evidence.get("possible") == 1, evidence
    assert safe_for_destructive(env) is False
