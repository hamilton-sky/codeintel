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

``verify_stdio_call`` goes one step further and actually INVOKES a tool, returning its response
text. That step is what a release gate needs: every codeintel result is a safe envelope whose
``ok`` is always True, so a check that stops at the envelope passes against a completely inert
build. Only the returned content separates a working release from a broken one.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any

# The stdio transport is newline-delimited JSON-RPC (no Content-Length framing).
_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "codeintel-verify", "version": "1"}


def _pump(pipe: Any, sink: queue.Queue[str | None]) -> None:
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
        self._q: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=_pump, args=(proc.stdout, self._q), daemon=True).start()

    def send(self, payload: dict) -> bool:
        stdin = self._proc.stdin
        if stdin is None:                      # not spawned with stdin=PIPE — nothing to write to
            return False
        try:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()
            return True
        except Exception:
            return False

    def await_id(self, want_id: int) -> dict | None:
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


def _spawn(argv: list, env: dict | None, cwd: str | None) -> tuple:
    """Start the server exactly as a host would. Returns ``(proc, None)`` or ``(None, failure)``."""
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
        return proc, None
    except FileNotFoundError:
        return None, _result(
            False,
            f"`{argv[0]}` is not on PATH — the agent will fail to start it. "
            f"Install codeintel so `{argv[0]}` resolves (e.g. `pipx install codecortex`), "
            f"or register an absolute path.",
        )
    except Exception as exc:
        return None, _result(False, f"could not launch `{argv[0]}` ({type(exc).__name__})")


def _reap(proc: subprocess.Popen | None) -> None:
    """Terminate, then kill, then close the pipes. Never raises."""
    if proc is None:
        return
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


def _handshake(conn: _Conn, proc: subprocess.Popen, timeout_s: float) -> tuple:
    """``initialize`` → ``notifications/initialized`` → ``tools/list``.

    Returns ``(server_info, tools, None)`` on success or ``(server_info, [], failure)``."""
    if not conn.send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": _CLIENT_INFO},
    }):
        return None, [], _result(False, "server closed stdin before the handshake started")

    init = conn.await_id(1)
    if init is None:
        if proc.poll() is not None:
            return None, [], _result(False, f"process exited immediately (code {proc.returncode})")
        return None, [], _result(False, f"no `initialize` response within {timeout_s:.0f}s")
    if "error" in init:
        return None, [], _result(False, f"initialize failed: {str(init.get('error'))[:160]}")

    server_info = (init.get("result") or {}).get("serverInfo")

    # Some servers gate tools/list on the initialized notification, per spec.
    conn.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    if not conn.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
        return server_info, [], _result(False, "server closed stdin before tools/list",
                                        server=server_info)

    listed = conn.await_id(2)
    if listed is None:
        return server_info, [], _result(False, f"no `tools/list` response within {timeout_s:.0f}s",
                                        server=server_info)
    if "error" in listed:
        return server_info, [], _result(False, f"tools/list failed: {str(listed.get('error'))[:160]}",
                                        server=server_info)

    tools = [
        str(t.get("name"))
        for t in ((listed.get("result") or {}).get("tools") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    if not tools:
        return server_info, [], _result(False, "handshake succeeded but the server exposes no tools",
                                        server=server_info)
    return server_info, tools, None


def verify_stdio_server(
    command: str,
    args: list[str] | None = None,
    *,
    timeout_s: float = 45.0,
    env: dict | None = None,
    cwd: str | None = None,
) -> dict:
    """Launch ``command args`` and complete an MCP handshake. Never raises.

    Returns ``{ok, tools, server, detail}`` — ``ok`` is True only when the process started,
    answered ``initialize``, and returned a non-empty ``tools/list``."""
    argv = [command, *(args or [])]
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    proc: subprocess.Popen | None = None
    try:
        proc, failure = _spawn(argv, env, cwd)
        if failure is not None:
            return failure

        server_info, tools, failure = _handshake(_Conn(proc, deadline), proc, timeout_s)
        if failure is not None:
            return failure

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
        _reap(proc)


def _extract_text(payload: Any) -> str:
    """Flatten an MCP ``tools/call`` result to text.

    A tool may answer as ``content: [{type: "text", text: ...}]``, as ``structuredContent``, or —
    for codeintel, whose handlers return a plain dict — as the raw JSON object. Join whatever is
    there so a caller can assert on the ANSWER rather than on the envelope."""
    parts: list[str] = []
    try:
        if isinstance(payload, dict):
            blocks = payload.get("content")
            parts.extend(str(block.get("text", "")) for block in blocks or []
                         if isinstance(block, dict) and block.get("type") == "text")
            structured = payload.get("structuredContent")
            if structured is not None:
                parts.append(json.dumps(structured))
            # Fall back to the raw object only when there IS an answer we just can't read as text
            # — a plain dict with no `content` key, or non-text blocks like an image. An explicitly
            # EMPTY `content` list means the server answered nothing, and dumping the envelope
            # there would turn "returned nothing" into a passing non-empty body.
            if not parts and blocks != []:
                parts.append(json.dumps(payload))
        elif payload is not None:
            parts.append(str(payload))
    except Exception:
        return ""
    return "\n".join(p for p in parts if p)


def verify_stdio_call(
    command: str,
    args: list[str] | None = None,
    *,
    tool: str,
    arguments: dict | None = None,
    timeout_s: float = 90.0,
    env: dict | None = None,
    cwd: str | None = None,
) -> dict:
    """Handshake, then actually CALL ``tool`` and return its response text. Never raises.

    ``verify_stdio_server`` stops at ``tools/list``, which proves the process boots but not that a
    query answers. Because every codeintel result is a safe envelope with ``ok: True``, that gap is
    exactly where an inert build passes a green check — so a release gate has to read the content.

    Returns ``{ok, tools, server, detail, content, payload}``. ``ok`` means the call completed and
    returned a non-empty body; the CALLER still has to assert that ``content`` says what it should.
    """
    argv = [command, *(args or [])]
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    proc: subprocess.Popen | None = None
    try:
        proc, failure = _spawn(argv, env, cwd)
        if failure is not None:
            return {**failure, "content": "", "payload": None}

        conn = _Conn(proc, deadline)
        server_info, tools, failure = _handshake(conn, proc, timeout_s)
        if failure is not None:
            return {**failure, "content": "", "payload": None}

        if tool not in tools:
            return _result(False, f"server does not expose `{tool}` (has: {', '.join(tools)})",
                           server=server_info, content="", payload=None)

        if not conn.send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }):
            return _result(False, f"server closed stdin before calling `{tool}`",
                           server=server_info, content="", payload=None)

        called = conn.await_id(3)
        if called is None:
            return _result(False, f"no `{tool}` response within {timeout_s:.0f}s",
                           server=server_info, content="", payload=None)
        if "error" in called:
            return _result(False, f"`{tool}` failed: {str(called.get('error'))[:160]}",
                           server=server_info, content="", payload=None)

        payload = called.get("result")
        content = _extract_text(payload)
        if not content.strip():
            return _result(False, f"`{tool}` returned an empty body",
                           server=server_info, content="", payload=payload)

        return {
            "ok": True,
            "tools": tools,
            "server": server_info,
            "detail": f"`{tool}` answered with {len(content)} chars",
            "content": content,
            "payload": payload,
        }
    except Exception as exc:
        return {**_result(False, f"tool-call error ({type(exc).__name__})"),
                "content": "", "payload": None}
    finally:
        _reap(proc)
