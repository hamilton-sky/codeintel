from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codeintel.server import code_doctor_handler, code_query_handler, code_status_handler

_MAX_BODY_BYTES = 1_048_576  # 1 MiB


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default stderr noise

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in ("/code/query", "/code/doctor"):
            self._send_json(404, {"error": "not-found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "bad-request"})
            return
        if content_length > _MAX_BODY_BYTES:
            self.close_connection = True          # do not read the oversized body
            self._send_json(413, {"error": "payload-too-large", "max_bytes": _MAX_BODY_BYTES})
            return
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "bad-request"})
            return
        if not isinstance(parsed, dict):
            self._send_json(400, {"error": "bad-request"})
            return
        if self.path == "/code/doctor":
            result = code_doctor_handler(parsed)
        else:
            result = code_query_handler(parsed)
        self._send_json(200, result)

    def do_GET(self) -> None:
        if self.path != "/code/status":
            self._send_json(404, {"error": "not-found"})
            return
        result = code_status_handler({})
        self._send_json(200, result)


class CodeIntelHTTPServer(ThreadingHTTPServer):
    # Threaded so one slow request (e.g. an LSP session warming, or a first-time index) can't
    # block every other agent's query. The gateway is a shared singleton, but its mutable state
    # is lock-guarded (query cache, reindexer, LSP sessions, graph project cache) and the
    # semantic engine is thread-confined with WAL, so concurrent requests are safe.
    daemon_threads = True


_LOOPBACK_NAMES = {"localhost"}


def _is_loopback(host: str) -> bool:
    # Treat as loopback ONLY: the literal name "localhost", or an IP literal in a loopback range
    # (127.0.0.0/8, ::1). A string-prefix test like host.startswith("127.") is unsafe — it would
    # accept an attacker-controlled HOSTNAME such as "127.0.0.1.evil.example", bypassing the guard.
    import ipaddress

    h = (host or "").strip().lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False  # a non-IP hostname is never treated as loopback


def run(host: str = "127.0.0.1", port: int = 8766, *, allow_remote: bool = False) -> None:
    if not _is_loopback(host) and not allow_remote:
        print(f"refusing to bind non-loopback host {host!r} without --allow-remote — this would "
              f"expose an UNAUTHENTICATED code-intel endpoint (your indexed repo) to the network",
              file=sys.stderr)
        raise SystemExit(2)
    if not _is_loopback(host):
        print(f"WARNING: serving codeintel on {host}:{port} with NO authentication — anyone who can "
              f"reach this port can read your indexed repo", file=sys.stderr)
    server = CodeIntelHTTPServer((host, port), _Handler)
    print(f"Listening on http://{host}:{port}")
    server.serve_forever()
