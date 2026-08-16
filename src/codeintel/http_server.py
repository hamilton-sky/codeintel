from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from codeintel import __version__
from codeintel.logconfig import configure_logging
from codeintel.metrics import Metrics
from codeintel.provider import log_swallowed
from codeintel.server import code_doctor_handler, code_query_handler, code_status_handler

_MAX_BODY_BYTES = 1_048_576       # 1 MiB
_REQUEST_TIMEOUT_S = 60           # per-request socket read timeout — drops an idle/half-open client
_MAX_CONCURRENT_REQUESTS = 64     # cap live worker threads so a burst can't exhaust threads/FDs
_DRAIN_TIMEOUT_S = 15             # on shutdown, wait up to this long for in-flight requests to finish
_ACCESS_LOG = os.environ.get("CODEINTEL_HTTP_ACCESS_LOG", "").strip().lower() in ("1", "true", "on", "yes")
_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_logger = logging.getLogger("codeintel")


class _Handler(BaseHTTPRequestHandler):
    # Applied to the request socket by the base handler's setup(); a slow/half-open client
    # connection is dropped instead of pinning a worker thread forever (basic slowloris guard).
    timeout = _REQUEST_TIMEOUT_S

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default stderr noise; opt-in access logging is handled in _observe

    def handle_one_request(self) -> None:
        self._t0 = time.monotonic()
        m = getattr(self.server, "metrics", None)
        if m is not None:
            m.inc_in_flight()
        try:
            super().handle_one_request()
        finally:
            if m is not None:
                m.dec_in_flight()

    def _observe(self, status: int) -> None:
        try:
            dur = time.monotonic() - getattr(self, "_t0", time.monotonic())
        except Exception:
            dur = 0.0
        m = getattr(self.server, "metrics", None)
        if m is not None:
            try:
                m.record(self.command or "?", urlparse(self.path).path, status, dur)
            except Exception:
                pass
        if _ACCESS_LOG:
            try:
                _logger.info("%s %s -> %s (%.1fms)", self.command, self.path, status, dur * 1000.0)
            except Exception:
                pass

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._observe(status)

    def _send_text(self, status: int, text: str, content_type: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._observe(status)

    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return header[len(prefix):].strip() if header.startswith(prefix) else ""

    def _resolve_role(self) -> tuple[bool, str]:
        """Authenticate the request and return ``(authorized, role)``. The role is
        **server-authoritative** — derived from the token, never from the request body. RBAC (a
        token→role auth config) takes precedence; else a single shared token; else no auth (the
        loopback default). Bytes-compare so a non-ASCII header can't crash ``hmac.compare_digest``."""
        auth = getattr(self.server, "token_auth", None)
        if auth is not None and auth.enabled:
            role = auth.role_for(self._bearer())
            return (role is not None, role or "")
        token = getattr(self.server, "auth_token", None)
        if not token:
            return (True, "")  # auth disabled → full access, no role
        presented = self._bearer()
        ok = bool(presented) and hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
        return (ok, "")

    def do_POST(self) -> None:
        ok, role = self._resolve_role()
        if not ok:
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
        parsed["role"] = role  # server-authoritative — overrides any client-supplied role (no escalation)
        if self.path == "/code/doctor":
            result = code_doctor_handler(parsed)
            # `registrations` (which agent hosts this machine registered codeintel with, and where)
            # is a LOCAL diagnostic: on a shared deployment the server is not an agent host, so the
            # field says nothing useful and only hands a client the server user's home layout and
            # which agent tools are installed there. Dropped on the network transport; the CLI and
            # the stdio MCP tool — both running as the user, on their own machine — still get it.
            if isinstance(result, dict):
                result.pop("registrations", None)
        else:
            result = code_query_handler(parsed)
        # An RBAC denial (query OR doctor) comes back as a safe-null with this reason — 403 it.
        status = 403 if result.get("reason") == "op-not-allowed-for-role" else 200
        self._send_json(status, result)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # Liveness / readiness are UNAUTHENTICATED by convention (kubelet/load balancers don't send
        # tokens) and reveal nothing sensitive — only up/ready.
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/readyz":
            try:
                from codeintel.server import _get_gateway
                _get_gateway()  # builds once (cached); readiness = the gateway is constructible
                self._send_json(200, {"status": "ready"})
            except Exception as exc:
                log_swallowed("readyz", exc)
                self._send_json(503, {"status": "not-ready"})
            return

        # Everything else is auth-gated when auth is configured (metrics can reveal usage patterns;
        # status reveals engine/index state). Any valid token/role may read these operational views.
        ok, _ = self._resolve_role()
        if not ok:
            self._send_json(401, {"error": "unauthorized"})
            return

        if path == "/metrics":
            m = getattr(self.server, "metrics", None)
            try:
                text = m.render() if m is not None else ""
            except Exception as exc:
                log_swallowed("metrics.render", exc)  # never leave the client with no response
                text = ""
            self._send_text(200, text, _PROM_CONTENT_TYPE)
            return
        if path == "/code/status":
            project_root = (parse_qs(parsed.query).get("project_root") or [""])[0]
            self._send_json(200, code_status_handler({"project_root": project_root}))
            return
        self._send_json(404, {"error": "not-found"})


class CodeIntelHTTPServer(ThreadingHTTPServer):
    # Threaded so one slow request (an LSP session warming, or a first-time index) can't block
    # every other agent's query. The gateway is a shared singleton, but its mutable state is
    # lock-guarded (query cache, reindexer, LSP sessions, graph project cache) and the semantic
    # engine is thread-confined with WAL, so concurrent requests are safe.
    daemon_threads = True
    auth_token: str | None = None  # set by run() when a single shared token is provided
    token_auth = None              # set by run(): a TokenAuth for RBAC (token->role), if configured

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.metrics = Metrics(version=__version__)
        # Stdlib ThreadingHTTPServer spawns one thread per connection with no ceiling, so many
        # slow/half-open clients could exhaust threads/FDs. Bound live workers; past the cap we
        # refuse fast with 503 rather than spawn an unbounded thread. (For a genuinely hostile
        # network, still front this with a reverse proxy — http.server is not hardened for that.)
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                self.metrics.inc_overload()  # surface overload in /metrics (rejected_total)
            except Exception:
                pass
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


def _no_auth_override() -> bool:
    return os.environ.get("CODEINTEL_ALLOW_NO_AUTH", "").strip().lower() in ("1", "true", "on", "yes")


def run(
    host: str = "127.0.0.1",
    port: int = 8766,
    *,
    allow_remote: bool = False,
    token: str | None = None,
) -> None:
    configure_logging()
    from codeintel.auth import load_auth
    token_auth = load_auth()                          # RBAC (token→role), if an auth config exists
    server_token = (token or "").strip() or None
    authenticated = bool(server_token) or token_auth.enabled
    if not _is_loopback(host):
        if not allow_remote:
            print(f"refusing to bind non-loopback host {host!r} without --allow-remote — this would "
                  f"expose a code-intel endpoint (your indexed repo) to the network", file=sys.stderr)
            raise SystemExit(2)
        if not authenticated and not _no_auth_override():
            # Fail closed: never serve an UNAUTHENTICATED endpoint on the network by accident (a
            # containerized `serve-http --host 0.0.0.0` with no auth stops here unless overridden).
            print(f"refusing to serve on {host}:{port} with NO authentication — set --token / "
                  f"CODEINTEL_HTTP_TOKEN, configure an RBAC auth.toml, or set CODEINTEL_ALLOW_NO_AUTH=1 "
                  f"to override for a trusted network", file=sys.stderr)
            raise SystemExit(2)

    server = CodeIntelHTTPServer((host, port), _Handler)
    server.auth_token = server_token
    server.token_auth = token_auth
    if not _is_loopback(host) and not authenticated:
        print(f"WARNING: serving codeintel on {host}:{port} with NO authentication "
              f"(CODEINTEL_ALLOW_NO_AUTH set) — anyone who can reach this port can read your "
              f"indexed repo", file=sys.stderr)
    if token_auth.enabled:
        auth_note = "  (RBAC: token→role auth)"
    elif server_token:
        auth_note = "  (bearer-token auth required)"
    else:
        auth_note = ""
    print(f"Listening on http://{host}:{port}{auth_note}")

    def _graceful(signum, frame) -> None:
        # shutdown() blocks until serve_forever() returns, so it must run OFF the serving thread —
        # spawn a helper thread to avoid deadlocking the signal handler on the main thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
    except (ValueError, OSError):
        pass  # not the main thread — signal handlers unavailable; rely on the caller to stop us

    try:
        server.serve_forever()
    finally:
        # Drain: serve_forever() has stopped accepting new connections; wait (bounded) for in-flight
        # requests to finish before closing. Worker threads are daemons, so server_close() will NOT
        # join them — this loop is what makes the shutdown actually graceful (a rolling restart
        # doesn't cut a live response mid-flight).
        deadline = time.monotonic() + _DRAIN_TIMEOUT_S
        while server.metrics.in_flight() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        server.server_close()
        print("codeintel http server stopped", file=sys.stderr)
