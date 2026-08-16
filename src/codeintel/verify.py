"""Post-registration verification — prove an agent host can actually *launch* codeintel.

Writing a config file only proves the file is well-formed. It does not prove the host will find
the command, that the process starts, or that it speaks MCP. That gap is exactly how
``--agent codex`` once shipped registering into a file Codex never reads, and how ``--agent
claude`` shipped registering into ``~/.claude/settings.json`` (Claude Code reads ``~/.claude.json``)
— both with green tests, because the tests asserted file *contents*.

``verify_stdio_server`` closes it: it spawns the registered command exactly as the host would and
drives a real MCP handshake over stdio — ``initialize`` → ``notifications/initialized`` →
``tools/list`` — then reports the tool names it got back. Never raises; hard-bounded by a deadline;
always reaps the subprocess.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Optional

# The stdio transport is newline-delimited JSON-RPC (no Content-Length framing).
_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "codeintel-verify", "version": "1"}


def _pump(pipe: Any, sink: "queue.Queue[Optional[str]]") -> None:
    """Drain a pipe into a queue on a daemon thread so reads can honor a deadline."""
    try:
        for line in pipe:
            sink.put(line)
    except Exception:
        pass
    finally:
        sink.put(None)  # sentinel: stream closed


class _Conn:
    """Minimal never-raise JSON-RPC-over-stdio client with a shared deadline."""

    def __init__(self, proc: subprocess.Popen, deadline: float) -> None:
        self._proc = proc
        self._deadline = deadline
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=_pump, args=(proc.stdout, self._q), daemon=True).start()

    def send(self, payload: dict) -> bool:
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            return True
        except Exception:
            return False

    def await_id(self, want_id: int) -> Optional[dict]:
        """Read until the response with ``want_id`` arrives, skipping notifications and any
        interleaved traffic. Returns None on timeout, stream close, or unparseable output."""
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:  # server closed stdout / died
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue  # a server that pollutes stdout with non-JSON: skip, don't fail yet
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg


def _result(ok: bool, detail: str, **extra: Any) -> dict:
    return {"ok": ok, "tools": [], "server": None, "detail": detail, **extra}


def verify_stdio_server(
    command: str,
    args: Optional[list[str]] = None,
    *,
    timeout_s: float = 45.0,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Launch ``command args`` and complete an MCP handshake. Never raises.

    Returns ``{ok, tools, server, detail}`` — ``ok`` is True only when the process started,
    answered ``initialize``, and returned a non-empty ``tools/list``."""
    argv = [command, *(args or [])]
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    proc: Optional[subprocess.Popen] = None
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # servers log to stderr; it is not the protocol channel
                text=True,
                bufsize=1,
                env=env,
                cwd=cwd,
            )
        except FileNotFoundError:
            return _result(
                False,
                f"`{command}` is not on PATH — the agent will fail to start it. "
                f"Install codeintel so `{command}` resolves (e.g. `pipx install codecortex`), "
                f"or register an absolute path.",
            )
        except Exception as exc:
            return _result(False, f"could not launch `{command}` ({type(exc).__name__})")

        conn = _Conn(proc, deadline)

        if not conn.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": _CLIENT_INFO},
        }):
            return _result(False, "server closed stdin before the handshake started")

        init = conn.await_id(1)
        if init is None:
            if proc.poll() is not None:
                return _result(False, f"process exited immediately (code {proc.returncode})")
            return _result(False, f"no `initialize` response within {timeout_s:.0f}s")
        if "error" in init:
            return _result(False, f"initialize failed: {str(init.get('error'))[:160]}")

        server_info = (init.get("result") or {}).get("serverInfo")

        # Some servers gate tools/list on the initialized notification, per spec.
        conn.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        if not conn.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
            return _result(False, "server closed stdin before tools/list", server=server_info)

        listed = conn.await_id(2)
        if listed is None:
            return _result(False, f"no `tools/list` response within {timeout_s:.0f}s",
                           server=server_info)
        if "error" in listed:
            return _result(False, f"tools/list failed: {str(listed.get('error'))[:160]}",
                           server=server_info)

        tools = [
            str(t.get("name"))
            for t in ((listed.get("result") or {}).get("tools") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        if not tools:
            return _result(False, "handshake succeeded but the server exposes no tools",
                           server=server_info)

        name = (server_info or {}).get("name") or command
        version = (server_info or {}).get("version") or "?"
        return {
            "ok": True,
            "tools": tools,
            "server": server_info,
            "detail": f"{name} {version} — {len(tools)} tools ({', '.join(tools)})",
        }
    except Exception as exc:
        return _result(False, f"verification error ({type(exc).__name__})")
    finally:
        if proc is not None:
            for step in (proc.terminate, proc.kill):
                try:
                    if proc.poll() is None:
                        step()
                        proc.wait(timeout=5)
                except Exception:
                    continue
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
