"""End-to-end MCP host tests: does an agent that reads our config actually get working tools?

Every other installer test asserts file *contents*. That is exactly the assertion that stayed
green while `--agent codex` registered into a file Codex never reads, and again while
`--agent claude` registered into `~/.claude/settings.json` (Claude Code reads `~/.claude.json`).

These tests close the loop the only way it can be closed: register into an isolated HOME, read
the command back OUT of the host's own config file, launch it, and complete a real MCP handshake
— `initialize` → `notifications/initialized` → `tools/list` → `tools/call`. A config that names
an unlaunchable command, or a server that answers nothing, fails here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from codeintel.installer import Installer
from codeintel.verify import verify_stdio_server

# Launching a server + importing the engine stack is slow on a cold CI runner.
_TIMEOUT_S = 90.0

# The server as a *module* — always available in the test environment, unlike the console script.
_MODULE_LAUNCH = ([sys.executable, "-m", "codeintel"], ["serve"])

_EXPECTED_TOOLS = {"code.query", "code.status", "code.doctor", "code.map"}


class _Client:
    """Tiny synchronous JSON-RPC-over-stdio client for asserting on real tool calls."""

    def __init__(self, argv: list[str]) -> None:
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._id = 0

    def __enter__(self) -> _Client:
        self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        })
        self._notify("notifications/initialized")
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()

    def _notify(self, method: str) -> None:
        self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self._proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {},
        }) + "\n")
        self._proc.stdin.flush()
        while True:
            line = self._proc.stdout.readline()
            assert line, f"server closed stdout before answering {method}"
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == self._id:
                assert "error" not in msg, f"{method} failed: {msg['error']}"
                return msg.get("result") or {}

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool and return the parsed JSON payload out of its text content block."""
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        assert result.get("isError") is not True, f"{name} reported isError"
        text = "".join(
            part.get("text", "") for part in (result.get("content") or [])
            if isinstance(part, dict)
        )
        return json.loads(text)


# --------------------------------------------------------------------------- protocol

def test_stdio_handshake_exposes_the_documented_tools():
    argv, args = _MODULE_LAUNCH
    res = verify_stdio_server(argv[0], [*argv[1:], *args], timeout_s=_TIMEOUT_S)
    assert res["ok"] is True, res["detail"]
    assert set(res["tools"]) == _EXPECTED_TOOLS
    assert (res["server"] or {}).get("name") == "codeintel"


def test_code_status_over_mcp_reports_the_readiness_tristate(tmp_path):
    argv, args = _MODULE_LAUNCH
    with _Client([*argv, *args]) as client:
        status = client.call_tool("code.status", {"project_root": str(tmp_path)})

    assert status["ok"] is True
    # The readiness contract an agent reasons over: never a bare "available" boolean.
    for engine in ("graph", "lsp", "semantic"):
        entry = status["readiness"][engine]
        assert set(entry) >= {"installed", "runnable", "repo_indexed", "status"}
        assert entry["status"] in ("ok", "warn", "fail")
    assert isinstance(status["healthy"], bool)


def test_code_query_over_mcp_returns_a_safe_envelope(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    argv, args = _MODULE_LAUNCH
    with _Client([*argv, *args]) as client:
        env = client.call_tool("code.query", {
            "op": "callers", "target": "definitely_not_a_symbol", "project_root": str(tmp_path),
        })

    # Never-raise contract, observed through the real transport rather than a direct call.
    assert env["ok"] is True
    assert {"op", "target", "result", "engine", "cached"} <= set(env)
    assert env["result"] is None and env.get("reason")


def test_doctor_over_mcp_never_raises(tmp_path):
    argv, args = _MODULE_LAUNCH
    with _Client([*argv, *args]) as client:
        report = client.call_tool("code.doctor", {"project_root": str(tmp_path)})
    assert report["ok"] is True and set(report["engines"]) == {"graph", "lsp", "semantic"}


# --------------------------------------------------------------------------- host configs
#
# These read the command back OUT of each host's own config file, so a wrong file location or a
# wrong config shape cannot pass.

_needs_console_script = pytest.mark.skipif(
    shutil.which("codeintel") is None,
    reason="the `codeintel` console script is not on PATH in this environment",
)


@_needs_console_script
def test_codex_registered_command_launches_and_handshakes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    assert Installer().register("codex")["ok"]

    config = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    entry = config["mcp_servers"]["codeintel"]          # the table Codex actually reads

    res = verify_stdio_server(entry["command"], list(entry.get("args") or []), timeout_s=_TIMEOUT_S)
    assert res["ok"] is True, res["detail"]
    assert set(res["tools"]) == _EXPECTED_TOOLS


@_needs_console_script
def test_claude_registered_command_launches_and_handshakes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert Installer().register("claude")["ok"]

    # ~/.claude.json — NOT ~/.claude/settings.json, which Claude Code ignores for MCP.
    config = json.loads((tmp_path / ".claude.json").read_text())
    entry = config["mcpServers"]["codeintel"]

    res = verify_stdio_server(entry["command"], list(entry.get("args") or []), timeout_s=_TIMEOUT_S)
    assert res["ok"] is True, res["detail"]
    assert set(res["tools"]) == _EXPECTED_TOOLS


@_needs_console_script
def test_zed_registered_command_launches_and_handshakes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert Installer().register("zed")["ok"]

    config = json.loads((tmp_path / "xdg" / "zed" / "settings.json").read_text())
    # Zed's context_servers entry is FLAT — `command` is a string with `args` beside it, same as
    # every other host. codeintel previously wrote a nested {"command": {"path", "args"}} object,
    # which Zed does not read; this test asserted that shape and so confirmed the bug.
    entry = config["context_servers"]["codeintel"]

    res = verify_stdio_server(entry["command"], list(entry.get("args") or []), timeout_s=_TIMEOUT_S)
    assert res["ok"] is True, res["detail"]


@_needs_console_script
def test_install_verify_flag_reports_a_live_handshake(tmp_path, monkeypatch):
    """The user-visible promise of `codeintel install`: registered AND proven usable."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    res = Installer().register("claude", verify=True, timeout_s=_TIMEOUT_S)

    assert res["ok"] and res["verified"]["ok"] is True
    assert set(res["verified"]["tools"]) == _EXPECTED_TOOLS


def test_paths_are_isolated_from_the_real_home(tmp_path, monkeypatch):
    """Guard the guard: if HOME isolation ever stops working these tests would silently
    rewrite the developer's own agent configs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    for result in Installer().register_all():
        assert Path(result["path"]).is_relative_to(tmp_path)
