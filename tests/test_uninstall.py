"""`codeintel uninstall` — removing exactly what `install` wrote, and nothing else.

The readiness doc's Phase 6 asked for "upgrade and uninstall paths tested", and the uninstall half
could not be closed by testing because no command removed a registration: `reset` removes caches,
`install` writes registrations, and nothing was the inverse of `install`. This is that inverse.

The whole risk of the command is in one sentence — it edits a user-owned config file that holds
other people's servers and settings — so most of this file is about what it must NOT do:

  * never delete the config file, not even when codeintel was its only entry. Deleting somebody's
    `~/.claude.json` because we happened to be its last server is a far larger action than the one
    that was asked for;
  * never touch a neighbouring server or an unrelated setting, in either format;
  * never rewrite a file it could not fully parse — Zed's JSONC and an ambiguous duplicate TOML
    table are refused with the block to remove by hand, which is the same refusal `install` makes;
  * be a no-op the second time, and say so rather than reporting work.

`--agent auto` means "wherever codeintel is REGISTERED", which is deliberately a different question
from the one `install --agent auto` asks ("what is installed on this machine"). They differ in both
directions that matter: an agent uninstalled from this machine can still hold our entry, and an
agent present but never registered has nothing to remove.
"""
from __future__ import annotations

import argparse
import json
import pathlib

import pytest

from codeintel.commands import uninstall as uninstall_cmd
from codeintel.installer import _AGENTS, _CONFIG, Installer, registered_agents, registered_command

AGENTS = list(_AGENTS)


def _clear_agent_envs(monkeypatch) -> None:
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def _home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_agent_envs(monkeypatch)


def _config_path(agent: str) -> pathlib.Path:
    from codeintel.installer import resolve_config_path
    return pathlib.Path(resolve_config_path(_CONFIG[agent]))


def _args(agent: str = "auto", dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(agent=agent, dry_run=dry_run)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The round trip
# ══════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("agent", AGENTS)
def test_install_then_uninstall_leaves_no_registration(tmp_path, monkeypatch, agent):
    _home(tmp_path, monkeypatch)
    Installer().register(agent)
    assert registered_command(_CONFIG[agent])[1] is not None

    res = Installer().unregister(agent)

    assert res["action"] == "removed" and res["ok"] is True
    assert registered_command(_CONFIG[agent])[1] is None
    assert agent not in registered_agents()


@pytest.mark.parametrize("agent", AGENTS)
def test_the_config_file_survives_even_when_we_were_its_only_entry(tmp_path, monkeypatch, agent):
    """The file belongs to the host, not to codeintel. Removing our last entry is not a licence to
    delete somebody's agent configuration — and for `claude` that file is `~/.claude.json`, which
    holds far more than MCP servers."""
    _home(tmp_path, monkeypatch)
    Installer().register(agent)
    cfg = _config_path(agent)
    assert cfg.exists()

    Installer().unregister(agent)

    assert cfg.exists(), f"{agent}: uninstall deleted the config file"


@pytest.mark.parametrize("agent", AGENTS)
def test_uninstalling_twice_is_a_no_op_that_says_so(tmp_path, monkeypatch, agent):
    """`absent` is not a failure. A teardown script that runs whether or not install ever ran must
    not be told it failed for reaching the state it wanted."""
    _home(tmp_path, monkeypatch)
    Installer().register(agent)
    assert Installer().unregister(agent)["action"] == "removed"

    cfg = _config_path(agent)
    after_first = cfg.read_bytes()
    second = Installer().unregister(agent)

    assert second["action"] == "absent" and second["ok"] is True
    assert cfg.read_bytes() == after_first, f"{agent}: a no-op uninstall rewrote the config"


def test_uninstalling_an_agent_that_was_never_registered_is_absent(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    res = Installer().unregister("claude")
    assert res["action"] == "absent" and res["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════════════════════
# What it must not touch
# ══════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("agent", AGENTS)
def test_neighbouring_servers_and_settings_survive(tmp_path, monkeypatch, agent):
    """The same guarantee `install`'s repair makes, in the other direction. The JSON path
    round-trips the whole document through `json.loads`/`json.dumps`, so it has the best chance of
    dropping something a user cared about."""
    _home(tmp_path, monkeypatch)
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

    assert Installer().unregister(agent)["action"] == "removed"

    text = cfg.read_text()
    assert "codeintel" not in text, f"{agent}: our entry survived"
    if _CONFIG[agent]["format"] == "json":
        doc = json.loads(text)
        assert doc[_CONFIG[agent]["key"][0]]["serena"] == {"command": "serena", "args": ["start"]}
        assert doc["unrelatedTopLevelSetting"] == {"theme": "dark", "fontSize": 13}
    else:
        assert 'command = "serena"' in text
        assert 'trust_level = "trusted"' in text


def test_a_jsonc_config_is_refused_rather_than_stripped_of_its_comments(tmp_path, monkeypatch):
    """Zed ships `settings.json` as JSONC. Rewriting it through `json.dumps` would silently delete
    the user's comments, which in Zed's default config is most of the file. `install` refuses for
    this reason; removing an entry does not make it acceptable."""
    _home(tmp_path, monkeypatch)
    cfg = _config_path("zed")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    original = (
        '{\n'
        '  // my editor settings\n'
        '  "theme": "One Dark",\n'
        '  "context_servers": { "codeintel": { "command": "codeintel", "args": ["serve"] } },\n'
        '}\n'
    )
    cfg.write_text(original)

    res = Installer().unregister("zed")

    assert res["action"] == "failed" and res["ok"] is False
    assert "JSONC" in res["reason"] and "by hand" in res["reason"]
    assert cfg.read_text() == original, "the JSONC config was modified despite the refusal"


def test_a_config_that_is_not_an_object_is_refused(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cfg = _config_path("claude")
    cfg.write_text('["not", "an", "object"]')

    res = Installer().unregister("claude")

    assert res["action"] == "failed"
    assert cfg.read_text() == '["not", "an", "object"]'


def test_duplicate_toml_tables_are_refused_rather_than_guessed(tmp_path, monkeypatch):
    """Two `[mcp_servers.codeintel]` tables is ambiguous — deleting "the" table means choosing one,
    and the file is hand-maintained. `_replace_toml_table` refuses the same shape for the same
    reason; a stale entry the user can see beats a config we rewrote without fully parsing it."""
    _home(tmp_path, monkeypatch)
    cfg = _config_path("codex")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    original = (
        '[mcp_servers.codeintel]\ncommand = "/one/codeintel"\nargs = ["serve"]\n\n'
        '[mcp_servers.codeintel]\ncommand = "/two/codeintel"\nargs = ["serve"]\n'
    )
    cfg.write_text(original)

    res = Installer().unregister("codex")

    assert res["action"] == "failed" and res["ok"] is False
    assert "by hand" in res["reason"]
    assert cfg.read_text() == original


def test_removing_the_codex_table_does_not_accumulate_blank_lines(tmp_path, monkeypatch):
    """install → uninstall → install must not grow the file. The separator blank line is written by
    install, so it has to be taken back by uninstall or every cycle adds one."""
    _home(tmp_path, monkeypatch)
    cfg = _config_path("codex")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[mcp_servers.serena]\ncommand = "serena"\n')
    first = cfg.read_text()

    for _ in range(3):
        Installer().register("codex")
        Installer().unregister("codex")

    assert cfg.read_text() == first, "a register/unregister cycle is not neutral on the file"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Selection, and the command itself
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_auto_targets_where_it_is_registered_not_what_is_installed(tmp_path, monkeypatch):
    """The distinction this command exists to get right, asserted on a tree where the two
    populations genuinely differ.

    `install --agent auto` asks what is INSTALLED here. Uninstall has to ask where our entry
    actually is. Codex below is installed and never registered: an install-side `auto` would target
    it, and uninstall must leave it alone — not create its config, not rewrite it, not report work
    on a file it has nothing in.
    """
    from codeintel.installer import detect_agents

    _home(tmp_path, monkeypatch)
    (tmp_path / ".codex").mkdir()           # installed, never registered
    Installer().register("claude")          # registered

    assert "codex" in detect_agents(), "fixture broken: codex should look installed"
    assert registered_agents() == ["claude"], registered_agents()

    codex_cfg = _config_path("codex")
    assert not codex_cfg.exists()

    assert uninstall_cmd.run(_args(agent="auto")) == 0

    assert registered_agents() == []
    assert not codex_cfg.exists(), "uninstall created a config for an agent it had nothing in"


def test_the_command_reports_nothing_to_do_as_success(tmp_path, monkeypatch, capsys):
    """Exit 0 for a goal that already holds. A teardown script should not fail because install
    never ran."""
    _home(tmp_path, monkeypatch)

    code = uninstall_cmd.run(_args(agent="auto"))

    assert code == 0
    assert "not registered with any agent" in capsys.readouterr().out


def test_dry_run_changes_nothing_and_names_the_command_it_would_remove(tmp_path, monkeypatch,
                                                                      capsys):
    _home(tmp_path, monkeypatch)
    Installer().register("claude")
    before = _config_path("claude").read_bytes()

    code = uninstall_cmd.run(_args(agent="auto", dry_run=True))
    out = capsys.readouterr().out

    assert code == 0
    assert "would remove" in out and "Dry run" in out
    assert _config_path("claude").read_bytes() == before
    assert registered_agents() == ["claude"], "a dry run removed the registration"


def test_the_command_says_the_index_is_untouched(tmp_path, monkeypatch, capsys):
    """The expensive artifact this command deliberately does not own. Ten minutes of embedding on a
    large repo, and a command called "uninstall" destroying it silently is the surprise this note
    exists to prevent — so it names what was not done, and the command that does it."""
    _home(tmp_path, monkeypatch)
    Installer().register("claude")

    uninstall_cmd.run(_args(agent="auto"))
    out = capsys.readouterr().out

    assert "index is untouched" in out
    assert "codeintel reset --all" in out


def test_a_refusal_is_reported_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    cfg = _config_path("zed")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{\n  // comment\n  "context_servers": {"codeintel": {"command": "x"}},\n}\n')

    code = uninstall_cmd.run(_args(agent="zed"))

    assert code == 1
    assert "x zed: failed" in capsys.readouterr().out


def test_doctor_stops_reporting_a_registration_after_uninstall(tmp_path, monkeypatch):
    """The cross-check between the two halves. `doctor` reads the config independently of the
    installer, so a removal that wrote to a different file than doctor reads would pass every
    assertion above and leave the agent still listed."""
    from codeintel.doctor import collect_registrations

    _home(tmp_path, monkeypatch)
    Installer().register("claude")
    assert any(r["agent"] == "claude" for r in collect_registrations())

    Installer().unregister("claude")

    assert not any(r["agent"] == "claude" for r in collect_registrations())


def test_uninstall_is_reversible_by_install(tmp_path, monkeypatch):
    """The reason this command needs no confirmation prompt: `codeintel install` puts it back. That
    is the argument for the difference from `reset --all`, so it is worth one assertion."""
    _home(tmp_path, monkeypatch)
    Installer().register("claude")
    original = registered_command(_CONFIG["claude"])[1]

    Installer().unregister("claude")
    assert registered_command(_CONFIG["claude"])[1] is None

    Installer().register("claude")
    assert registered_command(_CONFIG["claude"])[1] == original


def test_the_uninstall_guard_can_actually_fail(tmp_path, monkeypatch):
    """A round trip that cannot fail records "we did not check" as green. Prove the reader bites:
    a registration that was never removed must still be visible."""
    _home(tmp_path, monkeypatch)
    Installer().register("claude")

    assert registered_agents() == ["claude"], (
        "a fresh registration is invisible to the reader these tests assert with")
