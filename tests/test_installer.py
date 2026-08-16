"""Installer / agent-registration tests.

Two host quirks are load-bearing here, and BOTH shipped broken because the tests asserted that a
file was written rather than that the host reads that file:

  * `--agent codex` wrote a Claude-style JSON `mcpServers` block to `~/.codex/config.json`, but
    Codex reads `~/.codex/config.toml` as `[mcp_servers.<name>]` TOML (fixed in 0.8.2).
  * `--agent claude` wrote `mcpServers` into `~/.claude/settings.json`, but Claude Code reads
    user-scope MCP servers from `~/.claude.json` — `claude mcp list` never saw codeintel.

The structural lesson is in test_verify.py / test_mcp_handshake.py: config contents are necessary
but not sufficient, so `install` now also completes a real MCP handshake.
"""
from __future__ import annotations

import json
from pathlib import Path

from codeintel.installer import (
    Installer,
    detect_agents,
    resolve_command,
    resolve_config_path,
    _CONFIG,
)


def _codex_toml(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _claude_json(home: Path) -> Path:
    return home / ".claude.json"


# --------------------------------------------------------------------------- codex (TOML)

def test_codex_registers_as_toml_in_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cfg = _codex_toml(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.serena]\ncommand = "serena"\n')  # pre-existing MCP server

    res = Installer().register("codex")

    assert res["ok"] and res["action"] == "registered"
    text = cfg.read_text()
    assert "[mcp_servers.codeintel]" in text                 # written to config.toml as TOML
    assert f'command = "{resolve_command()}"' in text and 'args = ["serve"]' in text
    assert "[mcp_servers.serena]" in text                    # existing content preserved


def test_codex_does_not_write_the_wrong_json_file(tmp_path, monkeypatch):
    # regression: the old installer wrote ~/.codex/config.json (mcpServers), which Codex ignores
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    Installer().register("codex")
    assert _codex_toml(tmp_path).exists()
    assert not (tmp_path / ".codex" / "config.json").exists()


def test_codex_creates_config_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    res = Installer().register("codex")
    assert res["ok"] and _codex_toml(tmp_path).read_text().startswith("[mcp_servers.codeintel]")


def test_codex_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert Installer().register("codex")["action"] == "registered"
    assert Installer().register("codex")["action"] == "already"
    assert _codex_toml(tmp_path).read_text().count("[mcp_servers.codeintel]") == 1  # not duplicated


def test_codex_home_env_is_honored(tmp_path, monkeypatch):
    """Managed/enterprise setups relocate Codex with CODEX_HOME; writing to ~/.codex there
    registers into a directory the CLI never reads."""
    monkeypatch.setenv("HOME", str(tmp_path))
    managed = tmp_path / "managed" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(managed))

    res = Installer().register("codex")

    assert res["ok"] and res["path"] == str(managed / "config.toml")
    assert "[mcp_servers.codeintel]" in (managed / "config.toml").read_text()
    assert not _codex_toml(tmp_path).exists()          # NOT the default home


def test_empty_env_var_falls_back_to_the_default_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "   ")            # set-but-blank must not yield a bare path
    assert Installer().register("codex")["path"] == str(_codex_toml(tmp_path))


# --------------------------------------------------------------------------- claude (JSON)

def test_claude_registers_into_claude_json_not_settings_json(tmp_path, monkeypatch):
    """The regression that mattered: Claude Code reads ~/.claude.json for user-scope MCP
    servers. An `mcpServers` block in ~/.claude/settings.json is silently ignored."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    res = Installer().register("claude")

    assert res["ok"] and res["path"] == str(_claude_json(tmp_path))
    data = json.loads(_claude_json(tmp_path).read_text())
    assert data["mcpServers"]["codeintel"] == {"command": resolve_command(), "args": ["serve"]}
    assert not (tmp_path / ".claude" / "settings.json").exists()   # the file Claude ignores


def test_claude_preserves_unrelated_config(tmp_path, monkeypatch):
    """~/.claude.json holds the user's project history and settings — registration must merge."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cfg = _claude_json(tmp_path)
    cfg.write_text(json.dumps({
        "numStartups": 42,
        "projects": {"/repo": {"allowedTools": []}},
        "mcpServers": {"other": {"command": "x"}},
    }))

    Installer().register("claude")

    data = json.loads(cfg.read_text())
    assert data["numStartups"] == 42                  # unrelated keys preserved
    assert data["projects"] == {"/repo": {"allowedTools": []}}
    assert "other" in data["mcpServers"]              # other server preserved
    assert "codeintel" in data["mcpServers"]          # ours added


def test_claude_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert Installer().register("claude")["action"] == "registered"
    assert Installer().register("claude")["action"] == "already"


def test_claude_config_dir_env_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    relocated = tmp_path / "xdg" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))

    res = Installer().register("claude")

    assert res["path"] == str(relocated / ".claude.json")
    assert "codeintel" in json.loads((relocated / ".claude.json").read_text())["mcpServers"]


def test_stale_settings_json_entry_is_reported_not_deleted(tmp_path, monkeypatch):
    """A pre-0.11.2 codeintel left an inert `mcpServers.codeintel` in ~/.claude/settings.json.
    Surface it so the user can clean it up; never edit a user config we no longer own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    stale = tmp_path / ".claude" / "settings.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"theme": "dark", "mcpServers": {"codeintel": {"command": "x"}}}))

    res = Installer().register("claude")

    assert res["legacy"] == str(stale)
    assert json.loads(stale.read_text())["mcpServers"]["codeintel"] == {"command": "x"}  # untouched


def test_no_stale_note_when_settings_json_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    stale = tmp_path / ".claude" / "settings.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"theme": "dark"}))
    assert Installer().register("claude")["legacy"] is None


# --------------------------------------------------------------------------- other json agents

def test_zed_uses_context_servers_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    Installer().register("zed")
    data = json.loads((tmp_path / ".config" / "zed" / "settings.json").read_text())
    assert "codeintel" in data["context_servers"]


def test_zed_honors_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    res = Installer().register("zed")
    assert res["path"] == str(tmp_path / "xdg" / "zed" / "settings.json")


def test_gemini_registers_mcp_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_CONFIG_DIR", raising=False)
    Installer().register("gemini")
    data = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert data["mcpServers"]["codeintel"] == {"command": resolve_command(), "args": ["serve"]}


# --------------------------------------------------------------------------- verification

def test_register_can_verify_the_handshake(tmp_path, monkeypatch):
    """`verify=True` must actually launch the registered command, not just write the file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    calls: list = []

    def _fake(command, args=None, **kw):
        calls.append((command, args))
        return {"ok": True, "tools": ["code.query"], "server": None, "detail": "stub"}

    monkeypatch.setattr("codeintel.verify.verify_stdio_server", _fake)

    res = Installer().register("claude", verify=True)

    # The launched argv must be the argv in the CONFIG FILE — verifying a differently-resolved
    # command proves nothing about the line the host will actually run.
    written = json.loads(_claude_json(tmp_path).read_text())["mcpServers"]["codeintel"]
    assert calls == [(written["command"], written["args"])]
    assert res["verified"]["ok"] is True


def test_failed_verification_is_reported_on_the_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "codeintel.verify.verify_stdio_server",
        lambda *a, **k: {"ok": False, "tools": [], "server": None, "detail": "not on PATH"},
    )
    res = Installer().register("claude", verify=True)
    assert res["ok"] is True                          # the file WAS written…
    assert res["verified"]["ok"] is False             # …but the host cannot launch it


def test_verification_runs_once_for_register_all(tmp_path, monkeypatch):
    """Verification tests the COMMAND, which is identical for every agent — booting a server
    per agent would quadruple the cost of `codeintel install` for no extra signal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    calls: list = []
    monkeypatch.setattr(
        "codeintel.verify.verify_stdio_server",
        lambda *a, **k: (calls.append(1), {"ok": True, "tools": ["t"], "server": None,
                                           "detail": "stub"})[1],
    )
    results = Installer().register_all(verify=True)
    assert len(calls) == 1
    assert all(r["verified"]["ok"] for r in results)


def test_verification_never_raises_out_of_register(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "codeintel.verify.verify_stdio_server",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    res = Installer().register("claude", verify=True)
    assert res["ok"] is True and res["verified"]["ok"] is False


# --------------------------------------------------------------------------- misc

def test_unknown_agent_fails_safely():
    res = Installer().register("nonesuch")
    assert res["ok"] is False and res["action"] == "failed" and "unknown" in res["reason"]


def test_register_all_covers_every_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    results = Installer().register_all()
    assert {r["agent"] for r in results} == {"claude", "codex", "gemini", "zed"}
    assert all(r["ok"] for r in results)


def test_every_agent_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    """Paths must not be frozen at import — otherwise an env var exported by the shell (or a
    test) after import is ignored and registration lands in the wrong home.

    Each agent's own home var is cleared first, because `resolve_config_path` deliberately
    PREFERS it over `~`. Without that, this passes on a dev laptop and fails on any host that
    exports one of them — a GitHub runner sets XDG_CONFIG_HOME, so `~/.config` for zed correctly
    resolved outside tmp_path and reddened CI for behavior that is working as documented."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    for spec in _CONFIG.values():
        assert str(resolve_config_path(spec)).startswith(str(tmp_path))


def test_an_agents_own_home_var_wins_over_home(tmp_path, monkeypatch):
    """The other half of call-time resolution: a managed/CI setup that relocates an agent's config
    root must be registered THERE, not in a `~` nobody reads."""
    monkeypatch.setenv("HOME", str(tmp_path / "ignored"))
    for spec in _CONFIG.values():
        var = spec.get("env")
        if not var:
            continue
        relocated = tmp_path / f"relocated-{var.lower()}"
        monkeypatch.setenv(var, str(relocated))
        assert resolve_config_path(spec) == relocated / spec["file"]


# --------------------------------------------------------------------------- launch command

def test_registered_command_is_an_absolute_path_by_default(tmp_path, monkeypatch):
    """A GUI-launched desktop agent does not source your shell profile, so a bare `codeintel` that
    a terminal resolves can be invisible to the app — and verification, which runs in THIS
    process's environment, would confirm a launch line the host cannot use."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    exe = fake_bin / "codeintel"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    Installer().register("claude")

    entry = json.loads(_claude_json(tmp_path).read_text())["mcpServers"]["codeintel"]
    assert entry["command"] == str(exe)
    assert Path(entry["command"]).is_absolute()


def test_relative_command_opts_back_into_the_bare_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    Installer().register("claude", absolute=False)
    entry = json.loads(_claude_json(tmp_path).read_text())["mcpServers"]["codeintel"]
    assert entry == {"command": "codeintel", "args": ["serve"]}


def test_falls_back_to_the_bare_name_when_not_on_path(monkeypatch, tmp_path):
    """A source checkout that has not been pip-installed still gets a usable (if unresolved)
    entry rather than an empty command."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert resolve_command() == "codeintel"


def test_reinstall_repairs_a_stale_codex_command(tmp_path, monkeypatch):
    """The cost of absolute paths: an upgrade moves the binary. Re-running install must repair the
    dead command in place, and leave every other table in the hand-edited config untouched."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cfg = _codex_toml(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[mcp_servers.serena]\ncommand = "serena"\n\n'
        '[mcp_servers.codeintel]\ncommand = "/gone/venv/bin/codeintel"\nargs = ["serve"]\n\n'
        '[projects."/repo"]\ntrust_level = "trusted"\n'
    )

    res = Installer().register("codex")

    text = cfg.read_text()
    assert res["action"] == "registered"
    assert "/gone/venv/bin/codeintel" not in text                 # stale command replaced
    assert f'command = "{resolve_command()}"' in text
    assert text.count("[mcp_servers.codeintel]") == 1             # not duplicated
    assert '[mcp_servers.serena]\ncommand = "serena"' in text     # neighbors untouched
    assert 'trust_level = "trusted"' in text


def test_codex_reinstall_is_still_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert Installer().register("codex")["action"] == "registered"
    assert Installer().register("codex")["action"] == "already"


# --------------------------------------------------------------------------- zed schema

def test_zed_entry_uses_the_flat_command_shape(tmp_path, monkeypatch):
    """Zed's `context_servers` entry is flat — `command` is a string with `args` beside it.
    codeintel shipped the nested `{"command": {"path", "args"}}` form, which Zed does not read;
    the third instance of writing a config the host ignores."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    Installer().register("zed")
    entry = json.loads((tmp_path / ".config" / "zed" / "settings.json").read_text())
    entry = entry["context_servers"]["codeintel"]
    assert isinstance(entry["command"], str)          # NOT a nested {"path": ...} object
    assert entry["args"] == ["serve"]


# --------------------------------------------------------------------------- auto detection

def _clear_agent_envs(monkeypatch):
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_auto_registers_only_agents_present_on_this_machine(tmp_path, monkeypatch):
    """`--agent all` as the default wrote ~/.gemini/ and ~/.config/zed/ for people who have
    neither — reaching into another app's namespace uninvited, and claiming coverage of hosts
    nobody verified."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    (tmp_path / ".codex").mkdir()          # only Codex is "installed"

    results, skipped = Installer().register_detected()

    assert [r["agent"] for r in results] == ["codex"]
    assert set(skipped) == {"claude", "gemini", "zed"}
    assert not (tmp_path / ".gemini").exists()
    assert not (tmp_path / ".config" / "zed").exists()


def test_auto_detects_each_agent_by_its_own_config_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    assert detect_agents() == []

    (tmp_path / ".claude.json").write_text("{}")
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".config" / "zed").mkdir(parents=True)
    assert set(detect_agents()) == {"claude", "gemini", "zed"}


def test_auto_honors_a_relocated_config_root(tmp_path, monkeypatch):
    """A managed/CI setup that moves CODEX_HOME is still 'installed'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    relocated = tmp_path / "managed" / "codex"
    relocated.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(relocated))
    assert "codex" in detect_agents()


def test_auto_registers_nothing_when_no_agent_is_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    results, skipped = Installer().register_detected()
    assert results == []
    assert set(skipped) == {"claude", "codex", "gemini", "zed"}
    assert list(tmp_path.iterdir()) == []      # nothing written at all


def test_all_still_forces_every_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    results = Installer().register_all()
    assert {r["agent"] for r in results} == {"claude", "codex", "gemini", "zed"}
    assert all(r["ok"] for r in results)


# --------------------------------------------------------------------------- stale registrations

def test_doctor_reports_a_stale_absolute_command(tmp_path, monkeypatch):
    """The one regression absolute paths introduce: an upgrade moves the binary and the config
    points at nothing. Invisible from inside the agent — the host just says the server won't
    start — so doctor has to name it."""
    from codeintel.doctor import collect_registrations

    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"codeintel": {"command": "/gone/venv/bin/codeintel", "args": ["serve"]}}}
    ))

    regs = {r["agent"]: r for r in collect_registrations()}

    assert regs["claude"]["runnable"] is False
    assert "re-run `codeintel install --agent claude`" in regs["claude"]["remediation"]
    assert "codex" not in regs                    # not registered ⇒ not reported


def test_doctor_reports_a_live_registration_as_runnable(tmp_path, monkeypatch):
    from codeintel.doctor import collect_registrations

    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    Installer().register("claude")

    regs = {r["agent"]: r for r in collect_registrations()}
    assert regs["claude"]["runnable"] is True
    assert regs["claude"]["remediation"] is None


def test_registered_command_reads_back_every_config_format(tmp_path, monkeypatch):
    from codeintel.installer import registered_command

    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    for agent in ("claude", "codex", "gemini", "zed"):
        Installer().register(agent)
        _path, command = registered_command(_CONFIG[agent])
        assert command == resolve_command(), agent


def test_registered_command_is_none_when_absent(tmp_path, monkeypatch):
    from codeintel.installer import registered_command

    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    assert registered_command(_CONFIG["codex"])[1] is None          # no file at all
    cfg = _codex_toml(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.other]\ncommand = "x"\n')          # file, but not ours
    assert registered_command(_CONFIG["codex"])[1] is None


def test_registered_command_never_raises_on_a_corrupt_config(tmp_path, monkeypatch):
    from codeintel.doctor import collect_registrations
    from codeintel.installer import registered_command

    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)
    (tmp_path / ".claude.json").write_text("{ not json at all ")
    assert registered_command(_CONFIG["claude"])[1] is None
    assert collect_registrations() == []
