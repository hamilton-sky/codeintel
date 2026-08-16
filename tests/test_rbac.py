"""RBAC (0.5.0): token→role authentication + per-role op scopes, enforced server-side. The role
is derived from the authenticated token, never from the request body — a client cannot escalate."""
from __future__ import annotations

import hashlib
import http.client
import json
import threading

import pytest

from codeintel.auth import TokenAuth, load_auth
from codeintel.http_server import CodeIntelHTTPServer, _Handler


# --------------------------------------------------------------------------- auth config loading

def test_load_auth_parses_roles_and_tokens(tmp_path, monkeypatch):
    cfg = tmp_path / "auth.toml"
    cfg.write_text('[roles]\nadmin = ["*"]\nreader = ["search", "context"]\n'
                   '[tokens]\n"admin-tok" = "admin"\n"reader-tok" = "reader"\n')
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(cfg))
    auth = load_auth()
    assert auth.enabled
    assert auth.role_for("admin-tok") == "admin"
    assert auth.role_for("reader-tok") == "reader"
    assert auth.role_for("unknown") is None


def test_load_auth_missing_file_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "nope.toml"))
    assert not load_auth().enabled


def test_load_auth_malformed_file_is_disabled_not_raised(tmp_path, monkeypatch):
    (tmp_path / "auth.toml").write_text("this is not === valid toml")
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "auth.toml"))
    assert not load_auth().enabled


def test_sha256_prefixed_token_matches_plaintext(tmp_path, monkeypatch):
    h = hashlib.sha256(b"secret-value").hexdigest()
    (tmp_path / "auth.toml").write_text(f'[roles]\nadmin = ["*"]\n[tokens]\n"sha256:{h}" = "admin"\n')
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "auth.toml"))
    assert load_auth().role_for("secret-value") == "admin"  # hashed at rest, matches plaintext


def test_build_policy_restricts_and_omits_wildcard():
    auth = TokenAuth({"h1": "admin", "h2": "reader"}, {"admin": ["*"], "reader": ["search"]})
    pol = auth.build_policy()
    assert pol.is_allowed("admin", "impact")       # ["*"] → unrestricted
    assert pol.is_allowed("reader", "search")      # in the allowlist
    assert not pol.is_allowed("reader", "impact")  # not in the allowlist


# --------------------------------------------------------------------------- HTTP enforcement

@pytest.fixture
def rbac_server(tmp_path, monkeypatch):
    (tmp_path / "auth.toml").write_text(
        '[roles]\nadmin = ["*"]\nreader = ["search", "context"]\n'
        '[tokens]\n"admintok" = "admin"\n"readertok" = "reader"\n'
    )
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "auth.toml"))
    from codeintel import server as srv
    srv._reset_gateway()  # rebuild the gateway so its policy reflects THIS auth config
    s = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    s.token_auth = load_auth()  # the same config the gateway policy is built from
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()
    srv._reset_gateway()  # cleanup: the next test rebuilds without this policy


def _query(port, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/code/query", json.dumps(body).encode(), headers)
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


def test_rbac_no_token_is_401(rbac_server):
    assert _query(rbac_server, {"op": "search", "target": "x"})[0] == 401


def test_rbac_invalid_token_is_401(rbac_server):
    assert _query(rbac_server, {"op": "search", "target": "x"}, token="bogus")[0] == 401


def test_rbac_admin_passes_the_policy_for_a_restricted_op(rbac_server):
    status, body = _query(rbac_server, {"op": "impact", "target": "x"}, token="admintok")
    assert status == 200
    assert body.get("reason") != "op-not-allowed-for-role"  # admin is allowed (engine may still be null)


def test_rbac_reader_denied_disallowed_op_is_403(rbac_server):
    status, body = _query(rbac_server, {"op": "impact", "target": "x"}, token="readertok")
    assert status == 403
    assert body["reason"] == "op-not-allowed-for-role"


def test_rbac_reader_allowed_op_is_not_denied(rbac_server):
    status, body = _query(rbac_server, {"op": "search", "target": "x"}, token="readertok")
    assert status == 200
    assert body.get("reason") != "op-not-allowed-for-role"


def test_rbac_role_is_server_authoritative_no_body_escalation(rbac_server):
    # A reader token claiming role="admin" in the body must STILL be treated as a reader.
    status, body = _query(rbac_server, {"op": "impact", "target": "x", "role": "admin"}, token="readertok")
    assert status == 403
    assert body["reason"] == "op-not-allowed-for-role"


def test_load_auth_non_list_ops_fails_closed(tmp_path, monkeypatch):
    # `reader = "search"` (missing brackets) must DENY ALL, never silently grant full access.
    (tmp_path / "auth.toml").write_text('[roles]\nadmin = ["*"]\nreader = "search"\n[tokens]\n"t" = "reader"\n')
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "auth.toml"))
    pol = load_auth().build_policy()
    assert not pol.is_allowed("reader", "search")  # fail-closed, not the "*" fallback
    assert not pol.is_allowed("reader", "impact")


def _doctor(port, token):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/code/doctor", b'{"project_root":"/tmp"}',
              {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    r = c.getresponse()
    body = json.loads(r.read())
    c.close()
    return r.status, body


def test_rbac_reader_denied_doctor_is_403(rbac_server):
    status, body = _doctor(rbac_server, "readertok")   # reader has no "doctor" scope
    assert status == 403 and body["reason"] == "op-not-allowed-for-role"


def test_doctor_over_http_omits_local_registrations(rbac_server):
    """`registrations` names the agent config files on THIS machine. A shared HTTP deployment is
    not an agent host, so the field says nothing useful there and only leaks the server user's home
    layout and which agent tools are installed. It stays on the local CLI / stdio MCP surfaces."""
    status, body = _doctor(rbac_server, "admintok")
    assert status == 200
    assert "registrations" not in body
    assert "engines" in body                       # the rest of the report is untouched


def test_rbac_admin_allowed_doctor(rbac_server):
    status, body = _doctor(rbac_server, "admintok")     # admin is ["*"]
    assert status == 200 and body.get("reason") != "op-not-allowed-for-role"


def test_denied_op_does_no_reindex_work(monkeypatch):
    from codeintel.gateway import Gateway
    from codeintel.policy import TieringPolicy
    from codeintel.reindexer import Reindexer
    rx = Reindexer()
    calls = {"n": 0}
    monkeypatch.setattr(rx, "maybe_reindex", lambda root: calls.__setitem__("n", calls["n"] + 1))
    gw = Gateway(graph=None, lsp=None, semantic=None,
                 policy=TieringPolicy(enabled=True, rules={"reader": ["search"]}), reindexer=rx)
    denied = gw.query(op="impact", target="x", role="reader", project_root="/tmp/x")
    assert denied["reason"] == "op-not-allowed-for-role"
    assert calls["n"] == 0                              # policy denial skips reindex entirely
    gw.query(op="search", target="x", role="reader", project_root="/tmp/x")
    assert calls["n"] == 1                              # an allowed op proceeds to reindex


def test_rbac_healthz_still_unauthenticated(rbac_server):
    c = http.client.HTTPConnection("127.0.0.1", rbac_server, timeout=5)
    c.request("GET", "/healthz")
    assert c.getresponse().status == 200  # probes never require a token, even under RBAC
    c.close()
