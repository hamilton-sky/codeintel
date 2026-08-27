"""The CLI command bodies — the half of the surface that used to be unreachable from a test.

Every subcommand's logic lived inside one ~470-line `main()`, so the only way to exercise it was to
drive argv through a subprocess and read stdout. That made the branchy parts — install's
verify/legacy/skipped reporting, reset's confirmation guard, query's warming poll — effectively
untested. Each command is now `run(args) -> int`: it returns its exit code instead of calling
sys.exit, so these tests call it directly and assert on the code.

The invariant that keeps that honest: `_MODULES` must stay in lockstep with the registered
commands, or a command dispatches to nothing.
"""
from __future__ import annotations

import argparse
import string
import subprocess
import sys
from importlib import import_module

import pytest

from codeintel.__main__ import _COMMANDS, _MODULES
from codeintel.commands import _common


def _args(defaults: dict | None = None, **overrides) -> argparse.Namespace:
    """A parsed-args stand-in. Overrides win, so a helper can supply the full flag set a command
    reads and a test can name only the one flag it cares about."""
    return argparse.Namespace(**{**(defaults or {}), **overrides})


# --------------------------------------------------------------------------- dispatch wiring

def test_every_command_dispatches_to_a_module_exposing_run():
    """The drift this catches: a command added to the help table and argparse but never given a
    module, or a module renamed out from under the dispatch dict."""
    assert set(_MODULES) == set(_COMMANDS)
    for command, module_name in _MODULES.items():
        module = import_module(f"codeintel.commands.{module_name}")
        assert callable(getattr(module, "run", None)), f"{command} -> {module_name}.run missing"


def test_importing_the_cli_does_not_pull_in_any_engine():
    """Dispatch is lazy so `codeintel serve` does not pay for the semantic engine's import (nor
    serve-http for the graph's). A module-scope engine import anywhere on the path from
    `__main__` gives that back for every command at once — measured in a clean interpreter,
    because this test session has already imported most of the package."""
    probe = ("import sys, codeintel.__main__; "
             "print([m for m in ('codeintel.indexer', 'codeintel.semantic_db', "
             "'codeintel.providers.graph', 'codeintel.server', 'codeintel.http_server') "
             "if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# --------------------------------------------------------------------------- shared plumbing

def test_resolve_root_prefers_the_argument_then_falls_back_to_cwd(tmp_path, monkeypatch):
    assert _common.resolve_root(_args(project_root=str(tmp_path))) == str(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Both "flag absent" and "flag present but None" mean cwd.
    assert _common.resolve_root(_args()) == str(tmp_path)
    assert _common.resolve_root(_args(project_root=None)) == str(tmp_path)


def test_emit_switches_between_json_and_the_text_rendering(capsys):
    report = {"summary": {"healthy": True}}
    _common.emit(report, as_json=True, render=lambda r: "TEXT")
    assert '"healthy": true' in capsys.readouterr().out

    _common.emit(report, as_json=False, render=lambda r: "TEXT")
    assert capsys.readouterr().out.strip() == "TEXT"


def test_never_raise_degrades_with_a_message_and_the_declared_exit_code(capsys):
    @_common.never_raise("boom failed: {exc}", code=3)
    def blows_up(args):
        raise ValueError("no backend")

    assert blows_up(None) == 3
    assert "boom failed: no backend" in capsys.readouterr().out


def test_never_raise_can_report_on_stderr(capsys):
    @_common.never_raise("serve-http failed: {exc}", code=1, stderr=True)
    def blows_up(args):
        raise OSError("port in use")

    assert blows_up(None) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "serve-http failed: port in use" in captured.err


@pytest.mark.parametrize("exc", [SystemExit(2), KeyboardInterrupt()])
def test_never_raise_lets_baseexception_through(exc):
    """SystemExit is how serve-http's non-loopback guard refuses to bind, and KeyboardInterrupt is
    a Ctrl-C. Swallowing either would turn a hard stop into a silent exit 0."""
    @_common.never_raise("swallowed: {exc}")
    def raises(args):
        raise exc

    with pytest.raises(type(exc)):
        raises(None)


# --------------------------------------------------------------------------- install

class _FakeInstaller:
    def __init__(self, results, skipped=()):
        self.results = results
        self.skipped = list(skipped)
        self.seen: dict = {}

    def register_detected(self, *, verify, absolute):
        self.seen = {"verify": verify, "absolute": absolute}
        return self.results, self.skipped

    def register_all(self, *, verify, absolute):
        self.seen = {"verify": verify, "absolute": absolute}
        return self.results

    def register_many(self, agents, *, verify, absolute):
        self.seen = {"verify": verify, "absolute": absolute, "agents": agents}
        return self.results


def _install(monkeypatch, results, skipped=(), **overrides):
    fake = _FakeInstaller(results, skipped)
    monkeypatch.setattr("codeintel.installer.Installer", lambda: fake)
    args = _args({"agent": "auto", "no_verify": False, "relative_command": False}, **overrides)
    return import_module("codeintel.commands.install").run(args), fake


def test_install_reports_each_agent_and_succeeds_when_any_registered(monkeypatch, capsys):
    code, _ = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/.claude.json", "action": "registered"},
        {"agent": "codex", "path": "/c/codex.toml", "action": "failed", "reason": "unwritable"},
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "v claude: registered at /c/.claude.json" in out
    assert "x codex: failed — unwritable" in out


def test_install_fails_when_no_agent_registered(monkeypatch, capsys):
    code, _ = _install(monkeypatch, [
        {"agent": "codex", "path": "/c/codex.toml", "action": "failed", "reason": "unwritable"},
    ])
    assert code == 1


def test_install_auto_with_nothing_detected_names_what_it_looked_for(monkeypatch, capsys):
    """The empty-handed case has to say "you don't have these" rather than stay silent, or it
    reads as "codeintel doesn't support your agent"."""
    code, _ = _install(monkeypatch, [], skipped=["claude", "codex", "zed"])
    out = capsys.readouterr().out
    assert code == 1
    assert "claude, codex, zed" in out
    assert "--agent <name>" in out


def test_install_failed_verification_overrides_a_written_config(monkeypatch, capsys):
    """A written config proves nothing about whether the host can launch the server. Registered +
    unverified must NOT exit 0, or a broken install looks clean in CI."""
    code, _ = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/.claude.json", "action": "registered",
         "verified": {"ok": False, "detail": "handshake timed out"}},
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "x NOT verified: handshake timed out" in out
    assert "will not be able to use it" in out


def test_install_successful_verification_is_reported(monkeypatch, capsys):
    code, _ = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/.claude.json", "action": "already",
         "verified": {"ok": True, "detail": "4 tools"}},
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "~ claude: already registered" in out
    assert "v verified: 4 tools" in out


def test_install_reports_each_stale_legacy_path_once(monkeypatch, capsys):
    code, _ = _install(monkeypatch, [
        {"agent": "claude", "path": "/c/a.json", "action": "registered", "legacy": "/c/old.json"},
        {"agent": "zed", "path": "/c/b.json", "action": "registered", "legacy": "/c/old.json"},
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("stale entry: /c/old.json") == 1


def test_install_flags_invert_into_the_installer_call(monkeypatch):
    """--no-verify and --relative-command are both negations; an inversion slip here silently
    disables verification or registers a bare name a GUI-launched agent cannot resolve."""
    _, fake = _install(monkeypatch, [{"agent": "claude", "path": "/c", "action": "registered"}],
                       no_verify=True, relative_command=True)
    assert fake.seen == {"verify": False, "absolute": False}

    _, fake = _install(monkeypatch, [{"agent": "claude", "path": "/c", "action": "registered"}])
    assert fake.seen == {"verify": True, "absolute": True}


def test_install_named_agent_registers_only_that_agent(monkeypatch, capsys):
    _, fake = _install(monkeypatch, [{"agent": "zed", "path": "/c", "action": "registered"}],
                       agent="zed")
    assert fake.seen["agents"] == ["zed"]
    assert "skipped" not in capsys.readouterr().out


def test_install_degrades_instead_of_tracebacking(monkeypatch, capsys):
    class _Boom:
        def register_detected(self, **kw):
            raise RuntimeError("home dir unresolvable")

    monkeypatch.setattr("codeintel.installer.Installer", _Boom)
    code = import_module("codeintel.commands.install").run(
        _args(agent="auto", no_verify=False, relative_command=False))
    assert code == 1
    assert "install failed: home dir unresolvable" in capsys.readouterr().out


# --------------------------------------------------------------------------- reset

def _reset_args(**kw):
    return _args({"project_root": None, "all": False, "yes": False, "json": False}, **kw)


def test_reset_refuses_without_yes_in_a_non_interactive_shell(monkeypatch, capsys):
    """Without a TTY there is nobody to answer the prompt, and input() would raise or read
    whatever is piped in — either way the wrong index gets cleared."""
    applied = []
    monkeypatch.setattr("codeintel.reset.run_reset",
                        lambda root, **kw: applied.append(kw.get("apply")) or {"count": 7})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert import_module("codeintel.commands.reset").run(_reset_args()) == 1
    assert True not in applied          # preview only; never applied
    assert "refusing to reset without --yes" in capsys.readouterr().out


def test_reset_aborts_on_a_declined_prompt(monkeypatch, capsys):
    applied = []
    monkeypatch.setattr("codeintel.reset.run_reset",
                        lambda root, **kw: applied.append(kw.get("apply")) or {"count": 7})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert import_module("codeintel.commands.reset").run(_reset_args()) == 0
    assert True not in applied
    assert "aborted" in capsys.readouterr().out


def test_reset_prompt_names_the_target_and_the_entry_count(monkeypatch):
    prompts = []
    monkeypatch.setattr("codeintel.reset.run_reset", lambda root, **kw: {"count": 7})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    import_module("codeintel.commands.reset").run(_reset_args(all=True))
    assert "ALL projects" in prompts[0] and "7 entries" in prompts[0]


def test_reset_applies_on_confirmation(monkeypatch, capsys):
    applied = []

    def _run_reset(root, **kw):
        applied.append(kw.get("apply"))
        return {"count": 7, "detail": "cleared 7 entries"}

    monkeypatch.setattr("codeintel.reset.run_reset", _run_reset)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    assert import_module("codeintel.commands.reset").run(_reset_args()) == 0
    assert applied == [False, True]     # preview, then the real thing
    assert "cleared 7 entries" in capsys.readouterr().out


def test_reset_with_yes_skips_the_prompt_entirely(monkeypatch):
    monkeypatch.setattr("codeintel.reset.run_reset",
                        lambda root, **kw: {"count": 7, "detail": "cleared"})
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("prompted despite --yes"))

    assert import_module("codeintel.commands.reset").run(_reset_args(yes=True)) == 0


# --------------------------------------------------------------------------- query

class _Gateway:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def query(self, **kw):
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def _query_args(**kw):
    return _args({"op": "callers", "target": "main", "engine": "auto", "project_root": None,
                  "json": False}, **kw)


def test_query_prints_the_result(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.server._build_gateway",
                        lambda **_kw: _Gateway([{"result": "## Callers of main (1)"}]))
    assert import_module("codeintel.commands.query").run(_query_args()) == 0
    assert "## Callers of main (1)" in capsys.readouterr().out


def test_query_waits_out_the_lsp_warming_window(monkeypatch, capsys):
    """A one-shot CLI process dies with its subprocess, so returning on the first `warming` would
    mean the LSP engine never answers a single CLI query."""
    query = import_module("codeintel.commands.query")
    gw = _Gateway([
        {"result": None, "reason": "warming"},
        {"result": None, "reason": "warming"},
        {"result": "def main() -> None"},
    ])
    monkeypatch.setattr("codeintel.server._build_gateway", lambda **_kw: gw)
    monkeypatch.setattr(query.time, "sleep", lambda _s: None)

    assert query.run(_query_args()) == 0
    assert gw.calls == 3
    assert "def main() -> None" in capsys.readouterr().out


def test_query_gives_up_when_warming_never_finishes(monkeypatch, capsys):
    """Bounded wait: the loop must exit on the deadline rather than spin forever."""
    query = import_module("codeintel.commands.query")
    clock = iter([0.0] + [float(i) for i in range(200)])
    monkeypatch.setattr("codeintel.server._build_gateway",
                        lambda **_kw: _Gateway([{"result": None, "reason": "warming"}]))
    monkeypatch.setattr(query.time, "sleep", lambda _s: None)
    monkeypatch.setattr(query.time, "monotonic", lambda: next(clock))

    assert query.run(_query_args()) == 0
    assert "No result (reason: warming)" in capsys.readouterr().out


def test_query_surfaces_the_reason_and_hint_when_empty(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.server._build_gateway", lambda **_kw: _Gateway([
        {"result": None, "reason": "not_indexed", "hint": "run `codeintel index`"}]))

    assert import_module("codeintel.commands.query").run(_query_args()) == 0
    captured = capsys.readouterr()
    assert "No result (reason: not_indexed)" in captured.out
    assert "hint: run `codeintel index`" in captured.err


def test_query_auto_engine_becomes_no_engine_preference(monkeypatch):
    """`--engine auto` is the CLI's word for "let the gateway choose"; passing it through
    literally would ask the gateway for an engine named "auto"."""
    seen = {}

    class _Recorder:
        def query(self, **kw):
            seen.update(kw)
            return {"result": "ok"}

    monkeypatch.setattr("codeintel.server._build_gateway", lambda **_kw: _Recorder())
    import_module("codeintel.commands.query").run(_query_args())
    assert seen["engine"] is None

    import_module("codeintel.commands.query").run(_query_args(engine="lsp"))
    assert seen["engine"] == "lsp"


def test_query_never_raises(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.server._build_gateway",
                        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no graph backend")))
    assert import_module("codeintel.commands.query").run(_query_args()) == 0
    assert "No result (reason: no graph backend)" in capsys.readouterr().out


# --------------------------------------------------------------------------- doctor / setup

@pytest.mark.parametrize("healthy,expected", [(True, 0), (False, 1)])
def test_doctor_exit_code_gates_on_health(monkeypatch, capsys, healthy, expected):
    """CI gates on this exit code; a doctor that always exits 0 is a broken gate."""
    monkeypatch.setattr("codeintel.doctor.run_doctor",
                        lambda root, deep=False: {"summary": {"healthy": healthy}})
    monkeypatch.setattr("codeintel.doctor.render_doctor_text", lambda r: "TABLE")

    args = _args(project_root=None, deep=False, json=False)
    assert import_module("codeintel.commands.doctor").run(args) == expected
    assert "TABLE" in capsys.readouterr().out


def test_doctor_json_emits_the_structured_report(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.doctor.run_doctor",
                        lambda root, deep=False: {"summary": {"healthy": True}})
    args = _args(project_root=None, deep=False, json=True)
    import_module("codeintel.commands.doctor").run(args)
    assert '"healthy": true' in capsys.readouterr().out


def test_doctor_degrades_instead_of_tracebacking(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.doctor.run_doctor",
                        lambda root, deep=False: (_ for _ in ()).throw(RuntimeError("nope")))
    args = _args(project_root=None, deep=False, json=False)
    assert import_module("codeintel.commands.doctor").run(args) == 0
    assert "doctor unavailable: nope" in capsys.readouterr().out


def _setup_args(**kw):
    return _args({"project_root": None, "all_steps": False, "install_uv": False,
                  "install_deps": False, "index": False, "warm": False, "json": False}, **kw)


def test_setup_all_implies_every_automatable_step(monkeypatch, capsys):
    seen = {}

    def _run_setup(root, **kw):
        seen.update(kw)
        return {"doctor": {"summary": {"healthy": True}}}

    monkeypatch.setattr("codeintel.onboarding.run_setup", _run_setup)
    monkeypatch.setattr("codeintel.onboarding.render_setup_text", lambda r: "STEPS")

    assert import_module("codeintel.commands.setup").run(_setup_args(all_steps=True)) == 0
    assert seen == {"install_uv": True, "install_deps": True, "do_index": True, "warm_lsp": True}


def test_setup_without_all_runs_only_the_named_steps(monkeypatch):
    seen = {}
    monkeypatch.setattr("codeintel.onboarding.run_setup",
                        lambda root, **kw: seen.update(kw) or {"doctor": {}})
    monkeypatch.setattr("codeintel.onboarding.render_setup_text", lambda r: "STEPS")

    import_module("codeintel.commands.setup").run(_setup_args(index=True))
    assert seen == {"install_uv": False, "install_deps": False, "do_index": True,
                    "warm_lsp": False}


@pytest.mark.parametrize("report,expected", [
    ({"doctor": {"summary": {"healthy": True}}}, 0),
    ({"doctor": {"summary": {"healthy": False}}}, 1),
    ({}, 1),                                            # no doctor block == not proven healthy
])
def test_setup_exit_code_gates_on_the_nested_doctor_verdict(monkeypatch, report, expected):
    monkeypatch.setattr("codeintel.onboarding.run_setup", lambda root, **kw: report)
    monkeypatch.setattr("codeintel.onboarding.render_setup_text", lambda r: "STEPS")
    assert import_module("codeintel.commands.setup").run(_setup_args()) == expected


# --------------------------------------------------------------------------- graph / map

def _graph_args(**kw):
    return _args({"project_root": None, "html": False, "out": None, "limit": 220}, **kw)


def test_graph_defaults_to_json_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.grapher.build_graph_payload",
                        lambda root, limit=220: {"nodes": [{"id": "a"}], "edges": []})
    assert import_module("codeintel.commands.graph").run(_graph_args()) == 0
    assert '"nodes"' in capsys.readouterr().out


def test_graph_html_writes_the_viewer_and_reports_its_size(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("codeintel.grapher.build_graph_payload",
                        lambda root, limit=220: {"nodes": [{"id": "a"}], "edges": [{"s": "a"}]})
    monkeypatch.setattr("codeintel.grapher.render_html", lambda payload: "<html></html>")
    out = tmp_path / "g.html"

    assert import_module("codeintel.commands.graph").run(_graph_args(html=True, out=str(out))) == 0
    assert out.read_text() == "<html></html>"
    assert "(1 nodes, 1 edges)" in capsys.readouterr().out


def test_graph_html_explains_an_empty_graph(monkeypatch, capsys, tmp_path):
    """An empty viewer with no explanation reads as "my repo has no calls" rather than "the graph
    backend never answered"."""
    monkeypatch.setattr("codeintel.grapher.build_graph_payload",
                        lambda root, limit=220: {"nodes": [], "edges": [], "reason": "not_indexed"})
    monkeypatch.setattr("codeintel.grapher.render_html", lambda payload: "<html></html>")

    args = _graph_args(html=True, out=str(tmp_path / "g.html"))
    import_module("codeintel.commands.graph").run(args)
    out = capsys.readouterr().out
    assert "graph empty: not_indexed" in out and "codeintel doctor" in out


def test_graph_degrades_instead_of_tracebacking(monkeypatch, capsys):
    """Degrades with a message — but exits NON-ZERO. This command's job is to write a file, and
    exiting 0 having failed to write it reports success to any make/CI step gating on $?."""
    monkeypatch.setattr("codeintel.grapher.build_graph_payload",
                        lambda root, limit=220: (_ for _ in ()).throw(RuntimeError("boom")))
    assert import_module("codeintel.commands.graph").run(_graph_args()) == 1
    assert "graph failed: boom" in capsys.readouterr().out


class _Gen:
    def __init__(self, wrote=True):
        self.wrote = wrote

    def generate(self, root, budget_bytes=None):
        return "# CODE_INTEL"

    def write(self, root, content):
        return ("/p/CODE_INTEL.md", self.wrote)


def test_map_keeps_the_existing_file_when_the_new_map_is_a_stub(monkeypatch, capsys):
    """Overwriting a good committed map with a stub because the graph happened to be down is the
    destructive failure mode here; the CLI has to say why it declined."""
    monkeypatch.setattr("codeintel.mapper.MapGenerator", lambda provider: _Gen(wrote=False))
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: object())

    args = _args(project_root=None, inject=False, budget=32768)
    assert import_module("codeintel.commands.map").run(args) == 0
    out = capsys.readouterr().out
    assert "Kept existing /p/CODE_INTEL.md" in out and "codeintel index" in out


def test_map_survives_an_unavailable_graph_provider(monkeypatch, capsys):
    """A missing graph backend degrades to a stub map, not a traceback — MapGenerator takes None."""
    seen = {}
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider",
                        lambda: (_ for _ in ()).throw(RuntimeError("no backend")))
    monkeypatch.setattr("codeintel.mapper.MapGenerator",
                        lambda provider: seen.update(provider=provider) or _Gen())

    args = _args(project_root=None, inject=False, budget=32768)
    assert import_module("codeintel.commands.map").run(args) == 0
    assert seen["provider"] is None
    assert "Wrote /p/CODE_INTEL.md" in capsys.readouterr().out


def test_map_inject_reports_what_it_did_to_which_file(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.mapper.MapGenerator", lambda provider: _Gen())
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: object())
    monkeypatch.setattr("codeintel.injector.Injector",
                        lambda: type("I", (), {"inject": lambda self, root: ("CLAUDE.md",
                                                                             "updated")})())

    args = _args(project_root=None, inject=True, budget=32768)
    import_module("codeintel.commands.map").run(args)
    assert "Inject: updated block in CLAUDE.md" in capsys.readouterr().out


def test_map_degrades_instead_of_tracebacking(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: object())
    monkeypatch.setattr("codeintel.mapper.MapGenerator",
                        lambda provider: (_ for _ in ()).throw(RuntimeError("boom")))

    args = _args(project_root=None, inject=False, budget=32768)
    assert import_module("codeintel.commands.map").run(args) == 1, "a failed write must not exit 0"
    assert "map failed: boom" in capsys.readouterr().out


# --------------------------------------------------------------------------- serve-http / status

def test_serve_http_prefers_the_flag_over_the_environment(monkeypatch):
    seen = {}
    monkeypatch.setenv("CODEINTEL_HTTP_TOKEN", "from-env")
    monkeypatch.setattr("codeintel.http_server.run", lambda **kw: seen.update(kw))

    args = _args(host="127.0.0.1", port=8766, allow_remote=False, token="from-flag")
    assert import_module("codeintel.commands.serve_http").run(args) == 0
    assert seen["token"] == "from-flag"


def test_serve_http_falls_back_to_the_environment_token(monkeypatch):
    seen = {}
    monkeypatch.setenv("CODEINTEL_HTTP_TOKEN", "from-env")
    monkeypatch.setattr("codeintel.http_server.run", lambda **kw: seen.update(kw))

    args = _args(host="0.0.0.0", port=9000, allow_remote=True, token=None)
    import_module("codeintel.commands.serve_http").run(args)
    assert seen == {"host": "0.0.0.0", "port": 9000, "allow_remote": True, "token": "from-env"}


def test_serve_http_reports_a_startup_failure_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.http_server.run",
                        lambda **kw: (_ for _ in ()).throw(OSError("address in use")))
    args = _args(host="127.0.0.1", port=8766, allow_remote=False, token=None)

    assert import_module("codeintel.commands.serve_http").run(args) == 1
    assert "serve-http failed: address in use" in capsys.readouterr().err


def test_serve_http_treats_ctrl_c_as_a_clean_shutdown(monkeypatch):
    monkeypatch.setattr("codeintel.http_server.run",
                        lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    args = _args(host="127.0.0.1", port=8766, allow_remote=False, token=None)
    assert import_module("codeintel.commands.serve_http").run(args) == 0


def test_serve_http_lets_the_loopback_guard_exit(monkeypatch):
    """The guard refuses a non-loopback bind with SystemExit; catching it would turn a refusal to
    expose the endpoint into a silent success."""
    monkeypatch.setattr("codeintel.http_server.run",
                        lambda **kw: (_ for _ in ()).throw(SystemExit(2)))
    args = _args(host="0.0.0.0", port=8766, allow_remote=False, token=None)
    with pytest.raises(SystemExit):
        import_module("codeintel.commands.serve_http").run(args)


def test_status_distinguishes_installed_from_ready(monkeypatch, capsys):
    """"available" used to mean "a binary is on PATH", which is neither runnable nor usable on
    THIS repo. Each engine has to say which of the three it is."""
    monkeypatch.setattr("codeintel.server.code_status_handler", lambda args: {
        "readiness": {
            "graph": {"status": "ok", "detail": "resolved project"},
            "lsp": {"status": "warn", "detail": "boot not verified"},
            "semantic": {"status": "fail", "detail": "no index"},
        },
        "healthy": False,
    })
    assert import_module("codeintel.commands.status").run(_args(project_root=None)) == 0
    out = capsys.readouterr().out
    assert "graph      ready" in out
    assert "lsp        installed (not verified)" in out
    assert "semantic   unavailable" in out
    assert "codeintel doctor" in out          # the way forward when unhealthy


def test_status_degrades_instead_of_tracebacking(monkeypatch, capsys):
    monkeypatch.setattr("codeintel.server.code_status_handler",
                        lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert import_module("codeintel.commands.status").run(_args(project_root=None)) == 0
    assert "Status unavailable: boom" in capsys.readouterr().out


# --------------------------------------------------------------------------- index

class _FakeDb:
    def __init__(self, path):
        self.path = path
        self.closed = False

    def init(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _stub_index(monkeypatch, tmp_path, *, count=3, indexer_exc=None, has_graph_backend=False):
    """Stand in for the whole semantic pass. Returns the recorder the assertions read."""
    seen: dict = {"db": None, "reindexed": False, "mapped": False}

    def _make_db(path):
        seen["db"] = _FakeDb(path)
        return seen["db"]

    class _FakeIndexer:
        def __init__(self, db, **kwargs):
            seen["kwargs"] = kwargs

        def index(self, root):
            if indexer_exc:
                raise indexer_exc
            return count

    class _FakeReindexer:
        def _graph_reindex(self, root):
            seen["reindexed"] = True

    class _FakeGen:
        def __init__(self, provider):
            pass

        def generate(self, root, budget_bytes=None):
            return "# map"

        def write(self, root, content):
            seen["mapped"] = True
            return ("/p/CODE_INTEL.md", True)

    monkeypatch.setattr("codeintel.config.load_config", lambda root: {"model": "m", "window": 20})
    monkeypatch.setattr("codeintel.semantic_db.default_db_path",
                        lambda model: str(tmp_path / "db" / "semantic.db"))
    monkeypatch.setattr("codeintel.semantic_db.SemanticDb", _make_db)
    monkeypatch.setattr("codeintel.indexer.Indexer", _FakeIndexer)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/x" if has_graph_backend else None)
    monkeypatch.setattr("codeintel.reindexer.Reindexer", _FakeReindexer)
    monkeypatch.setattr("codeintel.mapper.MapGenerator", _FakeGen)
    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", lambda: object())
    return seen


def test_index_reports_the_chunk_count(monkeypatch, tmp_path, capsys):
    _stub_index(monkeypatch, tmp_path, count=42)
    assert import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path))) == 0
    assert "Indexed 42 chunks" in capsys.readouterr().out


def test_index_prints_a_header_naming_the_model_and_strategy(monkeypatch, tmp_path, capsys):
    _stub_index(monkeypatch, tmp_path, count=5)
    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path)))
    out = capsys.readouterr().out
    assert "codeintel index" in out and "def-aligned chunks" in out


def test_index_quiet_suppresses_the_header_but_keeps_the_result_line(monkeypatch, tmp_path, capsys):
    _stub_index(monkeypatch, tmp_path, count=5)
    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path), quiet=True))
    out = capsys.readouterr().out
    assert "codeintel index" not in out          # header gone
    assert "Indexed 5 chunks" in out             # result line stays


def test_index_says_so_when_nothing_changed(monkeypatch, tmp_path, capsys):
    """Incremental runs are the common case; "Indexed 0 chunks" reads like a failure."""
    _stub_index(monkeypatch, tmp_path, count=0)
    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path)))
    assert "Nothing new to index" in capsys.readouterr().out


def test_index_closes_the_db_even_when_indexing_raises(monkeypatch, tmp_path, capsys):
    """A leaked sqlite handle on the failure path is how the next run inherits a locked DB."""
    seen = _stub_index(monkeypatch, tmp_path, indexer_exc=RuntimeError("model download failed"))
    # Exits non-zero: the index did not happen, and a script must be able to tell.
    assert import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path))) == 1
    assert seen["db"].closed is True
    assert "index failed: model download failed" in capsys.readouterr().out


def test_index_still_refreshes_the_map_after_a_semantic_failure(monkeypatch, tmp_path):
    """The graph and map passes are independent of the semantic one — a missing embedding model
    must not also cost you an up-to-date CODE_INTEL.md."""
    seen = _stub_index(monkeypatch, tmp_path, indexer_exc=RuntimeError("boom"))
    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path)))
    assert seen["mapped"] is True


def test_index_skips_the_graph_reindex_when_the_backend_is_absent(monkeypatch, tmp_path):
    seen = _stub_index(monkeypatch, tmp_path, has_graph_backend=False)
    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path)))
    assert seen["reindexed"] is False


def test_index_reindexes_the_graph_when_the_backend_is_present(monkeypatch, tmp_path):
    seen = _stub_index(monkeypatch, tmp_path, has_graph_backend=True)
    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path)))
    assert seen["reindexed"] is True


def test_index_survives_a_failing_graph_reindex_and_map_refresh(monkeypatch, tmp_path, capsys):
    """Both are best-effort: they run after the index is already committed, so their failure must
    not turn a successful index into a non-zero exit."""
    _stub_index(monkeypatch, tmp_path, has_graph_backend=True)
    monkeypatch.setattr("codeintel.reindexer.Reindexer",
                        lambda: (_ for _ in ()).throw(RuntimeError("graph down")))
    monkeypatch.setattr("codeintel.mapper.MapGenerator",
                        lambda provider: (_ for _ in ()).throw(RuntimeError("map down")))

    assert import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "Indexed 3 chunks" in out
    assert "graph down" not in out and "map down" not in out


def test_index_passes_the_configured_chunking_knobs_through(monkeypatch, tmp_path):
    """`index` is the only caller that builds an Indexer from config; a dropped key here silently
    reverts a user's tuning to the defaults."""
    seen = _stub_index(monkeypatch, tmp_path)
    monkeypatch.setattr("codeintel.config.load_config", lambda root: {
        "model": "custom/model", "window": 5, "stride": 2, "max_chunks": 9,
        "max_total_chunks": 99, "chunk_strategy": "line"})

    import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path)))
    knobs = {k: seen["kwargs"][k] for k in
             ("model_name", "window", "stride", "max_chunks", "max_total_chunks", "chunk_strategy")}
    assert knobs == {"model_name": "custom/model", "window": 5, "stride": 2,
                     "max_chunks": 9, "max_total_chunks": 99, "chunk_strategy": "line"}
    assert "progress" in seen["kwargs"]      # the live-progress sink is wired through too


# --------------------------------------------------------------------------- serve / gen-token

def test_serve_starts_the_stdio_server(monkeypatch):
    started = []
    monkeypatch.setattr("codeintel.server.run", lambda: started.append(True))
    assert import_module("codeintel.commands.serve").run(_args()) == 0
    assert started == [True]


def test_gen_token_prints_a_fresh_urlsafe_token(capsys):
    """Reused output would mean two deployments sharing a bearer token."""
    gen_token = import_module("codeintel.commands.gen_token")
    assert gen_token.run(_args()) == 0
    assert gen_token.run(_args()) == 0
    first, second = capsys.readouterr().out.split()

    assert first != second
    assert len(first) >= 40                                  # 32 random bytes, base64url-encoded
    assert set(first) <= set(string.ascii_letters + string.digits + "-_")


def test_index_rejects_a_project_root_that_is_not_a_directory(tmp_path, capsys):
    """`codeintel index /typo/path` walked nothing, found nothing, and printed "Nothing new to
    index" at exit 0 — indistinguishable from a correct incremental run, which is the worst
    possible response to a mistyped path in a script."""
    missing = str(tmp_path / "does-not-exist")
    assert import_module("codeintel.commands.index").run(_args(project_root=missing)) == 1
    assert "not a directory" in capsys.readouterr().out


def test_index_reports_an_unrecoverable_indexer_failure(monkeypatch, tmp_path, capsys):
    """Indexer.index() returns -1 for an unrecoverable failure; a `> 0` test sent that into the
    "Nothing new to index" branch, so total failure read as a clean no-op."""
    _stub_index(monkeypatch, tmp_path, count=-1)
    assert import_module("codeintel.commands.index").run(_args(project_root=str(tmp_path))) == 1
    out = capsys.readouterr().out
    assert "index failed" in out and "Nothing new to index" not in out


# --------------------------------------------------------------------------- root validation

@pytest.mark.parametrize("command,args_", [
    ("setup", {"project_root": None, "all_steps": False, "install_uv": False,
               "install_deps": False, "index": False, "warm": False, "json": False}),
    ("status", {"project_root": None}),
    ("map", {"project_root": None, "inject": False, "budget": 32768}),
    ("graph", {"project_root": None, "html": False, "out": None, "limit": 220}),
    ("c4", {"project_root": None, "out": None, "depth": None, "scope": None,
            "include_tests": False, "no_index": False, "json": False}),
])
def test_a_nonexistent_project_root_fails_loudly(command, args_, tmp_path, capsys):
    """A mistyped path produced confident, well-formed output about a directory that does not
    exist — `setup /typo` rendered a full three-engine health table for it. In a script that is
    indistinguishable from success, which is the worst possible response to a typo."""
    args_ = {**args_, "project_root": str(tmp_path / "does-not-exist")}
    assert import_module(f"codeintel.commands.{command}").run(_args(args_)) == 1
    assert "not a directory" in capsys.readouterr().out


def test_query_json_emits_the_whole_envelope(monkeypatch, capsys):
    """The README asks a bug reporter to include `engine`, `cached` and `reindexing` when a result
    looks wrong. The CLI printed only the rendered result or the bare reason, so those fields were
    unreachable — documentation describing a workflow the tool did not support."""
    import json as _json

    monkeypatch.setattr("codeintel.server._build_gateway", lambda **_kw: _Gateway([{
        "ok": True, "op": "callers", "target": "f", "result": "## Callers",
        "engine": "graph", "cached": True, "reindexing": True}]))

    assert import_module("codeintel.commands.query").run(_query_args(json=True)) == 0
    payload = _json.loads(capsys.readouterr().out)

    assert payload["engine"] == "graph"
    assert payload["cached"] is True
    assert payload["reindexing"] is True


def test_query_without_json_still_prints_only_the_answer(monkeypatch, capsys):
    """The default stays human-facing — an agent host reads the envelope over MCP, not the CLI."""
    monkeypatch.setattr("codeintel.server._build_gateway",
                        lambda **_kw: _Gateway([{"result": "## Callers", "engine": "graph"}]))

    import_module("codeintel.commands.query").run(_query_args(json=False))
    out = capsys.readouterr().out
    assert out.strip() == "## Callers"
    assert "engine" not in out


def test_query_json_stays_parseable_when_the_query_itself_fails(monkeypatch, capsys):
    """`--json` promises parseable stdout. The shared never-raise handler printed a plain-text
    line there, handing a `| jq` consumer a parse error alongside exit 0 — the worst combination,
    because the pipeline reports success."""
    import json as _json

    monkeypatch.setattr("codeintel.server._build_gateway",
                        lambda **_kw: (_ for _ in ()).throw(RuntimeError("backend exploded")))

    assert import_module("codeintel.commands.query").run(_query_args(json=True)) == 0
    payload = _json.loads(capsys.readouterr().out)      # must not raise
    assert payload["result"] is None
    assert "backend exploded" in payload["reason"]
