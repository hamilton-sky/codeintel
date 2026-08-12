"""Enterprise surface (0.4.0): health/readiness probes, Prometheus /metrics, and the metrics
registry. Real live server on an ephemeral port; no mocks."""
from __future__ import annotations

import http.client
import json
import threading

from codeintel.http_server import CodeIntelHTTPServer, _Handler
from codeintel.metrics import Metrics


def _serve(token=None):
    s = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    s.auth_token = token
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, s.server_address[1]


def _get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, body


# --------------------------------------------------------------------------- probes

def test_healthz_ok_and_unauthenticated_even_with_token():
    s, port = _serve(token="secret")   # health must not require the token (kubelet won't send one)
    try:
        status, body = _get(port, "/healthz")
        assert status == 200 and json.loads(body)["status"] == "ok"
    finally:
        s.shutdown()


def test_readyz_reports_ready():
    s, port = _serve()
    try:
        status, body = _get(port, "/readyz")
        assert status == 200 and json.loads(body)["status"] == "ready"
    finally:
        s.shutdown()


def test_readyz_unauthenticated_even_with_token():
    s, port = _serve(token="secret")
    try:
        assert _get(port, "/readyz")[0] == 200
    finally:
        s.shutdown()


# --------------------------------------------------------------------------- /metrics endpoint

def test_metrics_exposition_format():
    s, port = _serve()
    try:
        _get(port, "/healthz")            # generate a recorded request first
        status, body = _get(port, "/metrics")
        assert status == 200
        assert "# TYPE codeintel_requests_total counter" in body
        assert "codeintel_build_info" in body
        assert 'path="/healthz"' in body  # the earlier request was counted
    finally:
        s.shutdown()


def test_metrics_is_auth_gated_when_token_set():
    s, port = _serve(token="secret")
    try:
        assert _get(port, "/metrics")[0] == 401
        assert _get(port, "/metrics", {"Authorization": "Bearer secret"})[0] == 200
    finally:
        s.shutdown()


# --------------------------------------------------------------------------- metrics registry

def test_metrics_registry_counts_and_caps_cardinality():
    m = Metrics(version="9.9.9")
    m.record("POST", "/code/query", 200, 0.05)
    m.record("POST", "/code/query", 200, 0.05)
    m.record("GET", "/some/random/unknown/path", 404, 0.01)  # unknown → folded to path="other"
    out = m.render()
    assert 'codeintel_requests_total{method="POST",path="/code/query",status="200"} 2' in out
    assert 'path="other"' in out
    assert "/some/random/unknown/path" not in out            # no unbounded label cardinality
    assert 'codeintel_build_info{version="9.9.9"} 1' in out
    assert "codeintel_request_duration_seconds_count" in out


def test_metrics_in_flight_gauge_never_negative():
    m = Metrics()
    m.dec_in_flight()  # underflow guard
    assert "codeintel_requests_in_flight 0" in m.render()


def test_metrics_exposes_rejected_and_in_flight():
    m = Metrics()
    m.inc_overload()
    m.inc_overload()
    m.inc_in_flight()
    out = m.render()
    assert "codeintel_requests_rejected_total 2" in out  # capacity refusals are now visible
    assert "codeintel_requests_in_flight 1" in out
    assert m.in_flight() == 1                             # accessor used by the shutdown drain


# --------------------------------------------------------------------------- secure-by-default bind

def test_run_refuses_non_loopback_without_auth(monkeypatch):
    import pytest

    from codeintel.http_server import run
    monkeypatch.delenv("CODEINTEL_ALLOW_NO_AUTH", raising=False)
    # non-loopback + --allow-remote but NO token and no override → must fail closed, not bind.
    with pytest.raises(SystemExit):
        run(host="0.0.0.0", port=0, allow_remote=True, token=None)


def test_run_refuses_non_loopback_without_allow_remote():
    import pytest

    from codeintel.http_server import run
    with pytest.raises(SystemExit):
        run(host="0.0.0.0", port=0, allow_remote=False, token="tok")


# --------------------------------------------------------------------------- structured logging

def test_json_log_formatter_emits_valid_json():
    import json
    import logging

    from codeintel.logconfig import _JsonFormatter

    rec = logging.LogRecord("codeintel", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    parsed = json.loads(_JsonFormatter().format(rec))
    assert parsed["level"] == "WARNING"
    assert parsed["msg"] == "hello world"
    assert parsed["logger"] == "codeintel"
