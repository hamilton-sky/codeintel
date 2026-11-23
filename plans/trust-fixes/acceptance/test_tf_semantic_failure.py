"""Story: a user on a host where the embedding model cannot download.

The model download is blocked the way it is on an air-gapped host: `HF_HUB_OFFLINE=1` and an empty
`FASTEMBED_CACHE_PATH`, so fastembed fails fast with "Could not load model ... from any source"
(spec Scope: "A real semantic model download. Mocks only." — nothing is downloaded).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest
from _fakegraph import FakeGraph, gap_kinds

from codeintel.engines.semantic.provider import SemanticProvider
from codeintel.gateway.gateway import Gateway
from codeintel.gateway.policy import TieringPolicy
from codeintel.gateway.reindexer import Reindexer
from codeintel.transports import mcp as server


@pytest.fixture
def blocked(tmp_path, monkeypatch):
    """An environment (this process and its children) where the model cannot be fetched."""
    home = tmp_path / "home"
    home.mkdir()
    cache = tmp_path / "fastembed-empty"
    cache.mkdir()
    values = {
        "HOME": str(home),
        "CODEINTEL_HOME": str(home / ".codeintel"),
        "HF_HUB_OFFLINE": "1",
        "FASTEMBED_CACHE_PATH": str(cache),
        "NO_COLOR": "1",
        "PATH": "/usr/bin:/bin",            # no graph backend, no LSP: semantic is the subject
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CODEINTEL_REINDEX", raising=False)
    env = dict(os.environ)
    env.update(values)
    return env


def _repo(tmp_path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "core.py").write_text("def helper():\n    return 1\n\n\ndef compute():\n"
                                  "    return helper()\n")
    return str(root)


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "codeintel", *args], env=env,
                          capture_output=True, text=True, timeout=180)


def _failed_index(env: dict, root: str) -> None:
    out = _cli(env, "index", root)
    assert out.returncode != 0, "precondition: the index pass should fail to load the model"
    assert "could not load embedding model" in (out.stdout + out.stderr)


def _fastembed_tried(stderr: str) -> bool:
    """Whether this process attempted to load the model (fastembed logs every attempt)."""
    return "fastembed" in stderr and "Could not" in stderr


# Spec: After an index pass records a model-load failure, the next `search` (in any process)
# returns `index-failed` with the cause within 1 s. It never says "first-time indexing started".
def test_search_after_a_recorded_model_failure_is_index_failed_with_the_cause_within_a_second(
        tmp_path, blocked, monkeypatch):
    root = _repo(tmp_path)
    _failed_index(blocked, root)                       # another process records the failure
    monkeypatch.setattr("codeintel.engines.graph.provider.shutil.which", lambda x: None)
    server._reset_gateway()
    started = time.monotonic()
    env = server.code_query_handler({"op": "search", "target": "helper", "project_root": root})
    elapsed = time.monotonic() - started
    assert env.get("reason") == "index-failed", env
    assert "could not load" in str(env.get("hint") or "").lower(), env
    assert "first-time indexing" not in json.dumps(env), env
    assert elapsed < 1.0, f"took {elapsed:.2f}s"


# Spec: `doctor` reports semantic `runnable: false` with "last index could not load model:
# <cause>".
def test_doctor_reports_semantic_not_runnable_with_the_recorded_model_failure(tmp_path, blocked):
    root = _repo(tmp_path)
    _failed_index(blocked, root)
    out = _cli(blocked, "doctor", root, "--json")
    report = json.loads(out.stdout)
    semantic = report["engines"]["semantic"]
    assert semantic["runnable"] is False, semantic
    assert "last index could not load model:" in semantic["detail"], semantic["detail"]


# Spec: `status` names that cause instead of "the next index downloads ~50 MB".
def test_status_names_the_recorded_model_failure(tmp_path, blocked):
    root = _repo(tmp_path)
    _failed_index(blocked, root)
    out = _cli(blocked, "status", root)
    text = out.stdout + out.stderr
    assert "the next index downloads" not in text, text
    assert "could not load model" in text.lower(), text


# Edge case: Model failure recorded, then `FASTEMBED_CACHE_PATH` set ... -> the record no longer
# applies; an index is tried again.
def test_a_recorded_model_failure_stops_applying_once_fastembed_cache_path_changes(
        tmp_path, blocked):
    root = _repo(tmp_path)
    _failed_index(blocked, root)

    same = _cli(blocked, "query", "--op", "search", "--target", "helper",
                "--project-root", root, "--json")
    assert json.loads(same.stdout).get("reason") == "index-failed"
    assert not _fastembed_tried(same.stderr), "the recorded failure was retried as if unknown"

    elsewhere = tmp_path / "fastembed-seeded-elsewhere"
    elsewhere.mkdir()
    moved = dict(blocked, FASTEMBED_CACHE_PATH=str(elsewhere))
    again = _cli(moved, "query", "--op", "search", "--target", "helper",
                 "--project-root", root, "--json")
    assert _fastembed_tried(again.stderr), "a changed FASTEMBED_CACHE_PATH did not retry the index"


# ------------------------------------------------------------------------- graph name fallback

class _FailingEmbedding:
    """fastembed's model class on a host where the weights cannot be fetched."""

    def __init__(self, *args, **kwargs):
        raise ValueError("Could not load model BAAI/bge-small-en-v1.5 from any source.")


def _fallback_gateway(tmp_path, monkeypatch, policy=None) -> tuple[Gateway, str]:
    root = _repo(tmp_path)
    monkeypatch.setattr("fastembed.TextEmbedding", _FailingEmbedding)
    g = FakeGraph(root=root)
    g.node("core.helper", "core.py")
    g.node("core.compute", "core.py")
    graph = g.install(monkeypatch, root)
    gw = Gateway(graph=graph, semantic=SemanticProvider(blocking_index=True), policy=policy,
                 reindexer=Reindexer(enabled=False), oneshot=True)
    return gw, root


# Spec: When semantic is unavailable, `search` falls back to the graph name search, with
# `fallback: graph-name-search` and a gap saying the result is name-only.
def test_search_with_semantic_unavailable_falls_back_to_graph_name_search(
        tmp_path, blocked, monkeypatch):
    gw, root = _fallback_gateway(tmp_path, monkeypatch)
    env = gw.query(op="search", target="helper", project_root=root)
    assert env.get("fallback") == "graph-name-search", env
    assert "helper" in str(env.get("result") or ""), env
    assert env.get("gaps"), env
    assert any("name" in str(g.get("detail") or "").lower() for g in env["gaps"]), env["gaps"]
    assert env.get("confidence") == "partial"


# Edge case: Auto `search` falls back under an RBAC role allowed `search` but not `pattern` -> no
# fallback; the semantic answer is returned as it was.
def test_search_fallback_is_not_used_for_a_role_that_may_not_run_pattern(
        tmp_path, blocked, monkeypatch):
    policy = TieringPolicy(enabled=True, rules={"searcher": ["search"]},
                           roots={"searcher": ["*"], "admin": ["*"]})
    gw, root = _fallback_gateway(tmp_path, monkeypatch, policy=policy)

    allowed = gw.query(op="search", target="helper", project_root=root, role="admin")
    assert allowed.get("fallback") == "graph-name-search", allowed

    restricted = gw.query(op="search", target="helper", project_root=root, role="searcher")
    assert "fallback" not in restricted, restricted
    assert restricted.get("reason") == "index-failed", restricted
    assert "helper" not in str(restricted.get("result") or "")
    assert "graph-name-search" not in gap_kinds(restricted)


# ------------------------------------------------------------------------- reindexer order

# Spec: `Reindexer._do_reindex(root)` (the pass `Reindexer.maybe_reindex` submits) runs
# `_graph_reindex(root)` before `_semantic_reindex(root)` ... A test replaces both methods with
# recorders and reads the call order.
def test_do_reindex_runs_the_graph_pass_before_the_semantic_pass(tmp_path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(Reindexer, "_graph_reindex", lambda self, root: order.append("graph"))
    monkeypatch.setattr(Reindexer, "_semantic_reindex", lambda self, root: order.append("semantic"))
    Reindexer()._do_reindex(str(tmp_path))
    assert order == ["graph", "semantic"], order


# Spec: ... and still runs the graph pass when the semantic pass would block or raise.
def test_do_reindex_runs_the_graph_pass_while_the_semantic_pass_blocks(tmp_path, monkeypatch):
    graph_ran = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(Reindexer, "_graph_reindex", lambda self, root: graph_ran.set())
    monkeypatch.setattr(Reindexer, "_semantic_reindex", lambda self, root: release.wait(20))
    t = threading.Thread(target=Reindexer()._do_reindex, args=(str(tmp_path),), daemon=True)
    t.start()
    try:
        assert graph_ran.wait(3), "the graph pass waited behind a blocked semantic pass"
    finally:
        release.set()
        t.join(10)


# Spec: ... and still runs the graph pass when the semantic pass would ... raise.
def test_do_reindex_runs_the_graph_pass_when_the_semantic_pass_raises(tmp_path, monkeypatch):
    order: list[str] = []

    def _raise(self, root):
        order.append("semantic")
        raise RuntimeError("could not load embedding model")

    monkeypatch.setattr(Reindexer, "_graph_reindex", lambda self, root: order.append("graph"))
    monkeypatch.setattr(Reindexer, "_semantic_reindex", _raise)
    Reindexer()._do_reindex(str(tmp_path))
    assert order == ["graph", "semantic"], order
