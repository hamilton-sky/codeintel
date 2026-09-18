"""The upgrade path: what happens to a registration when the binary moves under it.

`codeintel install` writes an ABSOLUTE path, because a GUI-launched host does not source your shell
profile and a bare `codeintel` is routinely invisible to it. `docs/install.md` states the cost of
that choice and promises two things cover it:

    Re-running `codeintel install` repairs it in place — only codeintel's own entry is
    rewritten; neighbouring servers and unrelated settings stay byte-identical.
    `codeintel doctor` reports a stale launch command with the exact repair.

Both halves were already tested, and neither was tested as the thing a person actually does. The
repair was proven for **codex alone** — the one agent whose config is TOML — while `claude`,
`gemini` and `zed` write JSON through a different code path, in two different shapes
(`mcpServers` and `context_servers`). And the loop was never closed: a hand-written config with a
`/gone/venv/bin/codeintel` in it proves detection, and a separate hand-written config proves
repair, but nothing drove **install → the binary moves → doctor → reinstall → clean** end to end.
That sequence is the upgrade, and a promise about it is worth exactly as much as the weakest format
it was never run against.

WHY A FAKE BINARY RATHER THAN A MOCK. `resolve_command()` is `shutil.which("codeintel")` and
doctor's `runnable` is `isfile() and access(X_OK)` — both read the filesystem. Patching either one
would test the test. Here an executable file is created, registered, deleted, and recreated
somewhere else, which is what an upgrade does to a venv.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from codeintel.doctor import collect_registrations
from codeintel.installer import _CONFIG, Installer, registered_command, resolve_command

AGENTS = ["claude", "codex", "gemini", "zed"]


def _clear_agent_envs(monkeypatch) -> None:
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def _install_binary_at(monkeypatch, bindir: pathlib.Path) -> str:
    """Put a real, executable `codeintel` in `bindir` and make it the one PATH finds."""
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "codeintel"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert resolve_command() == str(exe), "the fixture binary is not the one PATH resolves"
    return str(exe)


def _config_path(agent: str) -> pathlib.Path:
    from codeintel.installer import resolve_config_path
    return pathlib.Path(resolve_config_path(_CONFIG[agent]))


def _recorded_command(agent: str) -> str | None:
    return registered_command(_CONFIG[agent])[1]


def _reg(agent: str) -> dict | None:
    return next((r for r in collect_registrations() if r["agent"] == agent), None)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The loop, closed, for every agent
# ══════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("agent", AGENTS)
def test_the_upgrade_loop_closes_for_every_agent(tmp_path, monkeypatch, agent):
    """install → the binary moves → doctor names it → reinstall → doctor is clean.

    Each step was covered in isolation and only for TOML. The value of running the sequence is that
    a repair which writes a SECOND entry, or writes to a different file than the one doctor reads,
    passes every isolated assertion and fails here — the two halves have to meet on the same file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    old = _install_binary_at(monkeypatch, tmp_path / "venv-a" / "bin")

    assert Installer().register(agent)["action"] == "registered"
    assert _recorded_command(agent) == old
    assert _reg(agent)["runnable"] is True, "a freshly installed registration is not runnable"

    # The upgrade: the old venv goes away and the binary reappears somewhere else.
    pathlib.Path(old).unlink()
    new = _install_binary_at(monkeypatch, tmp_path / "venv-b" / "bin")
    assert new != old

    stale = _reg(agent)
    assert stale["runnable"] is False, f"{agent}: a dead launch command was not reported"
    assert old in stale["remediation"]
    assert f"codeintel install --agent {agent}" in stale["remediation"]

    assert Installer().register(agent)["action"] == "registered"
    assert _recorded_command(agent) == new, f"{agent}: reinstall did not repair the command"

    repaired = _reg(agent)
    assert repaired["runnable"] is True, f"{agent}: still broken after the documented repair"
    assert repaired["remediation"] is None


@pytest.mark.parametrize("agent", AGENTS)
def test_a_repair_replaces_the_entry_rather_than_adding_one(tmp_path, monkeypatch, agent):
    """A second `codeintel` entry is the failure a config format makes easy and a reader never sees.

    The host launches one of them, and which one is a property of the parser's ordering rather than
    of anything codeintel decided. `registered_command` would keep reporting a healthy registration
    while a dead entry sat above it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    old = _install_binary_at(monkeypatch, tmp_path / "venv-a" / "bin")
    Installer().register(agent)

    pathlib.Path(old).unlink()
    _install_binary_at(monkeypatch, tmp_path / "venv-b" / "bin")
    Installer().register(agent)

    text = _config_path(agent).read_text()
    assert old not in text, f"{agent}: the dead command is still in the config"
    if _CONFIG[agent]["format"] == "json":
        section = json.loads(text)[_CONFIG[agent]["key"][0]]
        assert list(section).count("codeintel") == 1
    else:
        assert text.count(_CONFIG[agent]["table"]) == 1


@pytest.mark.parametrize("agent", AGENTS)
def test_the_repair_leaves_everything_it_did_not_write(tmp_path, monkeypatch, agent):
    """`docs/install.md` promises neighbouring servers and unrelated settings stay byte-identical.

    Proven for codex's TOML and asserted for nobody else, though the JSON writer is the one that
    round-trips the whole document through `json.loads`/`json.dumps` and therefore has the better
    chance of dropping something."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    old = _install_binary_at(monkeypatch, tmp_path / "venv-a" / "bin")
    Installer().register(agent)

    cfg = _config_path(agent)
    if _CONFIG[agent]["format"] == "json":
        doc = json.loads(cfg.read_text())
        doc[_CONFIG[agent]["key"][0]]["serena"] = {"command": "serena", "args": ["start"]}
        doc["unrelatedTopLevelSetting"] = {"theme": "dark", "fontSize": 13}
        cfg.write_text(json.dumps(doc, indent=2))
    else:
        cfg.write_text(cfg.read_text()
                       + '\n[mcp_servers.serena]\ncommand = "serena"\n'
                       + '\n[projects."/repo"]\ntrust_level = "trusted"\n')

    pathlib.Path(old).unlink()
    _install_binary_at(monkeypatch, tmp_path / "venv-b" / "bin")
    Installer().register(agent)

    text = cfg.read_text()
    if _CONFIG[agent]["format"] == "json":
        doc = json.loads(text)
        assert doc[_CONFIG[agent]["key"][0]]["serena"] == {"command": "serena", "args": ["start"]}
        assert doc["unrelatedTopLevelSetting"] == {"theme": "dark", "fontSize": 13}
    else:
        assert 'command = "serena"' in text
        assert 'trust_level = "trusted"' in text


@pytest.mark.parametrize("agent", AGENTS)
def test_an_upgrade_that_moves_nothing_rewrites_nothing(tmp_path, monkeypatch, agent):
    """The common case — a version bump inside the same venv — must be a no-op, byte for byte.

    A repair that rewrote the file anyway would churn every agent config on every `install`, and
    the idempotence claim is what makes `codeintel install` safe to put in a setup script."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    _install_binary_at(monkeypatch, tmp_path / "venv-a" / "bin")
    Installer().register(agent)

    cfg = _config_path(agent)
    before = cfg.read_bytes()
    assert Installer().register(agent)["action"] == "already"
    assert cfg.read_bytes() == before, f"{agent}: a no-op install rewrote the config"


def test_doctor_reports_only_the_agent_whose_binary_moved(tmp_path, monkeypatch):
    """Two agents registered, one config hand-pointed at a dead path: the healthy one must not be
    swept up. `runnable` is per registration, and a roll-up that condemned both would send someone
    re-running install against a config that was fine."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    _install_binary_at(monkeypatch, tmp_path / "venv-a" / "bin")
    Installer().register("claude")
    Installer().register("codex")

    cfg = _config_path("claude")
    doc = json.loads(cfg.read_text())
    doc["mcpServers"]["codeintel"]["command"] = str(tmp_path / "gone" / "codeintel")
    cfg.write_text(json.dumps(doc, indent=2))

    assert _reg("claude")["runnable"] is False
    assert _reg("codex")["runnable"] is True


def test_the_upgrade_guard_can_actually_fail(tmp_path, monkeypatch):
    """A loop that cannot fail records "we did not check" as green.

    Prove the instrumentation bites: move the binary and DON'T reinstall. The registration must
    still be reported broken — if it is not, every assertion above passes for free."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    old = _install_binary_at(monkeypatch, tmp_path / "venv-a" / "bin")
    Installer().register("claude")
    assert _reg("claude")["runnable"] is True

    pathlib.Path(old).unlink()
    _install_binary_at(monkeypatch, tmp_path / "venv-b" / "bin")

    assert _reg("claude")["runnable"] is False, (
        "a deleted binary was still reported runnable — this file is measuring nothing")
    assert os.path.isabs(_recorded_command("claude"))
