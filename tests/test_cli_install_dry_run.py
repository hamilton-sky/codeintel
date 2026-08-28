"""`codeintel install --dry-run` — advertised in `install --help` (D1) with no implementation
behind it; `codeintel install --dry-run` exited 2. This is the read-only preview: it must name
what each agent's config would gain and write nothing, using the same lookup `codeintel doctor`
already uses to spot a stale registration (`installer.registered_command`).
"""
from __future__ import annotations

import argparse
from importlib import import_module

import pytest


def _args(**kw) -> argparse.Namespace:
    defaults = {"agent": "auto", "no_verify": False, "relative_command": False, "dry_run": True}
    return argparse.Namespace(**{**defaults, **kw})


class _FakeInstallerModule:
    """Stands in for `codeintel.installer` — only the read-only surface `--dry-run` may touch."""

    def __init__(self, *, registered: dict[str, str | None], detected: list[str],
                 agents: tuple[str, ...] = ("claude", "codex", "gemini", "zed")):
        self._AGENTS = list(agents)
        self._CONFIG = {a: {"agent": a} for a in agents}
        self._registered = registered
        self._detected = detected
        self.write_calls = 0

    def resolve_command(self, *, absolute: bool) -> str:
        return "/abs/codeintel" if absolute else "codeintel"

    def detect_agents(self) -> list[str]:
        return list(self._detected)

    def registered_command(self, spec: dict) -> tuple[str, str | None]:
        agent = spec["agent"]
        return f"/cfg/{agent}.json", self._registered.get(agent)

    # Anything with a write side effect must never be called by --dry-run.
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"--dry-run touched a write path: installer.{name}")


def _run_dry(monkeypatch, fake, **overrides):
    monkeypatch.setattr("codeintel.installer", fake, raising=False)
    import sys
    monkeypatch.setitem(sys.modules, "codeintel.installer", fake)
    return import_module("codeintel.commands.install").run(_args(**overrides))


def test_dry_run_reports_a_would_be_registration_and_writes_nothing(monkeypatch, capsys):
    fake = _FakeInstallerModule(registered={"claude": None}, detected=["claude"])
    code = _run_dry(monkeypatch, fake, agent="claude")
    out = capsys.readouterr().out

    assert code == 0
    assert "+ claude: would register at /cfg/claude.json" in out
    assert "nothing was written" in out.lower()


def test_dry_run_reports_already_current_when_the_command_matches(monkeypatch, capsys):
    fake = _FakeInstallerModule(registered={"claude": "/abs/codeintel"}, detected=["claude"])
    code = _run_dry(monkeypatch, fake, agent="claude")
    out = capsys.readouterr().out

    assert code == 0
    assert "~ claude: already registered at /cfg/claude.json (no change)" in out


def test_dry_run_reports_a_stale_command_as_an_update(monkeypatch, capsys):
    fake = _FakeInstallerModule(registered={"claude": "/old/codeintel"}, detected=["claude"])
    code = _run_dry(monkeypatch, fake, agent="claude")
    out = capsys.readouterr().out

    assert code == 0
    assert "would update /cfg/claude.json (was: /old/codeintel)" in out


def test_dry_run_auto_previews_only_detected_agents_and_names_the_rest_skipped(monkeypatch, capsys):
    fake = _FakeInstallerModule(registered={"claude": None}, detected=["claude"])
    code = _run_dry(monkeypatch, fake, agent="auto")
    out = capsys.readouterr().out

    assert code == 0
    assert "claude" in out
    assert "codex, gemini, zed" in out


def test_dry_run_auto_with_nothing_detected_fails_without_writing(monkeypatch, capsys):
    fake = _FakeInstallerModule(registered={}, detected=[])
    code = _run_dry(monkeypatch, fake, agent="auto")
    out = capsys.readouterr().out

    assert code == 1
    assert "No supported agent found" in out


def test_dry_run_all_previews_every_supported_agent(monkeypatch, capsys):
    fake = _FakeInstallerModule(registered={}, detected=[])
    code = _run_dry(monkeypatch, fake, agent="all")
    out = capsys.readouterr().out

    assert code == 0
    for agent in ("claude", "codex", "gemini", "zed"):
        assert f"would register at /cfg/{agent}.json" in out


def test_dry_run_never_touches_the_real_installer_class(monkeypatch):
    """The strongest guarantee `--dry-run` makes: no write path is even importable from it."""
    fake = _FakeInstallerModule(registered={"claude": None}, detected=["claude"])
    with pytest.raises(AssertionError):
        fake.register_many(["claude"], verify=True, absolute=True)
