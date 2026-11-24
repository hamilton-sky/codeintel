"""Hints name only what the agent that reads them can do.

`codeintel install` registers one MCP server, `codeintel serve`, whose tools are `code.query`,
`code.status`, `code.doctor` and `code.map`. The graph backend's own tools (`index_repository`,
`search_graph`, `trace_path`) are never in the agent's toolset, so a hint that names them sends the
agent to a tool it does not have. The backend's stderr can also carry every indexed project's id —
a flattened absolute path — which a caller scoped to one root must not read.

The stderr tests drive a REAL executable (a `#!/bin/sh` script on PATH), as
`tests/test_backend_failure_detail.py` does, so the exit code and the stderr pipe run for real.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from codeintel.core.outcome import Missing
from codeintel.engines.graph.backend import _agent_facing
from codeintel.engines.graph.provider import GraphProvider, _suggest_op

ROOT = "/Users/x/Documents/project/codeintel"
LIST_PROJECTS = {"projects": [{"name": "codeintel", "root_path": ROOT}]}
_FOREIGN_TOOLS = ("index_repository", "search_graph", "trace_path")

_NAMES = ["compact_for_mcp", "code_query_handler", "compact", "render_compact_rows", "helper2"]


def _stub_provider(monkeypatch, *, node_rows=(), kind_rows=(), search=None):
    """A provider whose backend answers `list_projects`, `search_graph` over `_NAMES` (regex on the
    name, as the backend does), and `query_graph` with `node_rows` for the node probe, `kind_rows`
    for the dependency census and nothing for any edge query. Returns it with the list of
    `search_graph` payloads it was sent."""
    monkeypatch.setattr(
        "codeintel.engines.graph.provider.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    sent: list[dict] = []

    def run(method, payload, timeout_ms):
        if method == "list_projects":
            return LIST_PROJECTS
        if method == "search_graph":
            sent.append(payload)
            if search is not None:
                return search(payload)
            rx = re.compile(str(payload.get("name_pattern") or ""))
            return {"results": [{"name": n, "qualified_name": f"codeintel.pkg.{n}",
                                 "file_path": "pkg/server.py"} for n in _NAMES if rx.search(n)]}
        return None

    def rows(cypher, project, timeout_ms):
        if "type(c) AS kind" in cypher:
            return list(kind_rows)
        if "-[" not in cypher:
            return list(node_rows)
        return []

    monkeypatch.setattr(p, "_run", run)
    monkeypatch.setattr(p, "_query_rows", rows)
    return p, sent


# --------------------------------------------------------------------------- not in the graph

def test_a_typo_lists_the_close_name_and_names_no_backend_tool(monkeypatch):
    p, sent = _stub_provider(monkeypatch)
    env = p.build_result("callers", "compact_for_mcpp", [], 30000, ROOT)

    assert env["reason"] == "not-in-graph", env
    hint = env["hint"]
    assert "`compact_for_mcp`" in hint, hint
    assert "helper2" not in hint, "an unrelated name is not a close one"
    assert f"codeintel index {ROOT}" in hint, hint
    for tool in _FOREIGN_TOOLS:
        assert tool not in hint, hint
    assert len(sent) == 1, "one backend name search, not one per candidate"


def test_at_most_five_close_names_are_listed(monkeypatch):
    many = [f"compact_for_mcp{i}" for i in range(9)]
    p, _ = _stub_provider(monkeypatch, search=lambda payload: {"results": [
        {"name": n, "qualified_name": f"codeintel.{n}"} for n in many]})
    hint = p.build_result("callers", "compact_for_mcpp", [], 30000, ROOT)["hint"]

    assert sum(1 for n in many if f"`{n}`" in hint) == 5, hint


def test_a_failed_name_search_leaves_the_hint_without_names_and_the_reason_alone(monkeypatch):
    p, _ = _stub_provider(monkeypatch, search=lambda payload: None)
    env = p.build_result("callers", "compact_for_mcpp", [], 30000, ROOT)

    assert env["reason"] == "not-in-graph", env
    assert "did you mean" not in env["hint"].lower(), env["hint"]
    assert "`compact_for_mcp`" not in env["hint"], env["hint"]
    assert "codeintel index" in env["hint"], env["hint"]


def _failing_search(p, seen: list[int]):
    """A `search_graph` that records its time budget, records a backend failure as `_run` does,
    and returns nothing."""
    def search(method, payload, timeout_ms):
        seen.append(timeout_ms)
        p._last_failure = Missing("timeout", "the graph backend did not respond within the budget")
        return None
    return search


def test_a_failed_name_search_leaves_no_failure_behind_and_costs_less_than_a_full_budget(
        monkeypatch):
    p, _ = _stub_provider(monkeypatch)
    seen: list[int] = []
    inner = p._run
    fail = _failing_search(p, seen)
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        fail(method, payload, timeout_ms) if method == "search_graph"
        else inner(method, payload, timeout_ms)))
    env = p.build_result("callers", "compact_for_mcpp", [], 30000, ROOT)

    assert env["reason"] == "not-in-graph", env
    assert "did you mean" not in env["hint"].lower(), env["hint"]
    assert "timeout" not in json.dumps(env.get("gaps") or []), env
    assert p._last_failure is None, "a best-effort suggestion search leaves no failure behind"
    assert seen and all(0 < t < 30000 for t in seen), seen


def test_an_ancestor_scoped_answer_offers_no_names_from_outside_the_root(monkeypatch):
    p, sent = _stub_provider(monkeypatch)
    parent = ROOT.rsplit("/", 1)[0]
    inner = p._run
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        {"projects": [{"name": "parent", "root_path": parent}]} if method == "list_projects"
        else inner(method, payload, timeout_ms)))
    env = p.build_result("callers", "compact_for_mcpp", [], 30000, ROOT)

    assert env["reason"] == "not-in-graph", env
    assert "`compact_for_mcp`" not in env["hint"], env["hint"]
    assert "did you mean" not in env["hint"].lower(), env["hint"]
    assert not sent, "no name search runs over a project wider than the caller's root"


# --------------------------------------------------------------------------- backend stderr

def _fake_backend(tmp_path: Path, monkeypatch, stderr: str) -> None:
    """A `codebase-memory-mcp` on PATH that lists one project and fails every other call with
    `stderr` on its stderr and exit 1, as the real backend does."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    listing = json.dumps(LIST_PROJECTS)
    exe = bindir / "codebase-memory-mcp"
    exe.write_text(
        "#!/bin/sh\n"
        f"if [ \"$2\" = list_projects ]; then printf '%s' '{listing}'; exit 0; fi\n"
        f"printf '%s\\n' '{stderr}' >&2\nexit 1\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake backend is a sh script")


@posix_only
@pytest.mark.parametrize("stderr", [
    '{"error":"query failed","available_projects":["a-b-c","d-e-f"],"count":17}',
    # The tail of a long list, cut from the front, as the old 400-character cap left it.
    '…b-c","d-e-f"],"count":17}',
])
def test_a_backend_error_never_carries_other_projects_ids(tmp_path, monkeypatch, stderr):
    _fake_backend(tmp_path, monkeypatch, stderr)
    env = GraphProvider().build_result("callers", "helper", [], 30000, ROOT)

    assert env["reason"] == "backend-error", env
    said = json.dumps(env)
    assert "a-b-c" not in said and "d-e-f" not in said and "b-c" not in said, said
    assert '"count":17' not in env["hint"], env["hint"]


@posix_only
def test_a_backend_project_not_found_is_project_not_indexed(tmp_path, monkeypatch):
    _fake_backend(tmp_path, monkeypatch,
                  '{"error":"project not found or not indexed","hint":"Use list_projects to see '
                  'all indexed projects","available_projects":["tenant-b-secret"],"count":1}')
    env = GraphProvider().build_result("callees", "helper", [], 30000, ROOT)

    assert env["reason"] == "project-not-indexed", env
    assert "tenant-b-secret" not in json.dumps(env), env
    assert f"codeintel index {ROOT}" in env["hint"], env["hint"]


@posix_only
def test_the_backends_own_tool_advice_is_rewritten_to_code_query(tmp_path, monkeypatch):
    _fake_backend(tmp_path, monkeypatch,
                  '{"error":"function not found","function_name":"helper","hint":"Use '
                  'search_graph(name_pattern=\\".*helper.*\\") to find the exact name, then pass '
                  'it to trace_path."}')
    env = GraphProvider().build_result("chain", "helper", [], 30000, ROOT)

    said = json.dumps(env)
    for tool in _FOREIGN_TOOLS:
        assert tool not in said, said
    assert "code.query op=pattern target=helper" in env["hint"], env["hint"]


@pytest.mark.parametrize("said", [
    "project home-alice-work-repo not found",
    "project 'home-alice-work-repo' not found",
    'project "home-alice-work-repo" not found',
    "query failed for project: home-alice-work-repo",
])
def test_a_flattened_project_id_is_withheld_however_it_is_quoted(said):
    cleaned = _agent_facing(said)

    assert "home-alice-work-repo" not in cleaned, cleaned
    assert "alice" not in cleaned, cleaned


def test_the_backends_list_projects_advice_names_an_agent_tool():
    cleaned = _agent_facing("Use list_projects to see all indexed projects")

    assert "list_projects" not in cleaned, cleaned
    assert "code.status" in cleaned, cleaned


@posix_only
def test_a_bare_project_id_in_stderr_never_reaches_the_hint(tmp_path, monkeypatch):
    _fake_backend(tmp_path, monkeypatch, "query failed: project home-alice-work-repo is locked")
    env = GraphProvider().build_result("callers", "helper", [], 30000, ROOT)

    assert env["reason"] == "backend-error", env
    assert "alice" not in json.dumps(env), env


# --------------------------------------------------------------------------- unsupported op

def test_graph_symbol_points_at_lsp_not_at_itself(monkeypatch):
    p, _ = _stub_provider(monkeypatch)
    env = p.build_result("symbol", "Gateway", [], 30000, ROOT)

    assert env["reason"] == "unsupported-op", env
    hint = env["hint"]
    assert "did you mean symbol" not in hint, hint
    assert not re.search(r"\bchanges\b", hint), hint
    assert "engine=lsp" in hint and "op=context" in hint and "op=pattern" in hint, hint


def test_graph_search_points_at_the_semantic_engine(monkeypatch):
    p, _ = _stub_provider(monkeypatch)
    hint = p.build_result("search", "x", [], 30000, ROOT)["hint"]

    assert "engine=semantic" in hint, hint
    assert "did you mean search" not in hint, hint
    assert not re.search(r"\bchanges\b", hint), hint


def test_suggest_op_never_offers_the_op_asked_an_alias_or_a_withdrawn_op():
    assert "symbol" not in _suggest_op("symbol")
    assert "changes" not in _suggest_op("change")
    assert "changes" not in _suggest_op("chnges")
    assert "deadcode" not in _suggest_op("deadcod")
    assert "callers" in _suggest_op("callres")


# --------------------------------------------------------------------------- no edges

_NODE = [{"n.qualified_name": "codeintel.pkg.leaf", "n.file_path": "pkg/b.py"}]
# Backend 0.9.0 answers `type(c) AS kind` with `[["1","1"]]`; DEFINES/CONTAINS are structural.
_KINDS = [{"kind": "1", "n": "1"}, {"kind": "DEFINES", "n": "1"}, {"kind": "CONTAINS", "n": "2"}]


@pytest.mark.parametrize("op", ["callees", "impact", "context"])
def test_a_no_edges_hint_off_callers_is_about_outgoing_calls_not_dead_code(monkeypatch, op):
    p, _ = _stub_provider(monkeypatch, node_rows=_NODE, kind_rows=_KINDS)
    env = p.build_result(op, "leaf", [], 30000, ROOT)

    assert env["reason"] == "no-edges", env
    hint = env["hint"]
    assert "dead code" not in hint, hint
    assert "no outgoing calls resolved" in hint, hint
    assert "1 1" not in hint, hint
    assert "DEFINES" not in hint and "CONTAINS" not in hint, hint
    assert "Other relationships DO point at it" not in hint, hint


def test_a_no_edges_callers_hint_keeps_the_dead_code_caveat(monkeypatch):
    p, _ = _stub_provider(monkeypatch, node_rows=_NODE, kind_rows=_KINDS)
    env = p.build_result("callers", "leaf", [], 30000, ROOT)

    assert env["reason"] == "no-edges", env
    hint = env["hint"]
    assert "dead code" in hint, hint
    assert "1 1" not in hint, hint
    assert "Other relationships DO point at it" not in hint, hint


def test_a_real_edge_kind_is_still_named(monkeypatch):
    kinds = [*_KINDS, {"kind": "CALLS", "n": "2"}]
    p, _ = _stub_provider(monkeypatch, node_rows=_NODE, kind_rows=kinds)
    hint = p.build_result("callees", "leaf", [], 30000, ROOT)["hint"]

    assert "Other relationships DO point at it: 2 CALLS" in hint, hint
    assert "1 1" not in hint and "DEFINES" not in hint, hint


# --------------------------------------------------------------------------- stderr cleaning

def test_a_huge_stderr_is_cleaned_in_well_under_a_second():
    """`_agent_facing` runs on the whole stderr, before the cap; a backend that prints many
    unclosed `search_graph(name_pattern="` advices must not stall the request."""
    import time

    from codeintel.engines.graph.backend import _stderr_tail

    raw = ('search_graph(name_pattern="x ' * 4000)[:100_000].encode()
    started = time.monotonic()
    out = _stderr_tail(raw)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"cleaning 100 KB of stderr took {elapsed:.2f}s"
    assert "search_graph" not in out, out


@pytest.mark.parametrize("said, ids", [
    ("available projects: home-alice-work, home-bob-repo-x", ("alice", "bob")),
    ("project not found; projects = home-alice-work-repo, srv-bob-x-y, home-carol",
     ("alice", "bob", "carol")),
    ("candidates [home-alice-work-repo, srv-bob-x-y]", ("alice", "bob")),
    ('candidates ["home-alice-work-repo", "home-bob"]', ("alice", "bob")),
])
def test_every_project_id_in_a_list_is_withheld(said, ids):
    cleaned = _agent_facing(said)

    for who in ids:
        assert who not in cleaned, cleaned


def test_the_backends_deprecation_warning_is_not_rewritten_into_a_command_that_does_not_exist():
    from codeintel.engines.graph.backend import _stderr_tail

    out = _stderr_tail(
        b"warning: passing raw JSON to 'cli search_graph' is deprecated and will be removed in a "
        b"future release; use flags (run 'cli search_graph --help'), --args-file <path>, or piped "
        b"stdin.\nerror: query failed\n")

    assert "cli code.query" not in out, out
    assert "op=pattern --help" not in out, out
    assert "query failed" in out, out
