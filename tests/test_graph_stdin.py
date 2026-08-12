"""GraphProvider subprocess contract: piped-stdin primary + deprecated raw-JSON fallback.

The stable, non-deprecated backend form is `echo '<json>' | codebase-memory-mcp cli <method>`
(verified live — no deprecation warning). These tests pin the argv/stdin contract that would
silently drift, plus a live test against the real backend.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from codeintel.providers.graph import GraphProvider


class _Done:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _provider(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp"
    )
    return GraphProvider()


def test_run_pipes_payload_via_stdin_no_positional_arg(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("input")))
        return _Done(stdout=json.dumps({"projects": []}).encode(), returncode=0)

    monkeypatch.setattr("codeintel.providers.graph.subprocess.run", fake_run)
    p = _provider(monkeypatch)
    out = p._run("list_projects", {"x": 1}, 3000)

    assert out == {"projects": []}
    argv, inp = calls[0]
    assert argv == ["/fake/codebase-memory-mcp", "cli", "list_projects"]  # no raw-JSON positional
    assert inp == b'{"x": 1}'      # payload delivered over stdin
    assert len(calls) == 1          # stdin succeeded → fallback NOT used


def test_falls_back_to_rawjson_when_stdin_fails(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if len(argv) == 3:                       # stdin form → simulate an old backend rejecting it
            return _Done(stdout=b"", returncode=2)
        return _Done(stdout=json.dumps({"ok": 1}).encode(), returncode=0)  # raw-JSON form works

    monkeypatch.setattr("codeintel.providers.graph.subprocess.run", fake_run)
    p = _provider(monkeypatch)
    out = p._run("list_projects", {}, 3000)

    assert out == {"ok": 1}
    assert len(calls) == 2
    assert len(calls[0]) == 3 and len(calls[1]) == 4   # stdin (no arg) then raw-JSON (positional)


def test_stdin_and_fallback_timeout_returns_none(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr("codeintel.providers.graph.subprocess.run", boom)
    p = _provider(monkeypatch)
    assert p._run("list_projects", {}, 100) is None   # never raises


def test_reindexer_graph_reindex_routes_through_stdin(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp"
    )
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("input")))
        return _Done(stdout=b"{}", returncode=0)

    monkeypatch.setattr("codeintel.providers.graph.subprocess.run", fake_run)
    from codeintel.reindexer import Reindexer
    Reindexer()._graph_reindex("/some/repo")

    assert calls, "no subprocess call made"
    argv, inp = calls[0]
    assert argv[:3] == ["/fake/codebase-memory-mcp", "cli", "detect_changes"]
    assert inp == b'{"project_root": "/some/repo"}'   # stdin, not a positional raw-JSON arg


@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None, reason="codebase-memory-mcp backend not installed"
)
def test_live_stdin_list_projects():
    """Acceptance test for the migration: the REAL backend answers the piped-stdin form.
    When this is green in CI, the raw-JSON fallback in _run can be removed."""
    p = GraphProvider()
    raw = p._run_stdin("list_projects", "{}", 3000)
    assert isinstance(raw, dict) and "projects" in raw
