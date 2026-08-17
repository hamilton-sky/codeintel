"""GraphProvider subprocess contract: piped-stdin primary + deprecated raw-JSON fallback.

The stable, non-deprecated backend form is `echo '<json>' | codebase-memory-mcp cli <method>`
(verified live — no deprecation warning). These tests pin the argv/stdin contract that would
silently drift, plus a live test against the real backend.
"""
from __future__ import annotations

import json
import os
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
    # `index_repository`, NOT `detect_changes`: this test used to pin the latter, which only
    # reports uncommitted drift and never refreshes the graph — so it locked in a reindex that
    # reindexed nothing.
    assert argv[:3] == ["/fake/codebase-memory-mcp", "cli", "index_repository"]
    assert inp == b'{"repo_path": "/some/repo"}'      # stdin, not a positional raw-JSON arg


@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None, reason="codebase-memory-mcp backend not installed"
)
def test_live_stdin_list_projects():
    """Acceptance test for the migration: the REAL backend answers the piped-stdin form.
    When this is green in CI, the raw-JSON fallback in _run can be removed."""
    p = GraphProvider()
    # Not 3000ms: the real backend re-initialises a native runtime per invocation and measures
    # ~5.8s, so that budget failed here for the same reason it disabled resolution in the
    # product. A live test that times out reads as a contract failure and sends the reader
    # looking for a protocol bug that is not there.
    raw = p._run_stdin("list_projects", "{}", 30000)
    assert isinstance(raw, dict) and "projects" in raw


@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None, reason="codebase-memory-mcp backend not installed"
)
def test_live_reindex_argument_name_is_the_one_the_backend_accepts(tmp_path):
    """The test the mocked one above cannot be: does the REAL backend accept our payload?

    Every unit test here asserts we send what we *intended* to send, which is worthless when the
    intent is wrong — and it was, twice. `detect_changes`/`project_root` silently errored for
    months; the replacement `index_repository`/`project_root` shipped in 0.13.1 and still failed,
    because the parameter is `repo_path`. The backend answers a wrong name with "Indexing worker
    crashed on a file", so the error text actively points away from the cause.

    This asks the backend directly, against a one-file repo so it stays fast.
    """
    (tmp_path / "sample.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)

    from codeintel.reindexer import Reindexer
    Reindexer()._graph_reindex(str(tmp_path))

    # The reindex must have registered the repo — proof the payload was accepted, not just sent.
    # 5000ms was below the real backend's measured ~5.8s per invocation, so this timed out and
    # the assertion below blamed the payload for what was actually the deadline — the same
    # mistake that disabled project resolution in the product.
    raw = GraphProvider()._run_stdin("list_projects", "{}", 30000)
    roots = {p.get("root_path") for p in raw.get("projects", []) if isinstance(p, dict)}
    assert str(tmp_path.resolve()) in {os.path.realpath(r) for r in roots if r}, (
        "the backend did not register the repo — the reindex payload was rejected")
