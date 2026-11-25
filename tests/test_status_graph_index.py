"""`status` reports the GRAPH index's own age, and whether the working tree has moved past it.

`codeintel status` printed one "Index age" line, read from the semantic database. The graph index
is the one `callers`, `impact` and `changed` answer from, so a stale graph sat under a fresh-looking
age, and a graph indexed a minute ago sat under "Index age: unknown for this repo — run `codeintel
index`" whenever the embedding model could not be downloaded (re-running would not have helped).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = "tmp-status-age"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str, when: float | None = None) -> None:
    env = {**os.environ, **_GIT_ENV}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                   capture_output=True, check=True, env=env)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text("def helper():\n    return 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base", when=time.time() - 600)
    old = time.time() - 300
    os.utime(root / "pkg" / "core.py", (old, old))
    return root


def _index_at(tmp_path: Path, monkeypatch, when: float) -> None:
    cache = tmp_path / "graph-cache"
    cache.mkdir(exist_ok=True)
    db = cache / f"{PROJECT}.db"
    db.write_bytes(b"")
    os.utime(db, (when, when))
    monkeypatch.setenv("CBM_CACHE_DIR", str(cache))


def _status(monkeypatch, root: Path, graph_probe: dict) -> dict:
    from codeintel.transports import mcp as server

    probes = {
        "graph": {"installed": True, "runnable": True, "status": "ok", **graph_probe},
        "lsp": {"installed": False, "status": "fail"},
        "semantic": {"installed": True, "status": "fail", "repo_indexed": False},
    }
    monkeypatch.setattr("codeintel.setup.doctor.run_doctor",
                        lambda *a, **k: {"engines": probes, "summary": {"healthy": False}})
    server._reset_gateway()
    try:
        return server.code_status_handler({"project_root": str(root)})
    finally:
        server._reset_gateway()


@pytest.mark.usefixtures("real_index_age")
def test_status_says_the_graph_index_is_stale_when_a_file_changed_after_it(
        monkeypatch, tmp_path, repo):
    _index_at(tmp_path, monkeypatch, time.time() - 120)
    (repo / "pkg" / "core.py").write_text("def helper():\n    return 2\n")

    out = _status(monkeypatch, repo, {"repo_indexed": True, "project": PROJECT})

    gi = out["graph_index"]
    assert gi["project"] == PROJECT
    assert gi["state"] == "stale", gi
    assert "pkg/core.py" in gi["detail"], gi
    assert 100 <= gi["age_seconds"] <= 200, gi


@pytest.mark.usefixtures("real_index_age")
def test_status_says_the_graph_index_is_fresh_when_nothing_moved_after_it(
        monkeypatch, tmp_path, repo):
    _index_at(tmp_path, monkeypatch, time.time() - 60)

    out = _status(monkeypatch, repo, {"repo_indexed": True, "project": PROJECT})

    assert out["graph_index"]["state"] == "fresh", out["graph_index"]


def test_status_has_no_graph_index_age_when_the_graph_has_not_indexed_the_repo(
        monkeypatch, tmp_path, repo):
    out = _status(monkeypatch, repo, {"repo_indexed": False, "project": None})

    assert out["graph_index"] is None


# ------------------------------------------------------------------------------------ CLI text

def _args(**kw):
    return SimpleNamespace(**{"project_root": None, "json": False, "deep": False, **kw})


def _cli(monkeypatch, tmp_path, capsys, status: dict, **kw) -> str:
    monkeypatch.setattr("codeintel.transports.mcp.code_status_handler", lambda args: status)
    monkeypatch.setattr("codeintel.engines.semantic.db.default_db_path",
                        lambda model: str(tmp_path / "none.db"))
    import_module("codeintel.cli.status").run(_args(project_root=str(tmp_path), **kw))
    return capsys.readouterr().out


def test_status_text_prints_a_stale_graph_index_with_the_fix(monkeypatch, tmp_path, capsys):
    out = _cli(monkeypatch, tmp_path, capsys, {
        "readiness": {"graph": {"status": "ok"}}, "healthy": True,
        "graph_index": {"project": PROJECT, "written_at": time.time() - 3700,
                        "age_seconds": 3700, "state": "stale",
                        "detail": "the graph index was last written 5m before pkg/core.py last changed"},
    })

    line = next(ln for ln in out.splitlines() if ln.startswith("Graph index"))
    assert "1h 1m" in line and "STALE" in line, out
    assert "pkg/core.py" in out
    assert f"codeintel index {tmp_path}" in out


def test_status_text_prints_a_fresh_graph_index(monkeypatch, tmp_path, capsys):
    out = _cli(monkeypatch, tmp_path, capsys, {
        "readiness": {"graph": {"status": "ok"}}, "healthy": True,
        "graph_index": {"project": PROJECT, "written_at": time.time() - 120,
                        "age_seconds": 120, "state": "fresh", "detail": ""},
    })

    assert "Graph index: 0h 2m old, current with the working tree" in out, out


def test_status_does_not_advise_reindexing_when_the_semantic_engine_cannot_load_its_model(
        monkeypatch, tmp_path, capsys):
    (tmp_path / "none.db").write_bytes(b"")
    out = _cli(monkeypatch, tmp_path, capsys, {
        "readiness": {"semantic": {"status": "fail", "repo_indexed": False,
                                   "remediation": "check network/proxy access to huggingface.co"}},
        "healthy": False,
    })

    line = next(ln for ln in out.splitlines() if ln.startswith("Semantic index"))
    assert "codeintel index" not in line, line
    assert "codeintel doctor" in line, line


def test_status_json_carries_the_graph_index_block(monkeypatch, tmp_path, capsys):
    block = {"project": PROJECT, "written_at": 1.0, "age_seconds": 5, "state": "fresh",
             "detail": ""}
    out = _cli(monkeypatch, tmp_path, capsys,
               {"readiness": {}, "healthy": True, "graph_index": block}, json=True)

    assert json.loads(out)["graph_index"] == block
