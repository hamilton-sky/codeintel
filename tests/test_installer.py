"""Installer / agent-registration tests (added 0.8.2).

The installer had no tests, which is why `--agent codex` shipped writing a Claude-style JSON
`mcpServers` block to `~/.codex/config.json` — but Codex CLI reads MCP servers from
`~/.codex/config.toml` as `[mcp_servers.<name>]` TOML, so nothing was actually registered.
"""
from __future__ import annotations

import json
from pathlib import Path

from codeintel.installer import Installer


def _codex_toml(home: Path) -> Path:
    return home / ".codex" / "config.toml"


# --------------------------------------------------------------------------- codex (TOML)

def test_codex_registers_as_toml_in_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _codex_toml(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.serena]\ncommand = "serena"\n')  # pre-existing MCP server

    res = Installer().register("codex")

    assert res["ok"] and res["action"] == "registered"
    text = cfg.read_text()
    assert "[mcp_servers.codeintel]" in text                 # written to config.toml as TOML
    assert 'command = "codeintel"' in text and 'args = ["serve"]' in text
    assert "[mcp_servers.serena]" in text                    # existing content preserved


def test_codex_does_not_write_the_wrong_json_file(tmp_path, monkeypatch):
    # regression: the old installer wrote ~/.codex/config.json (mcpServers), which Codex ignores
    monkeypatch.setenv("HOME", str(tmp_path))
    Installer().register("codex")
    assert _codex_toml(tmp_path).exists()
    assert not (tmp_path / ".codex" / "config.json").exists()


def test_codex_creates_config_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = Installer().register("codex")
    assert res["ok"] and _codex_toml(tmp_path).read_text().startswith("[mcp_servers.codeintel]")


def test_codex_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Installer().register("codex")["action"] == "registered"
    assert Installer().register("codex")["action"] == "already"
    assert _codex_toml(tmp_path).read_text().count("[mcp_servers.codeintel]") == 1  # not duplicated


# --------------------------------------------------------------------------- json agents

def test_claude_registers_json_mcp_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = Installer().register("claude")
    assert res["ok"]
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["mcpServers"]["codeintel"] == {"command": "codeintel", "args": ["serve"]}


def test_claude_preserves_unrelated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = tmp_path / ".claude" / "settings.json"
    s.parent.mkdir(parents=True)
    s.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    Installer().register("claude")
    data = json.loads(s.read_text())
    assert data["theme"] == "dark"                # unrelated key preserved
    assert "other" in data["mcpServers"]          # other server preserved
    assert "codeintel" in data["mcpServers"]      # ours added


def test_zed_uses_context_servers_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    Installer().register("zed")
    data = json.loads((tmp_path / ".config" / "zed" / "settings.json").read_text())
    assert "codeintel" in data["context_servers"]


# --------------------------------------------------------------------------- misc

def test_unknown_agent_fails_safely():
    res = Installer().register("nonesuch")
    assert res["ok"] is False and res["action"] == "failed" and "unknown" in res["reason"]


def test_register_all_covers_every_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    results = Installer().register_all()
    assert {r["agent"] for r in results} == {"claude", "codex", "gemini", "zed"}
    assert all(r["ok"] for r in results)
