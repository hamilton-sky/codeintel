"""`codeintel prompt` builds a paste-to-your-agent setup prompt, tailored to a doctor report.

The value over a static README block is that it emits ONLY the outstanding steps, so these tests pin
that tailoring: a satisfied engine is never named as something to install, and a fully-healthy +
registered machine reduces to "just restart me"."""
from __future__ import annotations

from codeintel.agent_prompt import build_prompt, resolve_agent


def _report(*, graph="ok", lsp="ok", semantic="ok", registered_agent=None, healthy=True) -> dict:
    return {
        "summary": {"healthy": healthy, "ready": 3, "total": 3},
        "engines": {"graph": {"status": graph}, "lsp": {"status": lsp}, "semantic": {"status": semantic}},
        "registrations": ([{"agent": registered_agent, "runnable": True}] if registered_agent else []),
    }


def test_all_healthy_and_registered_reduces_to_restart():
    p = build_prompt("/repo", "claude", _report(registered_agent="claude"))
    assert "already installed and healthy" in p
    assert "restart" in p.lower()
    assert "pip install" not in p          # nothing to install
    assert "setup --all" not in p          # nothing to set up
    assert "/repo" in p                    # names the project it is about


def test_only_the_missing_engine_is_named_to_install():
    # graph missing, semantic ok → the pin appears, but the CLI is NOT reinstalled
    p = build_prompt("/repo", "claude", _report(graph="warn", registered_agent="claude", healthy=False))
    assert "codebase-memory-mcp==0.9.*" in p
    assert "codecortex" not in p
    assert "codeintel setup --all /repo" in p      # a missing engine still needs the index/uv step
    assert "install --agent" not in p              # already registered → no register step


def test_fresh_emits_the_full_sequence_regardless_of_local_state():
    # a healthy report is passed, but fresh=True must ignore it (a template for a clean machine)
    p = build_prompt(".", "claude", _report(registered_agent="claude"), fresh=True)
    assert "pip install codecortex 'codebase-memory-mcp==0.9.*'" in p
    assert "codeintel setup --all ." in p
    assert "codeintel install --agent claude" in p
    assert "doctor --deep ." in p
    assert "ready 3/3, healthy" in p


def test_agent_is_named_or_omitted_never_guessed():
    named = build_prompt(".", "codex", {}, fresh=True)
    assert "codeintel install --agent codex" in named
    generic = build_prompt(".", "", {}, fresh=True)     # no host detected
    assert "--agent" not in generic
    assert "codeintel install`" in generic              # bare install, no flag


def test_healthy_but_this_agent_unregistered_still_registers_it():
    # engines ok, but a DIFFERENT agent is registered → not the "already set up" path
    p = build_prompt("/repo", "claude", _report(registered_agent="codex"))
    assert "already installed and healthy" not in p
    assert "codeintel install --agent claude" in p
    assert "setup --all" not in p                        # all engines ok → no setup step


def test_prompt_is_never_empty_and_always_tells_the_agent_to_verify():
    for kw in ("doctor --deep", "restart"):
        assert kw in build_prompt("/x", "claude", _report(graph="warn", healthy=False))
        assert kw in build_prompt("/x", "claude", {}, fresh=True)


def test_resolve_agent_honours_named_falls_back_to_detected_then_generic(monkeypatch):
    assert resolve_agent("codex") == "codex"
    assert resolve_agent("CLAUDE") == "claude"                       # case-insensitive
    monkeypatch.setattr("codeintel.agent_prompt.detect_agents", lambda: ["gemini", "zed"])
    assert resolve_agent("auto") == "gemini"                          # first detected
    assert resolve_agent("nonsense") == "gemini"                      # unknown name → detect
    monkeypatch.setattr("codeintel.agent_prompt.detect_agents", list)
    assert resolve_agent("auto") == ""                                # none → generic


def test_the_command_never_raises_and_fresh_needs_no_backend():
    from types import SimpleNamespace

    from codeintel.commands import prompt as prompt_cmd

    # --fresh takes no probe, so it must succeed with no backend and print a non-empty prompt.
    args = SimpleNamespace(project_root=None, agent="claude", fresh=True, deep=False)
    assert prompt_cmd.run(args) == 0
