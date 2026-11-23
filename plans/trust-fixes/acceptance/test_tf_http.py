"""Story: an operator running the HTTP server with RBAC.

No other tenant's project ids in a hint, a denied `/code/status` is a 403, a nonexistent root is
`root-not-found`, and home redaction only touches a path that starts with home.
"""
from __future__ import annotations

import glob
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
from urllib.parse import quote

import pytest
from _fakegraph import FakeGraph

from codeintel.core.redact import redact_text
from codeintel.engines.graph.provider import GraphProvider
from codeintel.gateway.auth import load_auth
from codeintel.gateway.gateway import Gateway
from codeintel.gateway.reindexer import Reindexer
from codeintel.transports import mcp as server
from codeintel.transports.http import CodeIntelHTTPServer, _Handler

_BIN = os.path.dirname(sys.executable)
_TIMEOUT_S = 60


def _backend() -> str | None:
    return shutil.which("codebase-memory-mcp", path=_BIN + os.pathsep + os.environ.get("PATH", ""))


# --------------------------------------------------------------------------- backend-error hint

# Spec: A `backend-error` hint never contains other projects' ids. A "project not found" error from
# the backend is reported as `project-not-indexed`.
# (Live: the real codebase-memory-mcp answers a query about a project whose index was deleted while
# the server ran with "project not found ... available_projects: [<every other project>]".)
def test_a_project_deleted_under_a_running_server_is_project_not_indexed_without_other_ids(
        tmp_path, monkeypatch):
    if _backend() is None:
        pytest.skip("codebase-memory-mcp is not installed")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEBASE_MEMORY_HOME", raising=False)
    monkeypatch.delenv("CODEBASE_MEMORY_CACHE_DIR", raising=False)
    monkeypatch.setenv("PATH", _BIN + os.pathsep + "/usr/bin:/bin")
    roots = {}
    for name in ("mine", "tenant-b-secret"):
        d = tmp_path / name
        d.mkdir()
        (d / "m.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
        subprocess.run([_backend(), "cli", "index_repository"],
                       input=json.dumps({"repo_path": str(d)}).encode(),
                       capture_output=True, check=True, timeout=120)
        roots[name] = str(d)

    p = GraphProvider()
    first = p.build_result("callers", "b", [], 30000, roots["mine"])
    assert first.get("result"), first                 # resolved and answered while indexed
    cache = home / ".cache" / "codebase-memory-mcp"
    for f in glob.glob(str(cache / "*mine.db*")):
        os.remove(f)                                  # what `codeintel reset` does to it

    env = p.build_result("callees", "a", [], 30000, roots["mine"])
    assert "tenant-b-secret" not in json.dumps(env), env
    assert env.get("reason") == "project-not-indexed", env


# --------------------------------------------------------------------------- the RBAC server

@pytest.fixture
def rbac_server(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "auth.toml").write_text(
        '[roles]\nreader = ["overview", "callers", "callees", "search", "context"]\n'
        f'[roots]\nreader = ["{allowed}"]\n'
        '[tokens]\n"readtok" = "reader"\n')
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "auth.toml"))
    server._reset_gateway()
    s = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    s.token_auth = load_auth()
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1], str(allowed)
    s.shutdown()
    s.server_close()
    server._reset_gateway()


def _get(port: int, path: str, token: str) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=_TIMEOUT_S)
    c.request("GET", path, headers={"Authorization": f"Bearer {token}"})
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


def _post(port: int, body: dict, token: str) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=_TIMEOUT_S)
    c.request("POST", "/code/query", json.dumps(body).encode(),
              {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


# Spec: `GET /code/status?project_root=<root outside the role>` returns 403 with
# `reason: root-not-allowed-for-role`.
def test_status_for_a_root_outside_the_role_is_403_root_not_allowed(rbac_server, tmp_path):
    port, _allowed = rbac_server
    other = tmp_path / "other-tenant"
    other.mkdir()
    status, body = _get(port, f"/code/status?project_root={quote(str(other))}", "readtok")
    assert status == 403, (status, body)
    assert body.get("reason") == "root-not-allowed-for-role", body


# --------------------------------------------------------------------------- root-not-found

def _recording_gateway(tmp_path, monkeypatch) -> tuple[FakeGraph, list[str]]:
    """A gateway whose graph backend and reindexer record every call they get."""
    g = FakeGraph(root=str(tmp_path))
    g.node("m.main", "m.py")
    reindexed: list[str] = []
    monkeypatch.setattr(Reindexer, "maybe_reindex",
                        lambda self, root: reindexed.append(root) or False)
    gw = Gateway(graph=g.install(monkeypatch, str(tmp_path)), reindexer=Reindexer())
    monkeypatch.setattr(server, "_GATEWAY", gw)
    monkeypatch.setattr(server, "_refresh_missing_engines", lambda gw: None)
    return g, reindexed


# Spec: `code.query` with a `project_root` that does not exist returns `reason: root-not-found`
# before any dispatch or indexing.
def test_code_query_with_a_nonexistent_root_is_root_not_found_before_dispatch(
        tmp_path, monkeypatch):
    g, reindexed = _recording_gateway(tmp_path, monkeypatch)
    missing = str(tmp_path / "nonexistent" / "dir")
    env = server.code_query_handler({"op": "callers", "target": "main", "project_root": missing})
    assert env.get("reason") == "root-not-found", env
    assert g.calls == [], f"the backend was asked: {g.calls}"
    assert reindexed == [], "an index pass was started for a root that does not exist"


# Spec: `code.query` with a `project_root` that does not exist returns `reason: root-not-found`
# (over HTTP: `POST /code/query`).
def test_http_query_with_a_nonexistent_root_is_root_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "no-auth.toml"))
    g, _ = _recording_gateway(tmp_path, monkeypatch)
    s = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", s.server_address[1], timeout=_TIMEOUT_S)
        c.request("POST", "/code/query",
                  json.dumps({"op": "callers", "target": "main",
                              "project_root": str(tmp_path / "nonexistent")}).encode(),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
    finally:
        s.shutdown()
        s.server_close()
    assert body.get("reason") == "root-not-found", body


# Edge case: `project_root` empty on stdio -> cwd fallback unchanged; root-not-found only for a
# non-empty path that is not a dir.
def test_root_not_found_is_only_for_a_non_empty_path_that_is_not_a_directory(
        tmp_path, monkeypatch):
    _recording_gateway(tmp_path, monkeypatch)
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x")
    env = server.code_query_handler(
        {"op": "callers", "target": "main", "project_root": str(a_file)})
    assert env.get("reason") == "root-not-found", env

    monkeypatch.chdir(tmp_path)
    blank = server.code_query_handler({"op": "callers", "target": "main", "project_root": ""})
    assert blank.get("reason") != "root-not-found", blank


# --------------------------------------------------------------------------- home redaction

# Spec: Home redaction leaves `/srv/root/app`, `/proc/self/root/x` and `scratchpad/root/app` alone
# (HOME=/root), and still turns `/root/x` into `~/x`.
def test_home_redaction_only_rewrites_a_path_that_starts_with_home(monkeypatch):
    monkeypatch.setenv("HOME", "/root")
    assert redact_text("/srv/root/app") == "/srv/root/app"
    assert redact_text("/proc/self/root/x") == "/proc/self/root/x"
    assert redact_text("scratchpad/root/app") == "scratchpad/root/app"
    assert redact_text("run: codeintel index /tmp/s/scratchpad/root/app") == \
        "run: codeintel index /tmp/s/scratchpad/root/app"
    assert redact_text("/root/x") == "~/x"
    assert redact_text("run: codeintel index /root/x") == "run: codeintel index ~/x"


# Edge case: `https://example.com/root/api` under HOME=/root -> left unchanged: the home string is
# redacted only where it starts a path token.
def test_home_redaction_leaves_a_url_path_alone(monkeypatch):
    monkeypatch.setenv("HOME", "/root")
    assert redact_text("https://example.com/root/api") == "https://example.com/root/api"
    assert redact_text("/srv/root/app") == "/srv/root/app"
