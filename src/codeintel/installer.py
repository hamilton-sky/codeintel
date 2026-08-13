from __future__ import annotations

import json
import os
import pathlib

_AGENTS = ["claude", "codex", "gemini", "zed"]


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Write via a sibling temp file + os.replace so an interrupted write can never truncate
    the user's existing agent config (which holds unrelated settings) to a partial/empty file.
    The temp lives in the same directory as ``path`` so the replace stays on one filesystem."""
    tmp = path.with_name(path.name + ".codeintel.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# Per-agent registration recipe. Most agents take a Claude-style JSON block; Codex is different —
# its CLI reads MCP servers from ``~/.codex/config.toml`` as ``[mcp_servers.<name>]`` TOML tables,
# NOT a JSON ``mcpServers`` map in config.json (writing the latter registers nothing).
_CONFIG: dict[str, dict] = {
    "claude": {
        "format": "json",
        "path": "~/.claude/settings.json",
        "key": ["mcpServers", "codeintel"],
        "value": {"command": "codeintel", "args": ["serve"]},
    },
    "codex": {
        "format": "toml-mcp",
        "path": "~/.codex/config.toml",
        "table": "[mcp_servers.codeintel]",
        "block": '[mcp_servers.codeintel]\ncommand = "codeintel"\nargs = ["serve"]\n',
    },
    "gemini": {
        "format": "json",
        "path": "~/.gemini/settings.json",
        "key": ["mcpServers", "codeintel"],
        "value": {"command": "codeintel", "args": ["serve"]},
    },
    "zed": {
        "format": "json",
        "path": "~/.config/zed/settings.json",
        "key": ["context_servers", "codeintel"],
        "value": {"command": {"path": "codeintel", "args": ["serve"]}},
    },
}


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
    def register(self, agent: str) -> dict:
        spec = _CONFIG.get(agent)
        if spec is None:
            return self._result(agent, "", False, "failed", f"unknown agent '{agent}'")
        config_path = pathlib.Path(spec["path"]).expanduser()
        try:
            if spec.get("format") == "toml-mcp":
                return self._register_toml(agent, config_path, spec)
            return self._register_json(agent, config_path, spec)
        except Exception as exc:
            return self._result(agent, str(config_path), False, "failed", str(exc))

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

    def register_all(self) -> list[dict]:
        return [self.register(agent) for agent in _AGENTS]
