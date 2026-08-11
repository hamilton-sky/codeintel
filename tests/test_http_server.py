from __future__ import annotations

import http.client
import json
import threading

import pytest

from codeintel.http_server import CodeIntelHTTPServer, _Handler


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
