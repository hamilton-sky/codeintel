"""Tests for the graph viewer builder (grapher.py) — never-raise, real-shape fixtures, HTML injection.

Follows the repo's real-boundary style: the graph methods are patched with the SHAPES the real
backend returns (search_graph result dicts, query_graph column→value row dicts), not fabricated ones.
"""
from __future__ import annotations

from codeintel import grapher
from codeintel.providers.graph import GraphProvider


def _wire(monkeypatch, *, available=True, search=None, rows=None, project="proj"):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which",
                        lambda x: "/fake/cm" if available else None)
    monkeypatch.setattr(GraphProvider, "_resolve_project", lambda self, r: project if available else None)
    monkeypatch.setattr(GraphProvider, "_search_symbols", lambda self, extra, proj, t: search or [])
    monkeypatch.setattr(GraphProvider, "_query_rows", lambda self, cy, proj, t: rows or [])


SEARCH = [{"qualified_name": "pkg.a.foo", "name": "foo", "file_path": "src/pkg/a.py",
           "complexity": 5, "cognitive": 8, "in_degree": 2, "out_degree": 1, "lines": 20}]
ROWS = [
    {"a.qualified_name": "pkg.a.foo", "a.name": "foo", "a.file_path": "src/pkg/a.py",
     "b.qualified_name": "pkg.b.bar", "b.name": "bar", "b.file_path": "src/pkg/b.py", "type(c)": "CALLS"},
    # a builtin call — synthetic '<python-builtins>' file → must be filtered out
    {"a.qualified_name": "pkg.b.bar", "a.name": "bar", "a.file_path": "src/pkg/b.py",
     "b.qualified_name": "builtins.len", "b.name": "len", "b.file_path": "<python-builtins>", "type(c)": "CALLS"},
]


def test_build_payload_shapes_nodes_edges_and_filters_builtins(monkeypatch):
    _wire(monkeypatch, search=SEARCH, rows=ROWS)
    p = grapher.build_graph_payload("/repo")
    labels = {n["label"] for n in p["nodes"]}
    assert labels == {"foo", "bar"}                 # builtin 'len' edge dropped (synthetic file_path)
    assert len(p["edges"]) == 1 and p["edges"][0]["type"] == "CALLS"
    foo = next(n for n in p["nodes"] if n["label"] == "foo")
    assert foo["complexity"] == 5 and foo["file"] == "src/pkg/a.py"   # metrics enriched from search_graph


def test_build_payload_engine_unavailable_is_safe(monkeypatch):
    _wire(monkeypatch, available=False)
    p = grapher.build_graph_payload("/repo")
    assert p["nodes"] == [] and p["edges"] == [] and p["reason"] == "engine-unavailable"


def test_build_payload_not_indexed_is_safe(monkeypatch):
    _wire(monkeypatch, project=None)  # available but unresolved
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: "/fake/cm")
    monkeypatch.setattr(GraphProvider, "_resolve_project", lambda self, r: None)
    p = grapher.build_graph_payload("/repo")
    assert p["nodes"] == [] and p["reason"] == "project-not-indexed"


def test_build_payload_never_raises(monkeypatch):
    _wire(monkeypatch)
    def _boom(*a, **k):
        raise RuntimeError("backend exploded")
    monkeypatch.setattr(GraphProvider, "_query_rows", _boom)
    p = grapher.build_graph_payload("/repo")
    assert p["nodes"] == [] and p.get("reason") == "error"   # degraded, not raised


def test_build_payload_respects_edge_limit(monkeypatch):
    many = [{"a.qualified_name": f"m.f{i}", "a.name": f"f{i}", "a.file_path": "src/m.py",
             "b.qualified_name": f"m.g{i}", "b.name": f"g{i}", "b.file_path": "src/m.py", "type(c)": "CALLS"}
            for i in range(50)]
    _wire(monkeypatch, rows=many)
    p = grapher.build_graph_payload("/repo", limit=10)
    assert len(p["edges"]) == 10


def test_render_html_injects_and_is_self_contained():
    payload = {"project": "x", "engine": "graph", "op": "callgraph",
               "nodes": [{"id": "a", "label": "alpha", "file": "a.py"}], "edges": []}
    html = grapher.render_html(payload)
    assert "__DATA__" not in html                    # placeholder replaced
    assert "alpha" in html                            # data embedded
    assert "<title>" in html
    low = html.lower()
    assert 'src="http' not in low and 'href="http' not in low and "cdn" not in low   # no external refs


def test_render_html_falls_back_when_template_missing(monkeypatch):
    monkeypatch.setattr(grapher, "_template_path", lambda: "/nonexistent/does-not-exist.html")
    html = grapher.render_html({"project": "x", "nodes": [], "edges": []})
    assert "codeintel graph" in html and "__DATA__" not in html   # minimal page, never raised


def test_render_html_escapes_script_close():
    # a symbol containing '</script>' must not break out of the inline data block
    payload = {"project": "x", "nodes": [{"id": "a", "label": "a</script><b>", "file": "a.py"}], "edges": []}
    html = grapher.render_html(payload)
    assert "</script><b>" not in html   # the sequence is escaped in the embedded JSON
