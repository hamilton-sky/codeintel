from __future__ import annotations

import json
import os
import pathlib
import shutil

_AGENTS = ["claude", "codex", "gemini", "zed"]

_SERVER_NAME = "codeintel"
_COMMAND = "codeintel"
_ARGS = ["serve"]


def resolve_command(*, absolute: bool = True) -> str:
    """The command to write into an agent config.

    Defaults to the ABSOLUTE path of `codeintel` because the bare name is resolved by the host,
    not by the shell that ran `codeintel install` — and a GUI-launched desktop agent does not
    source your shell profile, so a `codeintel` that a terminal finds on PATH is routinely
    invisible to the app. Verification runs in THIS process's environment, so it would happily
    confirm a command the real host cannot launch: the one failure the verifier is blind to.

    Falls back to the bare name when the command is not on PATH (an editable/source checkout that
    has not been installed yet), and `--relative-command` forces it — the cost of an absolute path
    is that reinstalling into a different venv moves the binary, which `codeintel doctor` reports.
    """
    if not absolute:
        return _COMMAND
    return shutil.which(_COMMAND) or _COMMAND


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
# Host quirks the file layout has to respect. Every one of these shipped BROKEN first, each time
# with green tests, because the tests asserted the bytes we wrote rather than what the host reads:
#   * Codex reads MCP servers from `config.toml` as `[mcp_servers.<name>]` TOML tables — NOT a
#     JSON `mcpServers` map in config.json.
#   * Claude Code reads user-scope MCP servers from `~/.claude.json` — NOT `~/.claude/settings.json`
#     (that file is for hooks/theme/permissions; an `mcpServers` block there is silently ignored,
#     which `claude mcp list` confirms).
#   * Zed's `context_servers` entry is FLAT — `command` is a string with `args` beside it. codeintel
#     shipped the nested `{"command": {"path", "args"}}` form, which current Zed does not read.
#     Corrected against Zed's published MCP docs (checked 2026-08-16); unlike Codex and Claude this
#     one has not been confirmed against a running Zed, which is why `auto` will not register a host
#     you don't have.
#
# `detect` lists paths whose existence is evidence the host is actually installed — what `--agent
# auto` registers. Checked against the agent's own home env var first, same as `resolve_config_path`.
_CONFIG: dict[str, dict] = {
    "claude": {
        "format": "json",
        "env": "CLAUDE_CONFIG_DIR",
        "home": "~",
        "file": ".claude.json",
        "key": ["mcpServers", _SERVER_NAME],
        "detect": [".claude.json", ".claude"],
        # Where a pre-0.11.2 codeintel wrote — inert, reported so the user can delete it.
        "legacy": {"file": ".claude/settings.json", "key": ["mcpServers", _SERVER_NAME]},
    },
    "codex": {
        "format": "toml-mcp",
        "env": "CODEX_HOME",
        "home": "~/.codex",
        "file": "config.toml",
        "table": f"[mcp_servers.{_SERVER_NAME}]",
        "detect": ["."],
    },
    "gemini": {
        "format": "json",
        "env": "GEMINI_CONFIG_DIR",
        "home": "~/.gemini",
        "file": "settings.json",
        "key": ["mcpServers", _SERVER_NAME],
        "detect": ["."],
    },
    "zed": {
        "format": "json",
        "env": "XDG_CONFIG_HOME",
        "home": "~/.config",
        "file": "zed/settings.json",
        "key": ["context_servers", _SERVER_NAME],
        "detect": ["zed"],
    },
}


def launch_value(command: str) -> dict:
    """The JSON launch block. Every supported host uses the same flat shape — a `command` string
    with `args` beside it; only the containing key differs (`mcpServers` vs Zed's
    `context_servers`)."""
    return {"command": command, "args": list(_ARGS)}


def launch_block_toml(command: str) -> str:
    """Codex's TOML table. `command` is JSON-encoded so a Windows path's backslashes survive."""
    return (f"[mcp_servers.{_SERVER_NAME}]\n"
            f"command = {json.dumps(command)}\n"
            f"args = {json.dumps(_ARGS)}\n")


def registered_command(spec: dict) -> tuple[str, str | None]:
    """The launch command currently in an agent's config, or ``(path, None)`` if not registered.

    Read-only — this is what `codeintel doctor` uses to notice that a registered absolute path has
    gone stale (an upgrade or rebuilt venv moved the binary)."""
    path = resolve_config_path(spec)
    try:
        if not path.exists():
            return str(path), None
        text = path.read_text(encoding="utf-8")
        if spec.get("format") == "toml-mcp":
            table = spec["table"]
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.strip() != table:
                    continue
                for follow in lines[i + 1:]:
                    if follow.startswith("["):
                        break
                    key, _, raw = follow.partition("=")
                    if key.strip() == "command":
                        return str(path), json.loads(raw.strip())
            return str(path), None
        entry = _get_nested(json.loads(text), spec["key"])
        if isinstance(entry, dict) and isinstance(entry.get("command"), str):
            return str(path), entry["command"]
    except Exception:
        return str(path), None
    return str(path), None


def detect_agents() -> list[str]:
    """Agents this machine actually has, judged by their config root existing.

    `--agent all` writing four config files means a default install creates `~/.gemini/` and
    `~/.config/zed/` for people who have neither — reaching into another app's namespace uninvited,
    and (worse) claiming coverage of hosts nobody verified. Detection keeps the default honest:
    register what is here, name what was skipped."""
    found: list[str] = []
    for agent in _AGENTS:
        spec = _CONFIG[agent]
        try:
            root = _resolve_home(spec)
            if any((root / rel).exists() for rel in spec.get("detect", ["."])):
                found.append(agent)
        except Exception:
            continue
    return found


def _resolve_home(spec: dict) -> pathlib.Path:
    """The agent's config ROOT, honoring its own home env var when set and non-empty."""
    raw = os.environ.get(spec.get("env") or "", "").strip()
    return pathlib.Path(raw).expanduser() if raw else pathlib.Path(spec["home"]).expanduser()


def resolve_config_path(spec: dict) -> pathlib.Path:
    """Agent config path, honoring the agent's own home env var when it is set and non-empty.

    Resolved at call time (not import time) so tests and shells that export CODEX_HOME /
    CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME after import still get the right file."""
    return _resolve_home(spec) / spec["file"]


def _replace_toml_table(text: str, header: str, block: str) -> str | None:
    """Swap ONLY the ``header`` table for ``block``, leaving every other byte of the file alone.

    Needed because a stale absolute command (an upgrade moved the binary) must be repairable by
    re-running install — but the file is hand-maintained and holds unrelated Codex settings, so
    this returns None whenever the table cannot be located unambiguously. Leaving a stale entry the
    user can see beats rewriting a config we did not fully parse."""
    try:
        lines = text.splitlines(keepends=True)
        start = None
        for i, line in enumerate(lines):
            if line.strip() == header:
                if start is not None:
                    return None  # duplicate tables — ambiguous, don't guess
                start = i
        if start is None:
            return None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("["):  # next table header at column 0
                end = j
                break
        new_block = block if block.endswith("\n") else block + "\n"
        if end < len(lines) and lines[end].startswith("["):
            new_block += "\n"  # keep the blank line that separated the tables
        return "".join(lines[:start]) + new_block + "".join(lines[end:])
    except Exception:
        return None


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
    def register(self, agent: str, *, verify: bool = False, timeout_s: float = 45.0,
                 absolute: bool = True) -> dict:
        """Register codeintel with *agent*.

        With ``verify=True`` the registered command is then launched exactly as the host would
        and driven through a real MCP handshake — so a "registered" result means the agent can
        actually use it, not merely that a file was written."""
        spec = _CONFIG.get(agent)
        if spec is None:
            return self._result(agent, "", False, "failed", f"unknown agent '{agent}'")
        config_path = resolve_config_path(spec)
        command = resolve_command(absolute=absolute)
        try:
            if spec.get("format") == "toml-mcp":
                res = self._register_toml(agent, config_path, spec, command)
            else:
                res = self._register_json(agent, config_path, spec, command)
        except Exception as exc:
            return self._result(agent, str(config_path), False, "failed", str(exc))

        res["command"] = command
        res["legacy"] = self._legacy_note(spec)
        if verify and res.get("ok"):
            res["verified"] = self.verify(timeout_s=timeout_s, command=command)
        return res

    @staticmethod
    def verify(*, timeout_s: float = 45.0, command: str | None = None) -> dict:
        """Launch the registered command and complete an MCP handshake. Never raises.

        Takes the command actually written to the config so verification proves THAT argv, not a
        differently-resolved one — the point of the check is that the host's launch line works."""
        try:
            from codeintel.verify import verify_stdio_server
            return verify_stdio_server(
                command or resolve_command(), list(_ARGS), timeout_s=timeout_s
            )
        except Exception as exc:
            return {"ok": False, "tools": [], "server": None,
                    "detail": f"verification unavailable ({type(exc).__name__})"}

    @staticmethod
    def _legacy_note(spec: dict) -> str | None:
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

    def _register_json(self, agent: str, config_path: pathlib.Path, spec: dict,
                       command: str) -> dict:
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}

        value = launch_value(command)
        if _get_nested(data, spec["key"]) == value:
            return self._result(agent, str(config_path), True, "already")

        config_path.parent.mkdir(parents=True, exist_ok=True)
        _set_nested(data, spec["key"], value)
        _atomic_write_text(config_path, json.dumps(data, indent=2))
        return self._result(agent, str(config_path), True, "registered")

    def _register_toml(self, agent: str, config_path: pathlib.Path, spec: dict,
                       command: str) -> dict:
        """Codex: append an ``[mcp_servers.codeintel]`` table to config.toml, preserving everything
        already there (other servers, project trust levels, hooks). Text-based on purpose — it must
        not reformat or risk corrupting a config the user hand-edits, and it stays idempotent by
        checking for the table header. If a codeintel entry already exists it is left untouched."""
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        block = launch_block_toml(command)
        if spec["table"] in existing:
            # Already registered. Rewrite ONLY when the command line drifted (e.g. an upgrade moved
            # the binary and the old absolute path is now dead), and only our own table.
            if block.strip() in existing:
                return self._result(agent, str(config_path), True, "already")
            replaced = _replace_toml_table(existing, spec["table"], block)
            if replaced is None:
                return self._result(agent, str(config_path), True, "already")
            _atomic_write_text(config_path, replaced)
            return self._result(agent, str(config_path), True, "registered")

        config_path.parent.mkdir(parents=True, exist_ok=True)
        if existing == "":
            new_text = block
        else:
            prefix = existing if existing.endswith("\n") else existing + "\n"
            new_text = prefix + "\n" + block  # blank line before the new table
        _atomic_write_text(config_path, new_text)
        return self._result(agent, str(config_path), True, "registered")

    @staticmethod
    def _result(agent: str, path: str, ok: bool, action: str, reason: str = "") -> dict:
        return {"agent": agent, "path": path, "ok": ok, "action": action, "reason": reason}

    def register_many(self, agents: list[str], *, verify: bool = False, timeout_s: float = 45.0,
                      absolute: bool = True) -> list[dict]:
        """Register a set of agents. Verification is a property of the *command*, not of a given
        agent's config file, so it runs ONCE and its verdict is shared across the results."""
        command = resolve_command(absolute=absolute)
        results = [self.register(agent, absolute=absolute) for agent in agents]
        if verify and results:
            verdict = self.verify(timeout_s=timeout_s, command=command)
            for r in results:
                if r.get("ok"):
                    r["verified"] = verdict
        return results

    def register_all(self, *, verify: bool = False, timeout_s: float = 45.0,
                     absolute: bool = True) -> list[dict]:
        """Register every supported agent, installed or not (`--agent all`)."""
        return self.register_many(list(_AGENTS), verify=verify, timeout_s=timeout_s,
                                  absolute=absolute)

    def register_detected(self, *, verify: bool = False, timeout_s: float = 45.0,
                          absolute: bool = True) -> tuple[list[dict], list[str]]:
        """Register only the agents this machine actually has (`--agent auto`, the default).

        Returns ``(results, skipped)`` so the CLI can name what it did NOT touch — silence about a
        skipped host reads as "unsupported" rather than "not installed here"."""
        detected = detect_agents()
        skipped = [a for a in _AGENTS if a not in detected]
        return (self.register_many(detected, verify=verify, timeout_s=timeout_s,
                                   absolute=absolute), skipped)
