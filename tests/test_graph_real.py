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


# detect_changes — real shape (verbatim from `codebase-memory-mcp cli detect_changes`).
CAP_DETECT_CHANGES = {
    "changed_files": ["src/codeintel/providers/graph.py"],
    "changed_count": 1, "depth": 2,
    "impacted_symbols": [
        {"qualified_name": "codeintel.src.codeintel.providers.graph.GraphProvider._dispatch",
         "name": "_dispatch", "file_path": "src/codeintel/providers/graph.py"}],
}
CAP_DETECT_CHANGES_CLEAN = {"changed_files": [], "changed_count": 0, "impacted_symbols": [], "depth": 2}

# Real backend quirk (caught by dogfooding): changed_files come back DUPLICATED (staged + unstaged),
# and impacted_symbols interleaves bare file/module markers (label is a path) with real symbols.
CAP_DETECT_CHANGES_DUPES = {
    "changed_files": ["src/codeintel/gateway.py", "src/codeintel/gateway.py", "src/codeintel/server.py"],
    "impacted_symbols": [
        {"name": "src/codeintel/gateway.py", "qualified_name": "src/codeintel/gateway.py",
         "file_path": "src/codeintel/gateway.py"},                       # module marker → dropped
        {"name": "Gateway", "qualified_name": "codeintel.src.codeintel.gateway.Gateway",
         "file_path": "src/codeintel/gateway.py"},
        {"name": "Gateway", "qualified_name": "codeintel.src.codeintel.gateway.Gateway",
         "file_path": "src/codeintel/gateway.py"},                       # exact dup → deduped
    ],
}

# search_graph — real envelope {"total","has_more","results":[{…rich metrics…}]}. The `is_test:false`
# on a pytest function is the real trap the client-side path filter exists to catch.
CAP_HOTSPOTS = {"total": 3, "has_more": False, "results": [
    {"name": "code_status_handler", "qualified_name": "codeintel.src.codeintel.server.code_status_handler",
     "file_path": "src/codeintel/server.py", "in_degree": 5, "out_degree": 5, "complexity": 15,
     "cognitive": 34, "lines": 61},
    {"name": "len", "qualified_name": "builtins.len", "file_path": "<python-builtins>",
     "in_degree": 12, "out_degree": 0, "complexity": 0},
    {"name": "main", "qualified_name": "codeintel.src.codeintel.__main__.main",
     "file_path": "src/codeintel/__main__.py", "in_degree": 1, "out_degree": 41, "complexity": 33,
     "cognitive": 110, "lines": 220},
]}
CAP_TRACE_RISK = {
    "function": "code_status_handler", "direction": "both",
    "callees": [{"name": "str", "qualified_name": "builtins.str", "hop": 2, "risk": "HIGH"}],
    "callers": [{"name": "main", "qualified_name": "codeintel.src.codeintel.__main__.main",
                 "hop": 1, "risk": "CRITICAL"}],
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
    name = p._resolve_project("/Users/x/Documents/project/codeintel").name
    assert name == "codeintel"  # NOT "parent-project"


def test_resolve_prefix_when_inside_subdir(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS})
    # A path inside codeintel (no exact entry) resolves to the longest prefix = codeintel.
    name = p._resolve_project("/Users/x/Documents/project/codeintel/src/codeintel").name
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


# --------------------------------------------------------------------------- #
# 5 — repo-scan ops: changed / deadcode / hotspots + risk-labeled chain (0.9.0)
# --------------------------------------------------------------------------- #

ROOT = "/Users/x/Documents/project/codeintel"


def _capturing_provider(monkeypatch, method_response):
    """Provider that records the (method, payload, timeout) of the non-list_projects call."""
    seen: dict = {}
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()

    def _cap(method, payload, timeout_ms):
        if method == "list_projects":
            return CAP_LIST_PROJECTS
        seen["method"], seen["payload"], seen["timeout"] = method, payload, timeout_ms
        # An op may legitimately issue more than one backend call (hotspots asks for Function and
        # Method nodes separately). `seen` keeps the last for the existing single-call assertions;
        # `calls` keeps them all, for assertions about the set.
        seen.setdefault("calls", []).append({"method": method, "payload": payload})
        return method_response
    monkeypatch.setattr(p, "_run", _cap)
    return p, seen


def test_changed_renders_files_and_impacted_symbols(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": CAP_DETECT_CHANGES})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert r["ok"] is True and r["result"] is not None
    assert "Changes impact (1 files → 1 symbols defined in them" in r["result"]
    assert "src/codeintel/providers/graph.py" in r["result"]
    assert "GraphProvider._dispatch" in r["result"]


def test_changed_clean_tree_is_informative_not_null(monkeypatch):
    # A clean tree is a TRUE answer, not a lookup miss — a string, NOT safe-null (else the op looks broken).
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": CAP_DETECT_CHANGES_CLEAN})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert r["result"] is not None and "working tree clean" in r["result"]


def test_changed_dedupes_files_and_drops_module_markers(monkeypatch):
    # Regression for the dogfood finding: 3→2 files (gateway dedup'd); 3→1 symbols (path-marker
    # dropped, Gateway dedup'd). The header count encodes both fixes.
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": CAP_DETECT_CHANGES_DUPES})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert "Changes impact (2 files → 1 symbols defined in them" in r["result"]
    assert "Gateway" in r["result"]


def test_changed_backend_failure_is_safe_null(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": None})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert r["ok"] is True and r["result"] is None


def test_changed_malformed_dict_is_safe_null(monkeypatch):
    # A dict that isn't a detect_changes shape (e.g. a backend error object) must degrade to
    # safe-null, NOT render a false "working tree clean". (reviewer warning)
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": {"error": "boom"}})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert r["ok"] is True and r["result"] is None


def test_changed_drops_root_level_file_markers(monkeypatch):
    # A changed file at the project ROOT yields a marker whose label has no "/". The structural
    # label==file_path filter drops it (a "/"-in-label check would leak it as a fake symbol).
    resp = {"changed_files": ["main.py"], "impacted_symbols": [
        {"name": "main.py", "qualified_name": "main.py", "file_path": "main.py"},   # root marker → dropped
        {"name": "run", "qualified_name": "main.run", "file_path": "main.py"},      # real symbol → kept
    ]}
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": resp})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert "Changes impact (1 files → 1 symbols defined in them" in r["result"]  # marker dropped, real symbol kept
    assert "main.run" in r["result"]


def test_changed_keeps_symbol_with_slash_in_qualified_name(monkeypatch):
    # Go-style qualified names contain "/". The old "/"-in-label filter would wrongly drop them;
    # label != file_path so the structural filter keeps them. (reviewer inverse-direction finding)
    resp = {"changed_files": ["pkg/svc.go"], "impacted_symbols": [
        {"name": "Serve", "qualified_name": "github.com/org/repo/pkg.Serve", "file_path": "pkg/svc.go"},
    ]}
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": resp})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert "github.com/org/repo/pkg.Serve" in r["result"]          # kept despite "/" in qualified name
    assert "Changes impact (1 files → 1 symbols defined in them" in r["result"]


def test_changed_payload_ignores_target_and_raises_timeout_floor(monkeypatch):
    p, seen = _capturing_provider(monkeypatch, CAP_DETECT_CHANGES_CLEAN)
    p.build_result("changed", "ignored-target", [], 0, ROOT)
    assert seen["method"] == "detect_changes"
    assert seen["payload"] == {"project": "codeintel"}   # target never leaks into the payload
    assert seen["timeout"] >= 15000                        # higher floor: it drives a backend reindex


def test_a_retired_op_refuses_and_explains_itself(monkeypatch):
    """`deadcode` is retired: its implementation is gone and no flag brings it back.

    What has to survive that is the EXPLANATION. The op name is still in `_GRAPH_OPS`, so asking for
    it must produce `op-withdrawn` and a hint that says why and what to use instead — not
    `unsupported-op`, which would send an agent looking for a different tool for a question this
    tool has an accurate answer to (`callers` on a specific symbol).
    """
    monkeypatch.setenv("CODEINTEL_ENABLE_UNVERIFIED_OPS", "1")   # must not resurrect it
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "search_graph": CAP_HOTSPOTS})
    r = p.build_result("deadcode", "", [], 0, ROOT)

    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "op-withdrawn", r
    assert "callers" in (r.get("hint") or ""), r
    assert "retired" in (r.get("hint") or "").lower(), r


def test_hotspots_sorts_by_complexity_client_side(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "search_graph": CAP_HOTSPOTS})
    r = p.build_result("hotspots", "", [], 0, ROOT)
    assert r["result"] is not None
    # main (cx33) must rank above code_status_handler (cx15) despite the backend's name order.
    assert r["result"].index("__main__.main") < r["result"].index("code_status_handler")
    assert "builtins.len" not in r["result"]   # synthetic builtin filtered out
    assert "cx:33" in r["result"]


def test_hotspots_payload_has_min_degree(monkeypatch):
    p, seen = _capturing_provider(monkeypatch, CAP_HOTSPOTS)
    p.build_result("hotspots", "", [], 0, ROOT)
    assert seen["payload"].get("min_degree") is not None
    # Both node labels must be requested. Asking only for `Function` made every class method
    # invisible to this op — on one evaluated repo that hid 2,381 symbols — and a ranking that
    # cannot see methods reads exactly like a ranking that considered them.
    labels = {c["payload"].get("label") for c in seen["calls"]}
    assert labels == {"Function", "Method"}
    # The candidate set must be big enough that the client-side sort is ranking, not sampling: the
    # backend returns rows in NAME order, so a small cap makes "top hotspots" an alphabetical slice.
    assert seen["payload"].get("limit", 0) >= 1000


def test_chain_carries_risk_labels(monkeypatch):
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "trace_path": CAP_TRACE_RISK})
    r = p.build_result("chain", "code_status_handler", [], 0, ROOT)
    assert r["result"] is not None
    assert "[risk: CRITICAL]" in r["result"] and "[risk: HIGH]" in r["result"]


def test_chain_requests_risk_labels(monkeypatch):
    p, seen = _capturing_provider(monkeypatch, CAP_TRACE_RISK)
    p.build_result("chain", "code_status_handler", [], 0, ROOT)
    assert seen["method"] == "trace_path" and seen["payload"].get("risk_labels") is True


def test_new_ops_never_raise_when_run_throws(monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()

    def _boom(method, payload, timeout_ms):
        if method == "list_projects":
            return CAP_LIST_PROJECTS
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(p, "_run", _boom)
    for op in ("changed", "deadcode", "hotspots"):
        r = p.build_result(op, "", [], 0, ROOT)
        assert r["ok"] is True, f"{op} must never raise"


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
    # An incompatible backend is a real, reported condition (see the wire-format tests in
    # test_graph_provider.py and the doctor probe) — but it makes this end-to-end answer test
    # meaningless, so name it rather than failing with a confusing "no callers".
    if p._probe_wire_format(p._any_project_name(p._run("list_projects", {}, 30000))) is False:
        pytest.skip("installed codebase-memory-mcp speaks the 0.10.x text format, which this "
                    "release cannot parse — pin 0.9.x to run the live graph tests")
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


# ---- F5: `changed` must be scoped to SOURCE ------------------------------------------------
# Dogfooding on snitch-simulator reported "4 files → 28 symbols" where the files were `.gitignore`,
# two plan JSONs and `CODE_INTEL.md`, and all 28 "impacted symbols" were markdown HEADINGS out of
# CODE_INTEL.md — a file codeintel itself writes into the repo. On pathly-adapters it counted
# `.DS_Store`. A change-impact answer is about code; the indexer's corpus policy cannot be the
# filter here because `.md` is in the corpus DELIBERATELY, for semantic search.

def test_changed_drops_non_source_files_and_the_tools_own_artifact(monkeypatch):
    # The exact observed F5 payload: nothing here is source, and the "symbols" are md headings.
    resp = {"changed_files": [".gitignore", "plans/01-plan.json", "plans/02-plan.json",
                              "CODE_INTEL.md"],
            "impacted_symbols": [
                {"name": "## Top symbols", "qualified_name": "## Top symbols",
                 "file_path": "CODE_INTEL.md"},
                {"name": "## Entry points", "qualified_name": "## Entry points",
                 "file_path": "CODE_INTEL.md"},
            ]}
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": resp})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert r["ok"] is True and r["result"] is not None
    assert "CODE_INTEL.md" not in r["result"]      # never report the tool's own artifact
    assert ".gitignore" not in r["result"]
    assert "01-plan.json" not in r["result"]
    assert "Top symbols" not in r["result"]        # md headings are not impacted symbols
    # And it must NOT claim the tree is clean — there ARE changes, just none of them source.
    assert "working tree clean" not in r["result"]


def test_changed_drops_ds_store(monkeypatch):
    resp = {"changed_files": [".DS_Store"], "impacted_symbols": []}
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": resp})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert ".DS_Store" not in r["result"]


def test_changed_counts_only_source_when_mixed(monkeypatch):
    # Real source alongside junk: the header count must reflect the source only.
    resp = {"changed_files": ["CODE_INTEL.md", ".gitignore", "src/app.py"],
            "impacted_symbols": [
                {"name": "## Top symbols", "qualified_name": "## Top symbols",
                 "file_path": "CODE_INTEL.md"},
                {"name": "serve", "qualified_name": "src.app.serve", "file_path": "src/app.py"},
            ]}
    p = _provider(monkeypatch, {"list_projects": CAP_LIST_PROJECTS, "detect_changes": resp})
    r = p.build_result("changed", "", [], 0, ROOT)
    assert "Changes impact (1 files → 1 symbols defined in them" in r["result"]
    assert "src/app.py" in r["result"] and "src.app.serve" in r["result"]
    assert "CODE_INTEL.md" not in r["result"] and ".gitignore" not in r["result"]
