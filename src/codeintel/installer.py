from __future__ import annotations

import json
import pathlib

_AGENTS = ["claude", "codex", "gemini", "zed"]

_CONFIG: dict[str, dict] = {
    "claude": {
        "path": "~/.claude/settings.json",
        "key": ["mcpServers", "codeintel"],
        "value": {"command": "codeintel", "args": ["serve"]},
    },
    "codex": {
        "path": "~/.codex/config.json",
        "key": ["mcpServers", "codeintel"],
        "value": {"command": "codeintel", "args": ["serve"]},
    },
    "gemini": {
        "path": "~/.gemini/settings.json",
        "key": ["mcpServers", "codeintel"],
        "value": {"command": "codeintel", "args": ["serve"]},
    },
    "zed": {
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
            return {
                "agent": agent,
                "path": "",
                "ok": False,
                "action": "failed",
                "reason": f"unknown agent '{agent}'",
            }

        config_path = pathlib.Path(spec["path"]).expanduser()
        try:
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}

            current = _get_nested(data, spec["key"])
            if current == spec["value"]:
                return {
                    "agent": agent,
                    "path": str(config_path),
                    "ok": True,
                    "action": "already",
                    "reason": "",
                }

            config_path.parent.mkdir(parents=True, exist_ok=True)
            _set_nested(data, spec["key"], spec["value"])
            config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            return {
                "agent": agent,
                "path": str(config_path),
                "ok": True,
                "action": "registered",
                "reason": "",
            }

        except Exception as exc:
            return {
                "agent": agent,
                "path": str(config_path),
                "ok": False,
                "action": "failed",
                "reason": str(exc),
            }

    def register_all(self) -> list[dict]:
        return [self.register(agent) for agent in _AGENTS]
