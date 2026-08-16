"""GraphProvider tests: never-raise invariant and key behavioral guarantees."""
from __future__ import annotations

import os
import re
import subprocess

from codeintel.providers.graph import GraphProvider
from codeintel.server import code_status_handler


def test_match_project_resolves_a_relative_path(tmp_path, monkeypatch):
    # The backend stores absolute root_paths, so a relative project_root (e.g. `codeintel map .`)
    # must be normalized before matching — otherwise resolution fails from inside the repo and the
    # map/graph query silently reports "not indexed" (the map-stub bug).
    real = os.path.realpath(str(tmp_path))
    raw = {"projects": [{"name": "myrepo", "root_path": real}]}
    monkeypatch.chdir(tmp_path)
    assert GraphProvider._match_project(raw, ".") == "myrepo"      # relative resolves now
    assert GraphProvider._match_project(raw, real) == "myrepo"     # absolute still works
    assert GraphProvider._match_project(raw, os.path.join(real, "src")) == "myrepo"  # subdir prefix


# ---------------------------------------------------------------------------
# Group 1 — Never-raise: None args
# ---------------------------------------------------------------------------

def test_graph_provider_none_args():
    p = GraphProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 2 — Never-raise: wrong types
# ---------------------------------------------------------------------------

def test_graph_provider_wrong_types():
    p = GraphProvider()
    r = p.build_result(123, [], {}, "bad", object())
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 3 — Backend unavailable: safe-null with reason
# ---------------------------------------------------------------------------

def test_graph_provider_unavailable(monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    p = GraphProvider()
    assert p.available is False
    r = p.build_result("symbol", "x", [], 0, "")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "engine-unavailable"


# ---------------------------------------------------------------------------
# Group 4 — Project not indexed: safe-null with reason
# ---------------------------------------------------------------------------

def test_graph_provider_project_not_indexed(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: [])
    r = p.build_result("impact", "fn", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "project-not-indexed"


# ---------------------------------------------------------------------------
# Group 5 — Subprocess issues
# ---------------------------------------------------------------------------

def test_graph_provider_subprocess_timeout(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()

    def _raise_timeout(method, payload, timeout_ms):
        raise subprocess.TimeoutExpired(cmd="codebase-memory-mcp", timeout=5)

    monkeypatch.setattr(p, "_run", _raise_timeout)
    r = p.build_result("impact", "fn", [], 0, "/repo")
    assert r["ok"] is True


def test_graph_provider_subprocess_crash(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: None)
    r = p.build_result("impact", "fn", [], 0, "/repo")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 6 — Unknown op
# ---------------------------------------------------------------------------

def test_graph_provider_unknown_op(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()
    p._project_cache[""] = "myproject"
    r = p.build_result("nonexistent-op", "x", [], 0, "")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "unsupported-op"


# ---------------------------------------------------------------------------
# Group 7 — engine field when available
# ---------------------------------------------------------------------------

def test_graph_provider_engine_field(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )

    def _fake_run(method, payload, timeout_ms):
        if method == "list_projects":
            return {"projects": [{"root_path": "/repo", "name": "myproj"}]}
        if method == "query_graph":
            # Real backend shape: value-arrays aligned to columns (not a list of dicts).
            return {
                "columns": ["a.name", "a.qualified_name", "a.file_path", "type(c)"],
                "rows": [["bar", "pkg.bar", "bar.py", "CALLS"]],
                "total": 1,
            }
        return None

    p = GraphProvider()
    monkeypatch.setattr(p, "_run", _fake_run)
    r = p.build_result("callers", "x", [], 0, "/repo")
    assert r["ok"] is True
    assert r["engine"] == "graph"
    # The real-shape row must actually be parsed into the rendered result.
    assert r["result"] is not None
    assert "pkg.bar" in r["result"]


# ---------------------------------------------------------------------------
# Group 8 — Server status reflects graph availability
# ---------------------------------------------------------------------------

def test_code_status_with_graph(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    r = code_status_handler({})
    assert r["ok"] is True
    assert "graph" in r["engines"]


def test_code_status_without_graph(monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    r = code_status_handler({})
    assert "graph" not in r["engines"]


# --------------------------------------------------------------------------- duplicate projects

def _projects(*entries) -> dict:
    return {"projects": [dict(e) for e in entries]}


def test_match_project_prefers_the_most_complete_of_duplicate_registrations(tmp_path):
    """The backend can hold two projects for ONE root — a short name and a path slug — that drift
    apart independently. Taking the first match answered queries from a stale index while a
    complete one sat beside it: observed as 1475 nodes vs 2631 for this repo, which is how
    `callers` reported a function's pre-refactor shape hours after the refactor landed.
    """
    root = str(tmp_path)
    raw = _projects(
        {"name": "stale-short-name", "root_path": root, "nodes": 1475, "edges": 2809},
        {"name": "fresh-path-slug", "root_path": root, "nodes": 2631, "edges": 8303},
    )
    assert GraphProvider._match_project(raw, root) == "fresh-path-slug"

    # ...and independently of the order the backend happens to list them in.
    reversed_raw = {"projects": list(reversed(raw["projects"]))}
    assert GraphProvider._match_project(reversed_raw, root) == "fresh-path-slug"


def test_match_project_keeps_the_first_listed_when_there_is_nothing_to_choose_between(tmp_path):
    """With no completeness signal — equal counts, or a backend that omits `nodes` entirely — fall
    back to the original first-listed rule rather than inventing an ordering."""
    root = str(tmp_path)
    raw = _projects(
        {"name": "aaa", "root_path": root, "nodes": 10},
        {"name": "zzz", "root_path": root, "nodes": 10},
    )
    assert GraphProvider._match_project(raw, root) == "aaa"
    assert GraphProvider._match_project({"projects": list(reversed(raw["projects"]))}, root) == "zzz"


def test_match_project_survives_a_missing_or_junk_node_count(tmp_path):
    """`nodes` comes from an untrusted backend payload; a missing or non-numeric one must not
    take out project resolution entirely."""
    root = str(tmp_path)
    raw = _projects(
        {"name": "no-count", "root_path": root},
        {"name": "junk-count", "root_path": root, "nodes": "lots"},
        {"name": "real-count", "root_path": root, "nodes": 5},
    )
    assert GraphProvider._match_project(raw, root) == "real-count"


def test_match_project_still_prefers_exact_over_a_richer_parent(tmp_path):
    """Completeness only breaks ties among EXACT matches — a huge parent-directory index must
    never outrank the repo's own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = _projects(
        {"name": "parent", "root_path": str(tmp_path), "nodes": 999_999},
        {"name": "repo", "root_path": str(repo), "nodes": 12},
    )
    assert GraphProvider._match_project(raw, str(repo)) == "repo"


# --------------------------------------------------------------------------- failure reasons

def test_every_dispatched_op_is_declared_supported():
    """`_GRAPH_OPS` gates the unsupported-op reply, so an op wired into _dispatch but missing from
    the set would be reported unsupported while being perfectly implemented."""
    import inspect

    from codeintel.providers.graph import _GRAPH_OPS

    source = inspect.getsource(GraphProvider._dispatch)
    dispatched = set(re.findall(r'op == "([a-z]+)"', source))
    assert dispatched == set(_GRAPH_OPS), f"drift: {dispatched ^ set(_GRAPH_OPS)}"


def _provider_answering(dispatch_result):
    """A GraphProvider with the backend seam stubbed: available, resolving to a project, and
    dispatching to a fixed result — so the reason-mapping is tested, not the subprocess."""
    gp = GraphProvider.__new__(GraphProvider)
    gp.available = True                                            # type: ignore[attr-defined]
    gp._resolve_project = lambda root: "proj"                      # type: ignore[method-assign]
    gp._dispatch = lambda *a, **k: dispatch_result                 # type: ignore[method-assign]
    return gp


def test_a_supported_op_that_finds_nothing_is_not_reported_unsupported(tmp_path):
    """The most misleading string the never-raise envelope produced: `callers` — a documented,
    implemented op — came back `unsupported-op` whenever the symbol was absent, which sends an
    agent hunting for another tool when the real fix is `codeintel index`.
    """
    res = _provider_answering(None).build_result(
        "callers", "a_symbol_nobody_added_yet", None, 1000, str(tmp_path))

    assert res["result"] is None
    assert res["reason"] == "not-in-graph"
    assert "codeintel index" in res["hint"]
    assert "a_symbol_nobody_added_yet" in res["hint"]


def test_a_genuinely_unknown_op_is_still_reported_unsupported(tmp_path):
    res = _provider_answering(None).build_result("frobnicate", "x", None, 1000, str(tmp_path))
    assert res["reason"] == "unsupported-op"


def test_overview_titles_with_the_repo_name_not_the_backend_project_id(tmp_path, monkeypatch):
    """The backend's project id is often a flattened absolute path
    (`Users-alice-Documents-project-myrepo`). This heading lands in CODE_INTEL.md, which gets
    committed and pushed — so an internal id there publishes the author's home directory layout.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    gp = GraphProvider.__new__(GraphProvider)
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "Users-alice-Documents-project-myrepo", "total_nodes": 5, "total_edges": 4})

    out = gp._op_overview("", "Users-alice-Documents-project-myrepo", 1000, str(repo))
    assert out.splitlines()[0] == "## Architecture: myrepo"
    assert "Users-alice" not in out


def test_overview_resolves_a_relative_root_before_naming_it(tmp_path, monkeypatch):
    """`codeintel map .` passes "." — whose basename is "." — and that titled the committed file
    with a dot."""
    gp = GraphProvider.__new__(GraphProvider)
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "backend-id", "total_nodes": 5, "total_edges": 4})
    monkeypatch.chdir(tmp_path)

    out = gp._op_overview("", "backend-id", 1000, ".")
    assert out.splitlines()[0] == f"## Architecture: {tmp_path.resolve().name}"


def test_overview_falls_back_to_the_backend_name_without_a_root(monkeypatch):
    gp = GraphProvider.__new__(GraphProvider)
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "backend-id", "total_nodes": 5, "total_edges": 4})

    assert gp._op_overview("", "backend-id", 1000, "").splitlines()[0] == "## Architecture: backend-id"


def test_result_lines_do_not_carry_the_backends_project_id(tmp_path):
    """The backend prefixes every qualified name with its project id — for a path-slug
    registration, the flattened ABSOLUTE PATH. Every result line therefore began
    `Users-alice-Documents-project-myrepo.src.pkg.fn`: the author's home directory repeated per
    row, on results that run to a hundred lines. Noise for a human, wasted tokens for the agent
    this tool exists to serve."""
    from codeintel.providers.graph import _strip_project_prefix

    assert _strip_project_prefix(
        "Users-alice-Documents-project-myrepo.src.pkg.fn") == "src.pkg.fn"
    assert _strip_project_prefix("my-repo.src.pkg.fn") == "src.pkg.fn"


def test_stripping_never_eats_a_real_module_path():
    """A hyphen cannot appear in a Python package name, which is what makes the slug detectable —
    so a genuine dotted module path must pass through untouched."""
    from codeintel.providers.graph import _strip_project_prefix

    for qn in ("src.codeintel.gateway.query", "codeintel.server.run", "main", "", "pkg.mod"):
        assert _strip_project_prefix(qn) == qn
