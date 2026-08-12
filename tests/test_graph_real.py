"""GraphProvider tests against the REAL codebase-memory-mcp contract.

Two layers:
  1. Captured-real-response fixtures — the exact JSON shapes `codebase-memory-mcp 0.9.0`
     returns (``{"columns","rows"}`` value-arrays, ``{"results":[...]}``, ``trace_path``…),
     fed through the parsing/formatting code. This is what the old suite lacked: it mocked
     ``_run`` with a *fabricated* list-of-dicts shape that the real backend never emits, so
     every ``_op_*`` silently discarded real rows and the bug passed the tests.
  2. A live subprocess test that actually shells out to the backend (skipped when it or the
     indexed project is absent), so the real argv/method/response contract is exercised end-to-end.
"""
from __future__ import annotations

import os
import shutil

import pytest

from codeintel.providers.graph import GraphProvider

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# --------------------------------------------------------------------------- #
# Captured real responses (verbatim shapes from codebase-memory-mcp 0.9.0)
# --------------------------------------------------------------------------- #

# list_projects: note the PARENT project ".../project" is listed BEFORE the exact
# ".../project/codeintel" — the old first-prefix-match break bound queries to the parent.
CAP_LIST_PROJECTS = {
    "projects": [
        {"name": "parent-project", "root_path": "/Users/x/Documents/project"},
        {"name": "codeintel", "root_path": "/Users/x/Documents/project/codeintel"},
        {"name": "codeintel-dup", "root_path": "/Users/x/Documents/project/codeintel"},
    ]
}

# query_graph callers of safe_null_result — value-arrays aligned to columns, NOT list-of-dicts.
CAP_CALLERS = {
    "columns": ["a.name", "a.qualified_name", "a.file_path", "type(c)"],
    "rows": [
        ["src/codeintel/gateway.py", "codeintel.src.codeintel.gateway", "src/codeintel/gateway.py", "USAGE"],
        ["src/codeintel/providers/graph.py", "codeintel.src.codeintel.providers.graph", "src/codeintel/providers/graph.py", "USAGE"],
        ["src/codeintel/server.py", "codeintel.src.codeintel.server", "src/codeintel/server.py", "USAGE"],
    ],
    "total": 3,
}

CAP_CALLEES = {
    "columns": ["b.name", "b.qualified_name", "b.file_path", "type(c)"],
    "rows": [
        ["str", "builtins.str", "<python-builtins>", "CALLS"],
        ["Result", "codeintel.src.codeintel.provider.Result", "src/codeintel/provider.py", "USAGE"],
    ],
    "total": 2,
}

CAP_EMPTY = {"columns": ["a.name"], "rows": [], "total": 0,
             "hint": "Query returned no results."}

CAP_SEARCH_CODE = {
    "results": [
        {"node": "query", "qualified_name": "codeintel.src.codeintel.gateway.Gateway.query",
         "label": "Method", "file": "src/codeintel/gateway.py", "start_line": 134,
         "end_line": 228, "match_lines": [172, 228, 164, 168]},
        {"node": "build_result", "qualified_name": "codeintel.src.codeintel.providers.semantic.SemanticProvider.build_result",
         "label": "Method", "file": "src/codeintel/providers/semantic.py", "start_line": 24,
         "end_line": 84, "match_lines": [33, 35]},
    ]
}

CAP_ARCH = {
    "project": "codeintel", "total_nodes": 1475, "total_edges": 2809,
    "node_labels": [{"label": "Section", "count": 787}, {"label": "Function", "count": 132}],
    "edge_types": [{"type": "DEFINES", "count": 1637}, {"type": "CALLS", "count": 387}],
    "languages": [{"language": "python", "files": 133}],
}

CAP_TRACE = {
    "function": "_cypher_literal", "direction": "both", "mode": "calls",
    "callees": [{"name": "str", "qualified_name": "builtins.str", "hop": 1}],
    "callers": [
        {"name": "_op_callers", "qualified_name": "codeintel.src.codeintel.providers.graph.GraphProvider._op_callers", "hop": 1},
        {"name": "build_result", "qualified_name": "codeintel.src.codeintel.providers.graph.GraphProvider.build_result", "hop": 3},
    ],
}

CAP_TRACE_AMBIGUOUS = {
    "status": "ambiguous",
    "message": '2 matches for "build_result".',
    "suggestions": [
        {"qualified_name": "codeintel.src.codeintel.providers.graph.GraphProvider.build_result",
         "name": "build_result", "label": "Method", "file_path": "src/codeintel/providers/graph.py"},
        {"qualified_name": "codeintel.src.codeintel.providers.none.NoneProvider.build_result",
         "name": "build_result", "label": "Method", "file_path": "src/codeintel/providers/none.py"},
    ],
}


def _provider(monkeypatch, responses):
    """A GraphProvider whose _run replays captured responses keyed by method."""
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp"
    )
    p = GraphProvider()

    def _fake_run(method, payload, timeout_ms):
        val = responses.get(method)
        return val(payload) if callable(val) else val

    monkeypatch.setattr(p, "_run", _fake_run)
    return p


# --------------------------------------------------------------------------- #
# 1 — resolution: exact match beats a shorter parent-prefix match
# --------------------------------------------------------------------------- #

def test_resolve_prefers_exact_over_parent_prefix(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS})
    name = p._resolve_project("/Users/x/Documents/project/codeintel")
    assert name == "codeintel"  # NOT "parent-project"


def test_resolve_prefix_when_inside_subdir(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS})
    # A path inside codeintel (no exact entry) resolves to the longest prefix = codeintel.
    name = p._resolve_project("/Users/x/Documents/project/codeintel/src/codeintel")
    assert name == "codeintel"


# --------------------------------------------------------------------------- #
# 2 — callers / callees / impact parse value-array rows into real content
# --------------------------------------------------------------------------- #

def test_callers_parses_columns_rows(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "query_graph": CAP_CALLERS})
    r = p.build_result("callers", "safe_null_result", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["ok"] is True
    assert r["engine"] == "graph"
    assert r["result"] is not None
    assert "Callers of safe_null_result (3)" in r["result"]
    assert "codeintel.src.codeintel.gateway" in r["result"]
    assert "[USAGE]" in r["result"]


def test_callees_parses_columns_rows(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "query_graph": CAP_CALLEES})
    r = p.build_result("callees", "safe_null_result", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "builtins.str" in r["result"]
    assert "[CALLS]" in r["result"]


def test_impact_combines_callers_and_callees(monkeypatch):
    # query_graph is called for callers then callees; return callers-shape then callees-shape.
    seq = iter([CAP_CALLERS, CAP_CALLEES])
    p = _provider(
        monkeypatch,
        {"list_projects": CAP_LIST_PROJECTS, "query_graph": lambda payload: next(seq)},
    )
    r = p.build_result("impact", "safe_null_result", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "## Callers of" in r["result"] and "## Callees of" in r["result"]
    assert "gateway" in r["result"] and "builtins.str" in r["result"]


def test_context_aliases_to_impact(monkeypatch):
    # `context` is a fan-out op; the graph engine answers it with its impact (callers+callees) view.
    seq = iter([CAP_CALLERS, CAP_CALLEES])
    p = _provider(
        monkeypatch,
        {"list_projects": CAP_LIST_PROJECTS, "query_graph": lambda payload: next(seq)},
    )
    r = p.build_result("context", "safe_null_result", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "## Callers of" in r["result"] and "## Callees of" in r["result"]


def test_empty_rows_yield_safe_null(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "query_graph": CAP_EMPTY})
    r = p.build_result("callers", "does_not_exist", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["ok"] is True
    assert r["result"] is None  # no rows → safe-null, not a crash


# --------------------------------------------------------------------------- #
# 3 — pattern / overview / chain parse their real shapes
# --------------------------------------------------------------------------- #

def test_pattern_parses_search_code_results(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "search_code": CAP_SEARCH_CODE})
    r = p.build_result("pattern", "safe_null_result", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "Gateway.query" in r["result"] or "query [Method]" in r["result"]
    assert "src/codeintel/gateway.py:134" in r["result"]


def test_overview_parses_get_architecture(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "get_architecture": CAP_ARCH})
    r = p.build_result("overview", "", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "1475 nodes, 2809 edges" in r["result"]
    assert "Function: 132" in r["result"]
    assert "CALLS: 387" in r["result"]


def test_chain_parses_trace_path(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "trace_path": CAP_TRACE})
    r = p.build_result("chain", "_cypher_literal", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "Callees (downstream)" in r["result"]
    assert "builtins.str [hop 1]" in r["result"]
    assert "GraphProvider._op_callers [hop 1]" in r["result"]


def test_chain_handles_ambiguous_suggestions(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "trace_path": CAP_TRACE_AMBIGUOUS})
    r = p.build_result("chain", "build_result", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["result"] is not None
    assert "Ambiguous" in r["result"]
    assert "GraphProvider.build_result" in r["result"]


# --------------------------------------------------------------------------- #
# 4 — LIVE: actually shell out to the real backend (no mocks at all)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 3b — fault injection on the rewritten parsing path (never-raise)
# --------------------------------------------------------------------------- #

def test_malformed_query_response_is_safe_null(monkeypatch):
    # A dict with neither columns nor rows, and junk rows — must degrade, not crash.
    bad = {"garbage": 1, "rows": "not-a-list", "columns": None}
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "query_graph": bad})
    r = p.build_result("callers", "x", [], 0, "/Users/x/Documents/project/codeintel")
    assert r["ok"] is True
    assert r["result"] is None


def test_run_raising_is_caught(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp"
    )
    p = GraphProvider()

    def _boom(method, payload, timeout_ms):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(p, "_run", _boom)
    r = p.build_result("impact", "x", [], 0, "/repo")
    assert r["ok"] is True  # never-raise holds even when _run throws mid-query


@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None,
    reason="codebase-memory-mcp backend not installed",
)
def test_live_callers_of_safe_null_result():
    """End-to-end against the real subprocess + the indexed codeintel project.

    This is the test the old suite structurally could not have: it hits the real argv,
    method names, and response shapes. If the project isn't indexed in this environment,
    skip rather than fail."""
    p = GraphProvider()
    assert p.available is True
    if p._resolve_project(REPO_ROOT) is None:
        pytest.skip("codeintel project not indexed in this environment")

    r = p.build_result("callers", "safe_null_result", [], 0, REPO_ROOT)
    assert r["ok"] is True
    assert r["engine"] == "graph"
    assert r["result"] is not None, "real backend returned no callers — parsing/contract regression"
    assert "gateway" in r["result"]

    impact = p.build_result("impact", "safe_null_result", [], 0, REPO_ROOT)
    assert impact["result"] is not None
    assert "## Callers of" in impact["result"]
