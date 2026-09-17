"""Production-hardening behaviors added in 0.3.0:
  - SemanticProvider only pays the inline full-index on a COLD repo (warm rides the reindexer),
    but always indexes when background reindexing is disabled.
  - Indexer honours a global per-pass chunk ceiling (memory backstop).
  - overview auto-routing falls back to LSP when the repo isn't in the graph (not only when the
    graph backend is unavailable).
  - log_swallowed never raises.
"""
from __future__ import annotations

import os

import pytest

import codeintel.providers.semantic as sem
from codeintel.gateway import Gateway
from codeintel.provider import log_swallowed

# --------------------------------------------------------------------------- semantic cold/warm

def _patch_semantic(monkeypatch, tmp_path, has_index_value):
    """Point the provider at a throwaway db and stub the heavy Searcher/Indexer bits."""
    from codeintel.indexer import Indexer
    from codeintel.searcher import Searcher

    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    calls = {"index": 0}

    def _fake_index(self, root):
        calls["index"] += 1
        return 0

    monkeypatch.setattr(Indexer, "index", _fake_index)
    monkeypatch.setattr(Searcher, "has_index", lambda self, root: has_index_value)
    monkeypatch.setattr(Searcher, "search", lambda self, *a, **k: [])
    return calls


def test_cold_repo_triggers_inline_index(tmp_path, monkeypatch):
    if not sem._DEPS_OK:
        pytest.skip("semantic deps missing")
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    calls = _patch_semantic(monkeypatch, tmp_path, has_index_value=False)
    sem.SemanticProvider().build_result("search", "q", [], 0, str(tmp_path))
    assert calls["index"] == 1  # nothing indexed yet → index inline


def test_warm_repo_skips_inline_index(tmp_path, monkeypatch):
    if not sem._DEPS_OK:
        pytest.skip("semantic deps missing")
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    calls = _patch_semantic(monkeypatch, tmp_path, has_index_value=True)
    sem.SemanticProvider().build_result("search", "q", [], 0, str(tmp_path))
    assert calls["index"] == 0  # already indexed + background reindex on → skip the costly walk


def test_reindex_off_forces_inline_index_even_when_warm(tmp_path, monkeypatch):
    if not sem._DEPS_OK:
        pytest.skip("semantic deps missing")
    monkeypatch.setenv("CODEINTEL_REINDEX", "off")
    calls = _patch_semantic(monkeypatch, tmp_path, has_index_value=True)
    sem.SemanticProvider().build_result("search", "q", [], 0, str(tmp_path))
    assert calls["index"] == 1  # no background reindex → inline pass is the only freshness path


# --------------------------------------------------------------------------- indexer global cap

def test_indexer_stops_at_max_total_chunks(tmp_path):
    from codeintel.indexer import Indexer, _project_key
    from codeintel.semantic_db import SemanticDb

    for i in range(6):
        (tmp_path / f"f{i}.py").write_text("\n".join(f"x = {j}" for j in range(60)))

    db = SemanticDb(str(tmp_path / "db"))
    db.init()
    real = os.path.realpath(str(tmp_path))
    chunks = Indexer(db, max_chunks=100, max_total_chunks=3)._collect_new_chunks(
        tmp_path, _project_key(real), real
    )
    db.close()

    # The ceiling is checked at each file boundary, so it stops after the first file — far fewer
    # than all six files' worth of chunks got collected.
    files_touched = {c[2] for c in chunks}
    assert len(files_touched) < 6
    assert len(chunks) > 0


# --------------------------------------------------------------------------- overview fallback

class _StubProvider:
    available = True

    def __init__(self, result, reason=None):
        self._result, self._reason = result, reason

    def build_result(self, op, target, files, budget, project_root):
        r = {"ok": True, "op": op, "target": target, "result": self._result,
             "engine": "x", "cached": False}
        if self._reason:
            r["reason"] = self._reason
        return r


def test_overview_falls_back_to_lsp_when_repo_not_in_graph():
    graph = _StubProvider(result=None, reason="project-not-indexed")
    lsp = _StubProvider(result="## Overview\n(from lsp)")
    gw = Gateway(graph=graph, lsp=lsp, semantic=None)
    r = gw.query(op="overview", target="", engine="auto", project_root="/tmp/x")
    assert r["result"] == "## Overview\n(from lsp)"


def test_overview_falls_back_to_lsp_when_graph_unavailable():
    graph = _StubProvider(result=None, reason="engine-unavailable")
    lsp = _StubProvider(result="lsp-view")
    gw = Gateway(graph=graph, lsp=lsp, semantic=None)
    r = gw.query(op="overview", target="", engine="auto", project_root="/tmp/x")
    assert r["result"] == "lsp-view"


# --------------------------------------------------------------------------- reindex="never" wiring
#
# Both of these opt out of `conftest.py::_no_background_reindex`, because the scheduling decision
# IS what they assert. Neither does real work: each intercepts `_executor.submit`, so the pass is
# counted and never runs.
#
# The negative one has to opt in for a second reason, and it is the reason this whole suite of
# guards exists. Under the stub `maybe_reindex` does nothing, so `submitted == []` holds no matter
# what the config says — the assertion would pass while testing the stub instead of the gate. True
# about the call, false about the thing it claims to check.

def test_reindexer_honors_reindex_never(tmp_path, monkeypatch, background_reindex):
    from codeintel.reindexer import Reindexer
    (tmp_path / ".codeintel.toml").write_text('reindex = "never"\n')
    rx = Reindexer(debounce_seconds=0)
    submitted = []
    monkeypatch.setattr(rx._executor, "submit", lambda *a: submitted.append(a))
    rx.maybe_reindex(str(tmp_path))
    assert submitted == []  # reindex="never" → no background pass scheduled


def test_reindexer_schedules_by_default(tmp_path, monkeypatch, background_reindex):
    from codeintel.reindexer import Reindexer
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    rx = Reindexer(debounce_seconds=0)  # no .codeintel.toml → default "on-demand"
    submitted = []
    monkeypatch.setattr(rx._executor, "submit", lambda *a: submitted.append(a))
    rx.maybe_reindex(str(tmp_path))
    assert len(submitted) == 1  # default → a background pass is scheduled


# --------------------------------------------------------------------------- graph negative-lookup TTL

def _bare_graph(monkeypatch, run_result):
    import threading as _t

    import codeintel.providers.graph as gmod
    from codeintel.graph_backend import BackendClient
    from codeintel.graph_resolution import ProjectResolver
    p = gmod.GraphProvider.__new__(gmod.GraphProvider)  # skip _detect_backend / PATH probing
    p._backend = BackendClient.__new__(BackendClient)
    p._resolver = ProjectResolver.__new__(ProjectResolver)
    p._resolver._backend = p._backend
    p._project_cache = {}
    p._negative_until = {}
    p._project_cache_lock = _t.Lock()
    calls = {"n": 0}

    def fake_run(method, payload, timeout):
        calls["n"] += 1
        return run_result

    p._run = fake_run
    return p, calls


def test_graph_negative_lookup_reprobes_after_ttl(monkeypatch):
    p, calls = _bare_graph(monkeypatch, {"projects": []})  # never matches → None
    assert p._resolve_project("/repo/x") is None and calls["n"] == 1
    assert p._resolve_project("/repo/x") is None and calls["n"] == 1  # within TTL: no re-probe
    p._negative_until["/repo/x"] = 0.0                                # force-expire the TTL
    assert p._resolve_project("/repo/x") is None and calls["n"] == 2  # re-probed


def test_graph_positive_lookup_is_cached(monkeypatch):
    p, calls = _bare_graph(monkeypatch, {"projects": [{"name": "proj", "root_path": "/repo/x"}]})
    assert p._resolve_project("/repo/x").name == "proj" and calls["n"] == 1
    assert p._resolve_project("/repo/x").name == "proj" and calls["n"] == 1  # positive cached, no re-probe


# --------------------------------------------------------------------------- log_swallowed

def test_log_swallowed_never_raises():
    log_swallowed("unit-test", RuntimeError("boom"))  # must not raise


# --------------------------------------------------------------------------- the suite's own limits

def test_no_test_can_run_indefinitely():
    """The suite claims a per-test ceiling. This checks the claim rather than the intention.

    Wired in `pyproject.toml`, so a merge that drops the setting — or an environment where
    pytest-timeout is not installed and the key is silently ignored — leaves every test able to hang
    again. That was the shape of the stall: nothing was broken, something just never finished, and
    the only signal was a person's patience running out.
    """
    import pathlib
    import tomllib

    import pytest_timeout  # noqa: F401  — the key is inert without the plugin

    root = pathlib.Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)["tool"]["pytest"]["ini_options"]

    assert isinstance(cfg.get("timeout"), int), "no per-test timeout is configured"
    assert 0 < cfg["timeout"] <= 600, f"a {cfg['timeout']}s ceiling is not a ceiling"
    # `thread` kills the whole session, which turns one overrun into the same unexplained death
    # partway through that the ceiling exists to replace.
    assert cfg.get("timeout_method") == "signal", cfg.get("timeout_method")


def test_the_timeout_fails_one_test_and_lets_the_session_finish(tmp_path):
    """A ceiling that takes the session down with it is not an improvement on a hang."""
    import subprocess
    import sys

    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import time\n"
        "def test_slow():\n    time.sleep(30)\n"
        "def test_fast():\n    assert True\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
         "-o", "timeout=2", "-o", "timeout_method=signal", str(probe)],
        capture_output=True, text=True, timeout=120,
    )
    out = result.stdout + result.stderr
    assert "Timeout (>2.0s)" in out, out
    # The point: the run still reported, and the test after the slow one still ran.
    assert "1 failed, 1 passed" in out, out


def test_every_test_server_that_shuts_down_also_closes_its_socket():
    """`shutdown()` stops the serve loop. `server_close()` releases the listening socket.

    Sixteen teardowns called the first and none called the second, so every HTTP test leaked its
    listener and the suite reported a `ResourceWarning` per test. That is the readiness doc's P2
    item, and its name — "SQLite resource warnings" — was the misleading part: not one of the
    warnings came from sqlite. `SemanticDb` has had `close()` and a context manager throughout;
    the leak was sockets, plus one unclosed template file.

    A rule, not sixteen fixes: the next HTTP test will be written by copying an existing one, and
    that is exactly how all sixteen came to look alike.
    """
    import pathlib
    import re

    tests_dir = pathlib.Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        source = path.read_text()
        for match in re.finditer(r"^[ \t]*(\w+)\.shutdown\(\)[ \t]*$", source, re.M):
            server = match.group(1)
            line = source[:match.start()].count("\n") + 1
            # The close may come on the next line or later in the same teardown; requiring only
            # that the same name is closed somewhere in the file keeps this from dictating layout.
            if not re.search(rf"^[ \t]*{re.escape(server)}\.server_close\(\)", source, re.M):
                offenders.append(f"{path.name}:{line} — {server}.shutdown() with no "
                                 f"{server}.server_close()")
    assert not offenders, (
        "these stop a server's loop without releasing its listening socket:\n  "
        + "\n  ".join(offenders))
