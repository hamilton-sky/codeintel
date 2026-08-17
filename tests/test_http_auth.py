"""HTTP transport hardening (0.3.0): optional bearer-token auth, per-request timeout, and the
project-scoped status query. Real live server on an ephemeral port — no mocks."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from codeintel.http_server import _MAX_CONCURRENT_REQUESTS, CodeIntelHTTPServer, _Handler

# A request that reaches a live graph/LSP backend legitimately takes seconds: the backend is a
# native binary that re-initialises per invocation (~6s measured). The old 5s client timeout was
# fine only because no CI job installs those backends — with one present these tests time out,
# which is a property of the harness, not of the server.
_HTTP_TEST_TIMEOUT_S = 60


def _serve(token=None):
    server = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    server.auth_token = token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post(port, path="/code/query", body=None, headers=None):
    data = json.dumps(body if body is not None else {"op": "symbol", "target": "x"}).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_no_token_allows_request():
    server, port = _serve(token=None)
    try:
        status, body = _post(port)
        assert status == 200 and body["ok"] is True
    finally:
        server.shutdown()


def test_missing_token_is_rejected():
    server, port = _serve(token="s3cret")
    try:
        assert _post(port)[0] == 401
    finally:
        server.shutdown()


def test_wrong_token_is_rejected():
    server, port = _serve(token="s3cret")
    try:
        assert _post(port, headers={"Authorization": "Bearer nope"})[0] == 401
    finally:
        server.shutdown()


def test_correct_token_is_accepted():
    server, port = _serve(token="s3cret")
    try:
        status, body = _post(port, headers={"Authorization": "Bearer s3cret"})
        assert status == 200 and body["ok"] is True
    finally:
        server.shutdown()


def test_non_ascii_token_is_rejected_not_crashed():
    server, port = _serve(token="s3cret")
    try:
        # A non-ASCII bearer must yield a clean 401, never crash the handler thread — hmac
        # compare_digest raises TypeError on non-ASCII str, so the handler compares bytes.
        status, _ = _post(port, headers={"Authorization": "Bearer résumé"})
        assert status == 401
    finally:
        server.shutdown()


def test_auth_also_guards_status_get():
    server, port = _serve(token="s3cret")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/code/status")
        try:
            urllib.request.urlopen(req)
            pytest.fail("expected 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        server.shutdown()


def test_status_get_accepts_project_root_query():
    server, port = _serve(token=None)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/code/status?project_root=/tmp/nope")
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
        assert body["ok"] is True and "indexed" in body
    finally:
        server.shutdown()


def test_handler_has_a_request_timeout():
    assert _Handler.timeout and _Handler.timeout > 0


def test_server_refuses_with_503_when_at_capacity():
    import http.client

    server, port = _serve(token=None)
    # Drain every worker slot so the next connection is refused fast (503) instead of spawning an
    # unbounded thread — the bound that keeps a slow-client burst from exhausting threads/FDs.
    for _ in range(_MAX_CONCURRENT_REQUESTS):
        assert server._slots.acquire(blocking=False)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_HTTP_TEST_TIMEOUT_S)
        conn.request("GET", "/code/status")
        assert conn.getresponse().status == 503
        conn.close()
    finally:
        for _ in range(_MAX_CONCURRENT_REQUESTS):
            server._slots.release()
        server.shutdown()
