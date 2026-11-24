"""The kept-open graph backend session, and finding the backend when it is not on PATH.

Driven against a fake backend: a small Python script that speaks the same MCP-over-stdio as
`codebase-memory-mcp` with no arguments, and answers `cli` the old way. Each test picks the fake's
behaviour through an environment variable. A live parity test against the real backend sits at the
end, for the `graph-contract` CI job.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import textwrap
import time

import pytest

from codeintel.engines.graph import backend, locate, session
from codeintel.engines.graph.provider import GraphProvider

# Taken at import, before the autouse fixture in conftest.py switches it off for every test.
_REAL_OWN_BIN_DIRS = locate.own_bin_dirs

_FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    mode = os.environ.get("FAKE_CBM_MODE", "ok")
    log = os.environ.get("FAKE_CBM_LOG")
    def note(s):
        if log:
            with open(log, "a") as f:
                f.write(s + "\\n")
    TEXT = "rows: 1  (cols: n)\\n  warm_answer\\ntotal: 1\\n"
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        note("cli " + sys.argv[2])
        sys.stdin.read()
        if sys.argv[2] == "list_projects":
            print(json.dumps({{"projects": [{{"name": "p", "root_path": "/r"}}]}}))
        else:
            sys.stdout.write("rows: 1  (cols: n)\\n  cold_answer\\ntotal: 1\\n")
        sys.exit(0)
    note("server")
    if mode == "die":
        sys.exit(3)
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            if mode == "slow_init":
                time.sleep(1.0)
            out = {{"jsonrpc": "2.0", "id": msg["id"], "result": {{"protocolVersion": "x"}}}}
        else:
            name = msg["params"]["name"]
            note("warm " + name)
            if mode == "slow":
                time.sleep(2.0)
            if mode == "error":
                result = {{"content": [{{"type": "text", "text": "{{\\"error\\": \\"nope\\"}}"}}],
                          "isError": True}}
            elif name == "list_projects":
                result = {{"content": [{{"type": "text", "text": json.dumps(
                    {{"projects": [{{"name": "p", "root_path": "/r"}}]}})}}]}}
            else:
                result = {{"content": [{{"type": "text", "text": TEXT}}]}}
            out = {{"jsonrpc": "2.0", "id": msg["id"], "result": result}}
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
''')


@pytest.fixture
def fake_backend(tmp_path, monkeypatch, warm_graph_backend):
    path = tmp_path / "bin" / "codebase-memory-mcp"
    path.parent.mkdir()
    path.write_text(_FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CBM_LOG", str(log))
    monkeypatch.setenv(locate.BACKEND_ENV, str(path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    session.close_all()

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return str(path), calls


def _ready(cmd: str) -> bool:
    return session.session_for(cmd).wait_ready(10.0)


# --- finding the backend ------------------------------------------------------------------------


def _executable(path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def test_found_beside_the_interpreter_when_not_on_path(tmp_path, monkeypatch):
    found = _executable(tmp_path / "codebase-memory-mcp")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(locate, "own_bin_dirs", lambda: [str(tmp_path / "nowhere"), str(tmp_path)])
    assert locate.find_backend() == found
    assert GraphProvider().available is True


def test_path_wins_over_the_interpreter_folder(tmp_path, monkeypatch):
    _executable(tmp_path / "codebase-memory-mcp")
    monkeypatch.setattr(shutil, "which", lambda name: "/on/path/codebase-memory-mcp")
    monkeypatch.setattr(locate, "own_bin_dirs", lambda: [str(tmp_path)])
    assert locate.find_backend() == "/on/path/codebase-memory-mcp"


def test_explicit_setting_wins_and_a_stale_one_is_ignored(tmp_path, monkeypatch):
    explicit = _executable(tmp_path / "elsewhere")
    monkeypatch.setattr(shutil, "which", lambda name: "/on/path/codebase-memory-mcp")
    monkeypatch.setenv(locate.BACKEND_ENV, explicit)
    assert locate.find_backend() == explicit
    monkeypatch.setenv(locate.BACKEND_ENV, str(tmp_path / "gone"))
    assert locate.find_backend() == "/on/path/codebase-memory-mcp"


def test_a_non_executable_file_is_not_the_backend(tmp_path, monkeypatch):
    (tmp_path / "codebase-memory-mcp").write_text("not a program")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(locate, "own_bin_dirs", lambda: [str(tmp_path)])
    assert locate.find_backend() is None
    assert GraphProvider().available is False


def test_own_bin_dirs_names_the_interpreter_folder():
    # The autouse fixture stubs it for every other test; the real function is checked here.
    assert os.path.dirname(sys.executable) in _REAL_OWN_BIN_DIRS()


# --- the kept-open session ----------------------------------------------------------------------


def test_first_call_is_cold_and_later_calls_are_warm(fake_backend):
    cmd, calls = fake_backend
    p = GraphProvider()
    assert p._cmd == cmd
    first = p._run("query_graph", {"project": "p", "query": "q"}, 5000)
    assert first["rows"] == [["cold_answer"]]          # never waits for the session to open
    assert _ready(cmd)
    second = p._run("query_graph", {"project": "p", "query": "q"}, 5000)
    assert second["rows"] == [["warm_answer"]]
    assert p._run("list_projects", {}, 5000) == {"projects": [{"name": "p", "root_path": "/r"}]}
    assert calls().count("server") == 1                  # one server for the whole process
    assert "warm query_graph" in calls()
    assert backend.measured_cold_start_ms() is not None


def test_writes_never_go_through_the_session(fake_backend):
    cmd, calls = fake_backend
    p = GraphProvider()
    assert _ready(cmd)
    p._run("index_repository", {"repo_path": "/r"}, 5000)
    assert "cli index_repository" in calls()
    assert "warm index_repository" not in calls()


def test_an_mcp_error_falls_back_to_the_cli(fake_backend, monkeypatch):
    cmd, calls = fake_backend
    monkeypatch.setenv("FAKE_CBM_MODE", "error")
    p = GraphProvider()
    assert _ready(cmd)
    out = p._run("query_graph", {"project": "p", "query": "q"}, 5000)
    assert out["rows"] == [["cold_answer"]]
    assert calls()[-2:] == ["warm query_graph", "cli query_graph"]


def test_a_timeout_kills_the_session_and_the_next_call_reopens_it(fake_backend, monkeypatch):
    cmd, calls = fake_backend
    monkeypatch.setenv("FAKE_CBM_MODE", "slow")
    assert _ready(cmd)
    s = session.session_for(cmd)
    assert s.call("query_graph", {}, 200) is None
    assert not s.alive()                                   # a late reply can never be misread
    monkeypatch.setenv("FAKE_CBM_MODE", "ok")
    assert s.call("query_graph", {}, 5000) is None          # reopening, not waiting
    assert s.wait_ready(10.0)
    assert s.call("query_graph", {}, 5000) == b"rows: 1  (cols: n)\n  warm_answer\ntotal: 1\n"
    assert calls().count("server") == 2


def test_a_server_that_will_not_start_is_not_retried_on_every_call(fake_backend, monkeypatch):
    cmd, calls = fake_backend
    monkeypatch.setenv("FAKE_CBM_MODE", "die")
    s = session.session_for(cmd)
    assert s.wait_ready(10.0) is False
    for _ in range(3):
        assert s.call("query_graph", {}, 1000) is None
    time.sleep(0.2)
    assert calls().count("server") == 1


def test_a_different_cache_gets_its_own_session(fake_backend, monkeypatch, tmp_path):
    cmd, _ = fake_backend
    a = session.session_for(cmd)
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(tmp_path / "other"))
    b = session.session_for(cmd)
    assert a is not b


def test_switched_off_means_no_server_at_all(fake_backend, monkeypatch):
    _, calls = fake_backend
    monkeypatch.setenv(session.WARM_ENV, "0")
    p = GraphProvider()
    for _ in range(3):
        assert p._run("query_graph", {"project": "p", "query": "q"}, 5000)["rows"] == [
            ["cold_answer"]]
    time.sleep(0.2)
    assert "server" not in calls()


def test_close_during_a_slow_start_does_not_wait_for_it(fake_backend, monkeypatch):
    cmd, _ = fake_backend
    monkeypatch.setenv("FAKE_CBM_MODE", "slow_init")
    s = session.session_for(cmd)
    s.ensure_starting()
    began = time.monotonic()
    s.close()
    assert time.monotonic() - began < 0.9
    assert s.wait_ready(3.0) is False


# --- the default budget follows the measured start ----------------------------------------------


def test_default_budget_scales_with_the_slowest_measured_start():
    assert backend.default_budget_ms() == backend.DEFAULT_BUDGET_MS
    backend.note_cold_start(1200)
    assert backend.default_budget_ms() == backend.DEFAULT_BUDGET_MS      # fast host: unchanged
    backend.note_cold_start(4400)                                       # the measured slow host
    assert backend.default_budget_ms() == 2 * 4400 + 1000
    backend.note_cold_start(60_000)
    assert backend.default_budget_ms() == 30_000                         # never past the CLI's


def test_a_query_without_a_budget_uses_the_scaled_default(monkeypatch):
    from codeintel.engines.graph import provider as provider_module

    asked = []

    def recording() -> int:
        asked.append(True)
        return 1234

    monkeypatch.setattr(provider_module, "default_budget_ms", recording)
    monkeypatch.setattr("codeintel.engines.graph.provider.shutil.which",
                        lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: None)
    p.build_result("callers", "x", [], 0, "/r")
    assert asked == [True]
    p.build_result("callers", "x", [], 7000, "/r")       # a named budget is used as given
    assert asked == [True]


# --- live parity, for the graph-contract job ----------------------------------------------------


@pytest.mark.skipif(shutil.which("codebase-memory-mcp") is None,
                    reason="codebase-memory-mcp backend not installed")
def test_live_warm_answers_match_cold_answers(warm_graph_backend):
    """The session must hand the decoder exactly what `cli` prints, on every supported dialect."""
    cold = GraphProvider()
    projects = cold._run("list_projects", {}, 30000)
    name = cold._any_project_name(projects)
    if not name:
        pytest.skip("no project indexed in this environment")
    calls = [
        ("list_projects", {}),
        ("query_graph", {"project": name, "query": "MATCH (f:Function) RETURN f.name LIMIT 5"}),
        ("search_graph", {"project": name, "name_pattern": ".*", "limit": 5}),
    ]
    os.environ[session.WARM_ENV] = "0"
    try:
        expected = [json.dumps(cold._run(m, a, 30000), sort_keys=True) for m, a in calls]
    finally:
        os.environ.pop(session.WARM_ENV, None)
    warm = GraphProvider()
    assert session.session_for(warm._cmd).wait_ready(30.0), "the backend's MCP server did not start"
    launched = []
    original = backend.BackendClient._launch

    def counting(self, *args, **kwargs):
        launched.append(args[0])
        return original(self, *args, **kwargs)

    backend.BackendClient._launch = counting  # type: ignore[method-assign]
    try:
        got = [json.dumps(warm._run(m, a, 30000), sort_keys=True) for m, a in calls]
    finally:
        backend.BackendClient._launch = original  # type: ignore[method-assign]
    assert launched == [], "a warm session was open, yet a call launched the CLI"
    assert got == expected


# --- doctor reports what a graph call costs here ------------------------------------------------


def test_doctor_reports_the_backend_its_start_time_and_the_budget(fake_backend):
    from codeintel.setup.doctor import graph_latency, render_doctor_text, run_doctor

    cmd, _ = fake_backend
    backend.note_cold_start(4400)
    latency = graph_latency()
    assert latency["backend"] == cmd
    assert latency["cold_start_ms"] == 4400
    assert latency["default_budget_ms"] == 9800
    report = run_doctor("/nonexistent-root-for-doctor")
    assert report["engines"]["graph"]["latency"]["backend"] == cmd
    text = render_doctor_text(report)
    assert f"graph backend: {cmd}" in text
    assert "default budget" in text


def test_doctor_has_no_latency_line_without_a_backend(monkeypatch):
    from codeintel.setup.doctor import render_doctor_text, run_doctor

    monkeypatch.setattr(shutil, "which", lambda name: None)
    report = run_doctor("/nonexistent-root-for-doctor")
    assert "latency" not in report["engines"]["graph"]
    assert "graph backend:" not in render_doctor_text(report)


# --- a daemon running on another cache is named, with the fix -----------------------------------

_CLASH = ("codebase-memory-mcp: CBM could not start because the active account daemon uses a "
          "different cache directory (active cache aaaa; requested cache bbbb). Close all CBM "
          "sessions and commands, then retry with one consistent CBM_CACHE_DIR.")


def test_a_cache_directory_clash_is_named_with_its_fix(monkeypatch, tmp_path):
    from codeintel.engines.graph.backend import BackendRefused

    assert backend.is_cache_dir_refusal(_CLASH)
    assert backend.is_launch_refusal(_CLASH)               # not worth a second launch either
    assert not backend.is_cache_dir_refusal("some other refusal")
    monkeypatch.setenv("CBM_CACHE_DIR", str(tmp_path / "sandbox"))
    fix = GraphProvider._refusal_fix(BackendRefused("backend-error", "x", None, 1, _CLASH))
    assert "daemon stop" in fix and "CBM_CACHE_DIR" in fix
    monkeypatch.delenv("CBM_CACHE_DIR")
    assert "default cache" in backend.cache_dir_remediation()


def test_the_graph_cache_is_where_the_backend_reads_it(monkeypatch, tmp_path):
    from codeintel.core.paths import graph_cache_dir

    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("CBM_CACHE_DIR", str(tmp_path / "real"))
    assert graph_cache_dir() == str(tmp_path / "real")
    monkeypatch.delenv("CBM_CACHE_DIR")
    assert graph_cache_dir() == str(tmp_path / "legacy")
