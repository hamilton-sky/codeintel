from __future__ import annotations

import hmac
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from codeintel.provider import log_swallowed
from codeintel.server import code_doctor_handler, code_query_handler, code_status_handler

_MAX_BODY_BYTES = 1_048_576       # 1 MiB
_REQUEST_TIMEOUT_S = 60           # per-request socket read timeout — drops an idle/half-open client
_MAX_CONCURRENT_REQUESTS = 64     # cap live worker threads so a burst can't exhaust threads/FDs


class _Handler(BaseHTTPRequestHandler):
    # Applied to the request socket by the base handler's setup(); a slow/half-open client
    # connection is dropped instead of pinning a worker thread forever (basic slowloris guard).
    timeout = _REQUEST_TIMEOUT_S

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default stderr noise

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """When the server was started with a token, require ``Authorization: Bearer <token>``
        (constant-time compare). No token configured → auth is disabled (the loopback default)."""
        token = getattr(self.server, "auth_token", None)
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        presented = header[len(prefix):].strip()
        # Compare as UTF-8 bytes: hmac.compare_digest raises TypeError on a non-ASCII str, so a
        # malformed header (e.g. `Bearer résumé`) would otherwise crash the handler thread. Encoding
        # is always safe and keeps the comparison constant-time.
        return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
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
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        if parsed.path != "/code/status":
            self._send_json(404, {"error": "not-found"})
            return
        # Optional ?project_root=... scopes the `indexed` flag to that repo.
        project_root = (parse_qs(parsed.query).get("project_root") or [""])[0]
        result = code_status_handler({"project_root": project_root})
        self._send_json(200, result)


class CodeIntelHTTPServer(ThreadingHTTPServer):
    # Threaded so one slow request (an LSP session warming, or a first-time index) can't block
    # every other agent's query. The gateway is a shared singleton, but its mutable state is
    # lock-guarded (query cache, reindexer, LSP sessions, graph project cache) and the semantic
    # engine is thread-confined with WAL, so concurrent requests are safe.
    daemon_threads = True
    auth_token: str | None = None  # set by run() when a token is provided

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Stdlib ThreadingHTTPServer spawns one thread per connection with no ceiling, so many
        # slow/half-open clients could exhaust threads/FDs. Bound live workers; past the cap we
        # refuse fast with 503 rather than spawn an unbounded thread. (For a genuinely hostile
        # network, still front this with a reverse proxy — http.server is not hardened for that.)
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 22\r\n"
                    b'Connection: close\r\n\r\n{"error":"overloaded"}'
                )
            except Exception:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)  # spawns the worker thread

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        # A stalled/reset client raises here (socket timeout, broken pipe) — expected noise, not a
        # server bug (handlers are never-raise). Keep it off stderr; surface with CODEINTEL_DEBUG=1.
        log_swallowed("http.handle_error", sys.exc_info()[1] or Exception("unknown"))


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


def run(
    host: str = "127.0.0.1",
    port: int = 8766,
    *,
    allow_remote: bool = False,
    token: str | None = None,
) -> None:
    if not _is_loopback(host) and not allow_remote:
        print(f"refusing to bind non-loopback host {host!r} without --allow-remote — this would "
              f"expose an UNAUTHENTICATED code-intel endpoint (your indexed repo) to the network",
              file=sys.stderr)
        raise SystemExit(2)
    server = CodeIntelHTTPServer((host, port), _Handler)
    server.auth_token = (token or "").strip() or None
    if not _is_loopback(host) and not server.auth_token:
        print(f"WARNING: serving codeintel on {host}:{port} with NO authentication — anyone who can "
              f"reach this port can read your indexed repo (set --token to require a bearer token)",
              file=sys.stderr)
    auth_note = "  (bearer-token auth required)" if server.auth_token else ""
    print(f"Listening on http://{host}:{port}{auth_note}")
    server.serve_forever()
