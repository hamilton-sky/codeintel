from __future__ import annotations

import json
import os
import pathlib
from typing import Optional

_AGENTS = ["claude", "codex", "gemini", "zed"]

_SERVER_NAME = "codeintel"
_LAUNCH = {"command": "codeintel", "args": ["serve"]}


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Write via a sibling temp file + os.replace so an interrupted write can never truncate
    the user's existing agent config (which holds unrelated settings) to a partial/empty file.
    The temp lives in the same directory as ``path`` so the replace stays on one filesystem."""
    tmp = path.with_name(path.name + ".codeintel.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# Per-agent registration recipe.
#
# `home` is the agent's config ROOT and is resolved through the agent's own documented env var
# first (CODEX_HOME, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME) so managed/enterprise/CI setups that
# relocate it are registered correctly instead of writing to a `~` nobody reads. `file` is the
# path under it.
#
# Two host quirks the file layout has to respect, both of which previously shipped broken:
#   * Codex reads MCP servers from `config.toml` as `[mcp_servers.<name>]` TOML tables — NOT a
#     JSON `mcpServers` map in config.json.
#   * Claude Code reads user-scope MCP servers from `~/.claude.json` — NOT `~/.claude/settings.json`
#     (that file is for hooks/theme/permissions; an `mcpServers` block there is silently ignored,
#     which `claude mcp list` confirms).
_CONFIG: dict[str, dict] = {
    "claude": {
        "format": "json",
        "env": "CLAUDE_CONFIG_DIR",
        "home": "~",
        "file": ".claude.json",
        "key": ["mcpServers", _SERVER_NAME],
        "value": dict(_LAUNCH),
        # Where a pre-0.11.2 codeintel wrote — inert, reported so the user can delete it.
        "legacy": {"file": ".claude/settings.json", "key": ["mcpServers", _SERVER_NAME]},
    },
    "codex": {
        "format": "toml-mcp",
        "env": "CODEX_HOME",
        "home": "~/.codex",
        "file": "config.toml",
        "table": f"[mcp_servers.{_SERVER_NAME}]",
        "block": f'[mcp_servers.{_SERVER_NAME}]\ncommand = "codeintel"\nargs = ["serve"]\n',
    },
    "gemini": {
        "format": "json",
        "env": "GEMINI_CONFIG_DIR",
        "home": "~/.gemini",
        "file": "settings.json",
        "key": ["mcpServers", _SERVER_NAME],
        "value": dict(_LAUNCH),
    },
    "zed": {
        "format": "json",
        "env": "XDG_CONFIG_HOME",
        "home": "~/.config",
        "file": "zed/settings.json",
        "key": ["context_servers", _SERVER_NAME],
        "value": {"command": {"path": "codeintel", "args": ["serve"]}},
    },
}


def resolve_config_path(spec: dict) -> pathlib.Path:
    """Agent config path, honoring the agent's own home env var when it is set and non-empty.

    Resolved at call time (not import time) so tests and shells that export CODEX_HOME /
    CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME after import still get the right file."""
    raw = os.environ.get(spec.get("env") or "", "").strip()
    home = pathlib.Path(raw).expanduser() if raw else pathlib.Path(spec["home"]).expanduser()
    return home / spec["file"]


def _get_nested(data: dict, keys: list[str]):
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _set_nested(data: dict, keys: list[str], value) -> None:
    node = data
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


class Installer:
    def register(self, agent: str, *, verify: bool = False, timeout_s: float = 45.0) -> dict:
        """Register codeintel with *agent*.

        With ``verify=True`` the registered command is then launched exactly as the host would
        and driven through a real MCP handshake — so a "registered" result means the agent can
        actually use it, not merely that a file was written."""
        spec = _CONFIG.get(agent)
        if spec is None:
            return self._result(agent, "", False, "failed", f"unknown agent '{agent}'")
        config_path = resolve_config_path(spec)
        try:
            if spec.get("format") == "toml-mcp":
                res = self._register_toml(agent, config_path, spec)
            else:
                res = self._register_json(agent, config_path, spec)
        except Exception as exc:
            return self._result(agent, str(config_path), False, "failed", str(exc))

        res["legacy"] = self._legacy_note(spec)
        if verify and res.get("ok"):
            res["verified"] = self.verify(timeout_s=timeout_s)
        return res

    @staticmethod
    def verify(*, timeout_s: float = 45.0) -> dict:
        """Launch the registered command and complete an MCP handshake. Never raises."""
        try:
            from codeintel.verify import verify_stdio_server
            return verify_stdio_server(
                _LAUNCH["command"], list(_LAUNCH["args"]), timeout_s=timeout_s
            )
        except Exception as exc:
            return {"ok": False, "tools": [], "server": None,
                    "detail": f"verification unavailable ({type(exc).__name__})"}

    @staticmethod
    def _legacy_note(spec: dict) -> Optional[str]:
        """Path to a stale registration an older codeintel wrote into a file the host ignores.
        Read-only — never deleted automatically, since it lives in a user-owned config."""
        legacy = spec.get("legacy")
        if not legacy:
            return None
        try:
            raw = os.environ.get(spec.get("env") or "", "").strip()
            home = pathlib.Path(raw).expanduser() if raw else pathlib.Path(spec["home"]).expanduser()
            path = home / legacy["file"]
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and _get_nested(data, legacy["key"]) is not None:
                return str(path)
        except Exception:
            return None
        return None

    def _register_json(self, agent: str, config_path: pathlib.Path, spec: dict) -> dict:
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}

        if _get_nested(data, spec["key"]) == spec["value"]:
            return self._result(agent, str(config_path), True, "already")

        config_path.parent.mkdir(parents=True, exist_ok=True)
        _set_nested(data, spec["key"], spec["value"])
        _atomic_write_text(config_path, json.dumps(data, indent=2))
        return self._result(agent, str(config_path), True, "registered")

    def _register_toml(self, agent: str, config_path: pathlib.Path, spec: dict) -> dict:
        """Codex: append an ``[mcp_servers.codeintel]`` table to config.toml, preserving everything
        already there (other servers, project trust levels, hooks). Text-based on purpose — it must
        not reformat or risk corrupting a config the user hand-edits, and it stays idempotent by
        checking for the table header. If a codeintel entry already exists it is left untouched."""
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        if spec["table"] in existing:
            return self._result(agent, str(config_path), True, "already")

        config_path.parent.mkdir(parents=True, exist_ok=True)
        if existing == "":
            new_text = spec["block"]
        else:
            prefix = existing if existing.endswith("\n") else existing + "\n"
            new_text = prefix + "\n" + spec["block"]  # blank line before the new table
        _atomic_write_text(config_path, new_text)
        return self._result(agent, str(config_path), True, "registered")

    @staticmethod
    def _result(agent: str, path: str, ok: bool, action: str, reason: str = "") -> dict:
        return {"agent": agent, "path": path, "ok": ok, "action": action, "reason": reason}

    def register_all(self, *, verify: bool = False, timeout_s: float = 45.0) -> list[dict]:
        """Register every supported agent. Verification is a property of the *command*, not of a
        given agent's config file, so it runs ONCE and its verdict is shared across the results."""
        results = [self.register(agent) for agent in _AGENTS]
        if verify:
            verdict = self.verify(timeout_s=timeout_s)
            for r in results:
                if r.get("ok"):
                    r["verified"] = verdict
        return results
