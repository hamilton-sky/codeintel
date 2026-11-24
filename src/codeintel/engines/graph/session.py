"""A long-lived codebase-memory-mcp process, so a graph query does not pay a cold start.

Every graph call used to launch `codebase-memory-mcp cli <method>` as a fresh process. Measured on
0.10.8 (Linux cloud host, this repository indexed): 1.5 to 4.8 s per launch, against a default
budget of 5 s, and one `callers` answer makes several calls. Starting the backend's own warm daemon
(`codebase-memory-mcp daemon start`) does not help: each `cli` launch still cost about 1.7 s. The
same backend run as an MCP server on stdio answers the same call in about 0.03 s once it is up, and
its reply text is byte-for-byte what `cli` prints (one trailing newline aside), so the existing
decoder reads it unchanged. It also sees an external reindex without a restart (measured: a
function added and reindexed through `cli` appeared on the next call of a session opened before).

So a process keeps ONE such server per (binary, cache location) and sends read-only calls to it.
The rules that keep this from changing any answer:

* **Opening it never delays a call.** The server starts on a background thread the first time it
  is wanted (opening it was measured at 0.9 to 4.8 s), and until it is ready every call takes the
  one-shot path exactly as before. So the first call of a process is never slower than it was, and
  a budget is never spent waiting for a server that is still starting.
* **Read-only methods only** (`WARM_METHODS`). `index_repository` runs for minutes and would hold
  the session; it and anything else that writes keep using `cli`.
* **Anything unusual falls back to `cli`**: a reply that is an MCP error (`isError`; the CLI reports
  those as a non-zero exit with its own wording, which the failure taxonomy in `backend.py` reads),
  a closed pipe, a malformed reply. `call` returns None and the caller runs the one-shot path with
  whatever budget is left, so the worst case is the old behaviour, never a different answer.
* **A timeout kills the server.** The in-flight request's state is unknown, and a late reply must
  never be read as the answer to the next request. The next call starts a new one.
* **A server that fails to start is not retried for `_START_COOLDOWN_S`**, so a broken install
  costs one failed start, not one per call.
* **One request at a time.** Concurrent callers (`serve-http`) wait for the lock only as long as
  their own budget allows, then go to `cli` in parallel.
* The key includes the cache-location environment variables, because a server started for one
  cache would otherwise answer a caller who pointed the backend somewhere else.

Off with ``CODEINTEL_GRAPH_WARM=0``.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time

WARM_ENV = "CODEINTEL_GRAPH_WARM"

# The calls codeintel makes that only read the graph. Anything else goes to the one-shot CLI.
WARM_METHODS = frozenset({
    "query_graph", "search_graph", "search_code", "list_projects", "get_architecture",
    "trace_path", "get_code_snippet",
})

# How long the MCP handshake may take before the server is given up on.
_START_TIMEOUT_S = 30.0
_START_COOLDOWN_S = 60.0
_PROTOCOL = "2025-03-26"

# The environment variables that decide which cache the backend reads. Part of the session key.
_CACHE_ENV = ("CODEBASE_MEMORY_HOME", "CODEBASE_MEMORY_CACHE_DIR", "CBM_CACHE_DIR", "HOME",
              "XDG_CACHE_HOME")


def warm_enabled() -> bool:
    return os.environ.get(WARM_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


class _Closed(Exception):
    pass


class WarmSession:
    """One `codebase-memory-mcp` stdio MCP server. Every public method never raises."""

    def __init__(self, cmd: str) -> None:
        self.cmd = cmd
        # Held for one request/reply exchange, and for the handshake.
        self._lock = threading.Lock()
        # Guards `_starting` / `_failed_at` / `_closed`.
        self._state = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._ready = False
        self._starting = False
        self._closed = False
        self._replies: queue.Queue = queue.Queue()
        self._next_id = 0
        self._failed_at: float | None = None
        # Measured, for the doctor and the default budget: how long the last start took, and the
        # last completed call's round trip.
        self.start_ms: int | None = None
        self.call_ms: int | None = None

    # -- lifecycle --------------------------------------------------------------------------- #

    def alive(self) -> bool:
        proc = self._proc
        return self._ready and proc is not None and proc.poll() is None

    def _reader(self, proc: subprocess.Popen, replies: queue.Queue) -> None:
        try:
            for line in proc.stdout or ():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    replies.put(msg)
        except Exception:
            pass
        replies.put(None)  # the pipe closed: wake any waiter

    def _kill(self) -> None:
        self._ready = False
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

    def _send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise _Closed
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

    def _await(self, msg_id: int, deadline: float) -> dict:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                msg = self._replies.get(timeout=left)
            except queue.Empty:
                raise TimeoutError from None
            if msg is None:
                raise _Closed
            if msg.get("id") == msg_id:
                return msg

    def ensure_starting(self) -> None:
        """Start the server on a background thread, unless it is up, starting, or cooling down."""
        with self._state:
            if self._closed or self._starting or self.alive():
                return
            if (self._failed_at is not None
                    and time.monotonic() - self._failed_at < _START_COOLDOWN_S):
                return
            self._starting = True
        try:
            threading.Thread(target=self._start, name="codeintel-graph-session-start",
                             daemon=True).start()
        except Exception:
            with self._state:
                self._starting = False

    def _start(self) -> None:
        ok = False
        began = time.monotonic()
        try:
            with self._lock:
                self._replies = queue.Queue()
                self._proc = subprocess.Popen(
                    [self.cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
                threading.Thread(target=self._reader, args=(self._proc, self._replies),
                                 name="codeintel-graph-session", daemon=True).start()
                self._next_id += 1
                init_id = self._next_id
                self._send({"jsonrpc": "2.0", "id": init_id, "method": "initialize", "params": {
                    "protocolVersion": _PROTOCOL, "capabilities": {},
                    "clientInfo": {"name": "codeintel", "version": "1"}}})
                reply = self._await(init_id, began + _START_TIMEOUT_S)
                if "result" not in reply:
                    raise _Closed
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                with self._state:
                    if self._closed:
                        raise _Closed
                    self._ready = True
                ok = True
        except Exception:
            self._kill()
        finally:
            with self._state:
                self._starting = False
                self._failed_at = None if ok else time.monotonic()
        if ok:
            self.start_ms = int((time.monotonic() - began) * 1000)
            _note_start(self.start_ms)

    def close(self) -> None:
        """Stop the server. Does not wait for a start in progress: that start sees `_closed`."""
        with self._state:
            self._closed = True
        self._kill()

    def wait_ready(self, timeout_s: float) -> bool:
        """Block until the server is up, or *timeout_s* passes. For the doctor and tests."""
        self.ensure_starting()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.alive():
                return True
            with self._state:
                if not self._starting:
                    return self.alive()
            time.sleep(0.02)
        return self.alive()

    # -- calls ------------------------------------------------------------------------------- #

    def call(self, method: str, payload: dict, timeout_ms: int) -> bytes | None:
        """The reply text as `cli` would print it, or None to say "use the one-shot path".

        None never means "the backend answered nothing": that is an empty text, returned as is."""
        if not self.alive():
            self.ensure_starting()
            return None
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return None
        try:
            if not self.alive():
                return None
            self._next_id += 1
            msg_id = self._next_id
            began = time.monotonic()
            try:
                self._send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                            "params": {"name": method, "arguments": payload}})
                reply = self._await(msg_id, deadline)
            except Exception:
                # Timed out or the pipe broke: the request's fate is unknown, so this server is done.
                self._kill()
                return None
            result = reply.get("result")
            if not isinstance(result, dict) or result.get("isError"):
                return None
            content = result.get("content")
            if not isinstance(content, list) or not content:
                return None
            first = content[0]
            if not isinstance(first, dict) or first.get("type") != "text":
                return None
            text = first.get("text")
            if not isinstance(text, str):
                return None
            self.call_ms = int((time.monotonic() - began) * 1000)
            return (text if text.endswith("\n") else text + "\n").encode()
        except Exception:
            return None
        finally:
            self._lock.release()


_slowest_start_ms: int | None = None
_start_lock = threading.Lock()


def _note_start(ms: int) -> None:
    global _slowest_start_ms
    with _start_lock:
        _slowest_start_ms = ms if _slowest_start_ms is None else max(_slowest_start_ms, ms)


def slowest_start_ms() -> int | None:
    """The slowest session start measured in this process, or None."""
    with _start_lock:
        return _slowest_start_ms


def _reset_measurements() -> None:
    global _slowest_start_ms
    with _start_lock:
        _slowest_start_ms = None


_sessions: dict[tuple, WarmSession] = {}
_sessions_lock = threading.Lock()


def _key(cmd: str) -> tuple:
    return (cmd, *(os.environ.get(name, "") for name in _CACHE_ENV))


def session_for(cmd: str) -> WarmSession:
    key = _key(cmd)
    with _sessions_lock:
        s = _sessions.get(key)
        if s is None:
            s = _sessions[key] = WarmSession(cmd)
        return s


def existing_session(cmd: str) -> WarmSession | None:
    with _sessions_lock:
        return _sessions.get(_key(cmd))


def is_open(cmd: str) -> bool:
    """Whether this process already has a running session for *cmd* (under the current cache)."""
    s = existing_session(cmd)
    return s is not None and s.alive()


def close_all() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for s in sessions:
        s.close()


def warm_call(cmd: str | None, method: str, payload: dict, timeout_ms: int) -> bytes | None:
    """The warm path for one call: reply bytes, or None for "use the one-shot path"."""
    if not cmd or method not in WARM_METHODS or not warm_enabled():
        return None
    return session_for(cmd).call(method, payload, timeout_ms)


atexit.register(close_all)
