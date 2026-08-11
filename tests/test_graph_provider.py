"""GraphProvider tests: never-raise invariant and key behavioral guarantees."""
from __future__ import annotations

import subprocess

from codeintel.providers.graph import GraphProvider
from codeintel.server import code_status_handler


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
            return [{"root_path": "/repo", "name": "myproj"}]
        if method == "query_graph":
            return [{"caller.name": "bar", "caller.file_path": "bar.py"}]
        return None

    p = GraphProvider()
    monkeypatch.setattr(p, "_run", _fake_run)
    r = p.build_result("callers", "x", [], 0, "/repo")
    assert r["ok"] is True
    assert r["engine"] == "graph"


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
