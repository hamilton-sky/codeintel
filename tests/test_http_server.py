from __future__ import annotations

import http.client
import json
import threading

import pytest

from codeintel.http_server import CodeIntelHTTPServer, _Handler, _MAX_BODY_BYTES, _is_loopback, run


@pytest.fixture
def server():
    srv = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, port
    finally:
        srv.shutdown()


def _post(port: int, path: str, body: bytes, content_type: str = "application/json") -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _get(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _post_with_headers(port: int, path: str, body: bytes, headers: dict) -> tuple[int, dict]:
    # Bypasses http.client's automatic Content-Length calculation so a test can
    # send a header that lies about the body size without transferring that many bytes.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def test_query_valid_body(server):
    _, port = server
    body = json.dumps({"op": "symbol", "target": "Gateway"}).encode()
    status, data = _post(port, "/code/query", body)
    assert status == 200
    assert data.get("ok") is True
    for key in ("op", "target", "result", "engine", "cached"):
        assert key in data


def test_query_bad_json(server):
    _, port = server
    status, data = _post(port, "/code/query", b"not-json")
    assert status == 400
    assert data.get("error") == "bad-request"


def test_query_empty_body(server):
    _, port = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/code/query", body=b"", headers={"Content-Length": "0"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 400
    assert data.get("error") == "bad-request"


def test_query_engine_miss(server):
    _, port = server
    body = json.dumps({"op": "symbol", "target": "DoesNotExist"}).encode()
    status, data = _post(port, "/code/query", body)
    assert status == 200
    assert data.get("ok") is True


def test_status(server):
    _, port = server
    status, data = _get(port, "/code/status")
    assert status == 200
    assert data.get("ok") is True


def test_unknown_route(server):
    _, port = server
    status, data = _get(port, "/unknown")
    assert status == 404
    assert data.get("error") == "not-found"


def test_oversized_body_returns_413(server):
    _, port = server
    fake_length = _MAX_BODY_BYTES + 1
    status, data = _post_with_headers(
        port, "/code/query", b"", {"Content-Length": str(fake_length)}
    )
    assert status == 413
    assert data.get("error") == "payload-too-large"
    assert data.get("max_bytes") == _MAX_BODY_BYTES

    # server must still be up for a subsequent normal request
    body = json.dumps({"op": "symbol", "target": "Gateway"}).encode()
    status2, data2 = _post(port, "/code/query", body)
    assert status2 == 200
    assert data2.get("ok") is True


def test_normal_body_still_ok(server):
    _, port = server
    body = json.dumps({"op": "symbol", "target": "Gateway"}).encode()
    status, data = _post(port, "/code/query", body)
    assert status == 200
    assert data.get("ok") is True


def test_run_refuses_non_loopback_without_flag():
    with pytest.raises(SystemExit):
        run("0.0.0.0", 0)


def test_run_allows_loopback():
    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("127.0.0.5") is True   # anywhere in 127.0.0.0/8
    assert _is_loopback("::1") is True
    assert _is_loopback("localhost") is True
    assert _is_loopback("0.0.0.0") is False


def test_is_loopback_rejects_hostname_bypass():
    # A hostname that merely starts with "127." must NOT be treated as loopback (security guard).
    assert _is_loopback("127.0.0.1.attacker.example") is False
    assert _is_loopback("127.0.0.1.evil.com") is False
    assert _is_loopback("evil.com") is False
    assert _is_loopback("") is False
