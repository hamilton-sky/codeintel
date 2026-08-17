"""RBAC (0.5.0): token→role authentication + per-role op scopes, enforced server-side. The role
is derived from the authenticated token, never from the request body — a client cannot escalate."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading

import pytest

from codeintel.auth import TokenAuth, load_auth
from codeintel.http_server import CodeIntelHTTPServer, _Handler
from codeintel.reindexer import Reindexer

# A request that reaches a live graph/LSP backend legitimately takes seconds: the backend is a
# native binary that re-initialises per invocation (~6s measured). The old 5s client timeout was
# fine only because no CI job installs those backends — with one present these tests time out,
# which is a property of the harness, not of the server.
_HTTP_TEST_TIMEOUT_S = 60

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
        # [roots] is required as of 0.14.0 — these tests are about OP scoping, so both roles are
        # root-unrestricted here and the dedicated scoping tests below cover the roots dimension.
        '[roots]\nadmin = ["*"]\nreader = ["*"]\n'
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
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=_HTTP_TEST_TIMEOUT_S)
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
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=_HTTP_TEST_TIMEOUT_S)
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
                 policy=TieringPolicy(enabled=True, rules={"reader": ["search"]},
                                      roots={"reader": ["*"]}), reindexer=rx)
    denied = gw.query(op="impact", target="x", role="reader", project_root="/tmp/x")
    assert denied["reason"] == "op-not-allowed-for-role"
    assert calls["n"] == 0                              # policy denial skips reindex entirely
    gw.query(op="search", target="x", role="reader", project_root="/tmp/x")
    assert calls["n"] == 1                              # an allowed op proceeds to reindex


def test_rbac_healthz_still_unauthenticated(rbac_server):
    c = http.client.HTTPConnection("127.0.0.1", rbac_server, timeout=_HTTP_TEST_TIMEOUT_S)
    c.request("GET", "/healthz")
    assert c.getresponse().status == 200  # probes never require a token, even under RBAC
    c.close()


# --------------------------------------------------------------------------- project scoping
#
# RBAC used to scope only WHICH OPS a token may call, never WHICH DIRECTORY it may point them at.
# Since `project_root` arrives in the request body, the least-privileged documented role
# (`searcher = ["search", "context"]`) could name any directory the server process could read, and
# the semantic provider would walk, index, and return its contents. Demonstrated against a live
# server before the fix. These pin the closure.

def _scoped(roots, rules=None):
    from codeintel.gateway import Gateway
    from codeintel.policy import TieringPolicy
    return Gateway(graph=None, lsp=None, semantic=None,
                   policy=TieringPolicy(enabled=True, rules=rules or {}, roots=roots),
                   reindexer=Reindexer())


def test_a_role_may_not_target_a_project_outside_its_roots(tmp_path):
    allowed, forbidden = tmp_path / "allowed", tmp_path / "secret"
    allowed.mkdir(); forbidden.mkdir()
    gw = _scoped({"reader": [str(allowed)]})

    denied = gw.query(op="search", target="x", role="reader", project_root=str(forbidden))
    assert denied["result"] is None
    assert denied["reason"] == "root-not-allowed-for-role"
    assert "auth.toml" in denied["hint"]


def test_a_role_may_target_its_own_root_and_below(tmp_path):
    allowed = tmp_path / "allowed"
    (allowed / "pkg").mkdir(parents=True)
    gw = _scoped({"reader": [str(allowed)]})

    for root in (str(allowed), str(allowed / "pkg")):
        res = gw.query(op="search", target="x", role="reader", project_root=root)
        assert res.get("reason") != "root-not-allowed-for-role", root


def test_a_sibling_sharing_a_name_prefix_is_not_inside_the_root(tmp_path):
    """String-prefix containment would let `/srv/repo-secrets` pass for a root of `/srv/repo`."""
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo-secrets").mkdir()
    gw = _scoped({"reader": [str(tmp_path / "repo")]})

    denied = gw.query(op="search", target="x", role="reader",
                      project_root=str(tmp_path / "repo-secrets"))
    assert denied["reason"] == "root-not-allowed-for-role"


def test_dot_dot_traversal_cannot_escape_an_allowed_root(tmp_path):
    allowed, forbidden = tmp_path / "allowed", tmp_path / "secret"
    allowed.mkdir(); forbidden.mkdir()
    gw = _scoped({"reader": [str(allowed)]})

    denied = gw.query(op="search", target="x", role="reader",
                      project_root=str(allowed / ".." / "secret"))
    assert denied["reason"] == "root-not-allowed-for-role"


def test_a_symlink_out_of_an_allowed_root_does_not_escape(tmp_path):
    allowed, forbidden = tmp_path / "allowed", tmp_path / "secret"
    allowed.mkdir(); forbidden.mkdir()
    (allowed / "escape").symlink_to(forbidden, target_is_directory=True)
    gw = _scoped({"reader": [str(allowed)]})

    denied = gw.query(op="search", target="x", role="reader",
                      project_root=str(allowed / "escape"))
    assert denied["reason"] == "root-not-allowed-for-role"


@pytest.mark.parametrize("roots", [{}, {"reader": []}, {"other": ["/srv"]}])
def test_an_undeclared_or_empty_root_list_denies_everything(tmp_path, roots):
    """Fails CLOSED, unlike the op allowlist. A root list that defaults to "everywhere" would be
    the whole vulnerability with extra configuration."""
    gw = _scoped(roots)
    denied = gw.query(op="search", target="x", role="reader", project_root=str(tmp_path))
    assert denied["reason"] == "root-not-allowed-for-role"


def test_a_wildcard_root_is_unrestricted(tmp_path):
    gw = _scoped({"admin": ["*"]})
    res = gw.query(op="search", target="x", role="admin", project_root=str(tmp_path))
    assert res.get("reason") != "root-not-allowed-for-role"


def test_scoping_is_inert_when_rbac_is_off(tmp_path):
    """The local stdio path passes role="" with RBAC disabled — it must stay unrestricted, or
    every ordinary `codeintel query` breaks."""
    from codeintel.gateway import Gateway
    from codeintel.policy import TieringPolicy
    gw = Gateway(graph=None, lsp=None, semantic=None,
                 policy=TieringPolicy(enabled=False), reindexer=Reindexer())
    res = gw.query(op="search", target="x", role="", project_root=str(tmp_path))
    assert res.get("reason") != "root-not-allowed-for-role"


def test_a_denied_root_does_no_work_at_all(tmp_path, monkeypatch):
    """The denial must land BEFORE maybe_reindex: an out-of-scope path must not even be walked,
    or the check leaks existence and burns the exact indexing work it is meant to prevent."""
    from codeintel.gateway import Gateway
    from codeintel.policy import TieringPolicy
    rx = Reindexer()
    calls = {"n": 0}
    monkeypatch.setattr(rx, "maybe_reindex", lambda root: calls.__setitem__("n", calls["n"] + 1))
    gw = Gateway(graph=None, lsp=None, semantic=None,
                 policy=TieringPolicy(enabled=True, roots={"reader": ["/srv/allowed"]}),
                 reindexer=rx)

    gw.query(op="search", target="x", role="reader", project_root=str(tmp_path))
    assert calls["n"] == 0


def test_load_auth_parses_the_roots_table(tmp_path, monkeypatch):
    cfg = tmp_path / "auth.toml"
    cfg.write_text(
        '[roles]\nreader = ["search"]\n'
        '[roots]\nreader = ["/srv/repos/team-a"]\n'
        '[tokens]\n"tok" = "reader"\n'
    )
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(cfg))
    policy = load_auth().build_policy()

    assert policy.is_root_allowed("reader", "/srv/repos/team-a") is True
    assert policy.is_root_allowed("reader", "/srv/repos/team-b") is False


def test_load_auth_roots_as_a_bare_string_fails_closed(tmp_path, monkeypatch):
    """`reader = "/srv/repo"` without brackets is a typo; reading it as one allowed path would
    hand out access the operator never wrote."""
    cfg = tmp_path / "auth.toml"
    cfg.write_text('[roles]\nreader = ["search"]\n[roots]\nreader = "/srv/repo"\n'
                   '[tokens]\n"tok" = "reader"\n')
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(cfg))

    assert load_auth().build_policy().is_root_allowed("reader", "/srv/repo") is False


def test_an_all_wildcard_ops_config_still_enforces_roots(tmp_path, monkeypatch):
    """`enabled` was `bool(rules)`, so a config whose roles were all `["*"]` produced a DISABLED
    policy — and a disabled policy enforces no root scoping either."""
    cfg = tmp_path / "auth.toml"
    cfg.write_text('[roles]\nadmin = ["*"]\n[roots]\nadmin = ["/srv/only"]\n'
                   '[tokens]\n"tok" = "admin"\n')
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(cfg))
    policy = load_auth().build_policy()

    assert policy.is_allowed("admin", "anything") is True         # ops still unrestricted
    assert policy.is_root_allowed("admin", "/srv/elsewhere") is False


def test_a_planted_symlink_cannot_smuggle_a_file_out_of_an_indexed_root(tmp_path):
    """The scoping check bounds which root a caller may NAME; it says nothing about what the
    indexer reads once inside. `os.walk` defaults to followlinks=False, which stops recursion into
    symlinked DIRECTORIES but leaves symlinked FILES in the listing — and the later open() follows
    them. A tenant able to write inside their own allowed root could therefore link to any file the
    server can read and have it indexed, embedded, and returned as a snippet: a complete bypass one
    directory level down. Demonstrated before this guard existed.
    """
    from codeintel.indexer import Indexer
    from codeintel.semantic_db import SemanticDb

    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "secret.py").write_text("def stolen():\n    return 'CANARY'\n")
    (allowed / "fine.py").write_text("def ok(): pass\n")
    (allowed / "planted.py").symlink_to(outside / "secret.py")

    db = SemanticDb(str(tmp_path / "db.sqlite"))
    db.init()
    try:
        Indexer(db).index(str(allowed))
        from codeintel.searcher import Searcher
        hits = {h.get("path") for h in Searcher(db).search("stolen secret", str(allowed))}
    finally:
        db.close()

    assert "planted.py" not in hits, "a symlink escaped the indexed root"
    assert "fine.py" in hits, "the guard must not also drop legitimate in-root files"


def test_a_blank_project_root_is_denied_rather_than_meaning_the_server_cwd(tmp_path, monkeypatch):
    """`os.path.realpath("")` is the process's cwd, not an error — so a missing project_root
    silently resolved to wherever the server was launched, and passed whenever that happened to
    sit under an allowed root."""
    from codeintel.policy import TieringPolicy

    monkeypatch.chdir(tmp_path)
    policy = TieringPolicy(enabled=True, roots={"reader": [str(tmp_path)]})

    assert policy.is_root_allowed("reader", str(tmp_path)) is True
    for blank in ("", "   ", None):
        assert policy.is_root_allowed("reader", blank or "") is False, repr(blank)


def test_a_hardlink_cannot_smuggle_a_file_into_an_indexed_root(tmp_path):
    """A hardlink is a second directory entry for the SAME inode. It sits physically inside the
    root, so its realpath is inside the root and the symlink guard passes it — `realpath` cannot
    see a hardlink at all. That reopened the exact hole the symlink guard closes: a tenant able to
    write in their own root could `ln` another tenant's file in and have it indexed. There is no
    way to ask "does this inode also live outside?", so extra links disqualify.
    """
    from codeintel.indexer import Indexer
    from codeintel.semantic_db import SemanticDb

    allowed, secret = tmp_path / "allowed", tmp_path / "secret"
    allowed.mkdir(); secret.mkdir()
    (secret / "creds.py").write_text("SECRET = 'sk-live-tenant-b'\n")
    (allowed / "fine.py").write_text("def ok(): pass\n")
    try:
        os.link(secret / "creds.py", allowed / "pulled.py")
    except OSError:                                     # cross-device or unsupported
        pytest.skip("hard links unavailable on this filesystem")

    db = SemanticDb(str(tmp_path / "db.sqlite"))
    db.init()
    try:
        names = sorted(f.name for f in Indexer(db)._walk_files(allowed))
    finally:
        db.close()

    assert "pulled.py" not in names, "a hardlink smuggled an outside file into the index"
    assert names == ["fine.py"], "the guard must not also drop ordinary in-root files"


def test_ordinary_single_link_files_are_still_indexed(tmp_path):
    """Guard the guard: the link-count check must not quietly exclude normal source files."""
    from codeintel.indexer import Indexer
    from codeintel.semantic_db import SemanticDb

    for name in ("a.py", "b.py", "c.md"):
        (tmp_path / name).write_text("x = 1\n")

    db = SemanticDb(str(tmp_path / "db.sqlite"))
    db.init()
    try:
        names = sorted(f.name for f in Indexer(db)._walk_files(tmp_path))
    finally:
        db.close()
    assert names == ["a.py", "b.py", "c.md"]


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_entry_in_a_roots_list_does_not_grant_the_server_cwd(blank, monkeypatch, tmp_path):
    """The one fail-OPEN case in an otherwise fail-closed model: `_normalize_root("")` is the
    process cwd, not "", so a stray empty string or trailing-comma artifact silently added
    "wherever the server was launched" to the allowlist."""
    from codeintel.policy import TieringPolicy

    monkeypatch.chdir(tmp_path)
    policy = TieringPolicy(enabled=True, roots={"reader": ["/srv/allowed", blank]})

    assert policy.is_root_allowed("reader", str(tmp_path)) is False
    assert policy.is_root_allowed("reader", "/srv/allowed") is True


# --------------------------------------------------------------------------- #
# Containment at READ time, not just at index time
#
# Every existing containment test plants its symlink BEFORE indexing and asserts the walk skips
# it. That shape cannot catch the real hole: containment was a property of the indexing walk, and
# nothing re-checked when the snippet was read back. A file indexed normally and swapped for a
# symlink afterwards stayed in the DB (`.exists()` follows the link, so cleanup kept the row) and
# was re-opened at query time with no check at all.
# --------------------------------------------------------------------------- #

def test_a_symlink_planted_after_indexing_cannot_be_read_back(tmp_path):
    """The post-index swap: index a real file, then point it outside the root."""
    from codeintel.containment import ContainmentError, contained_path, real_root
    from codeintel.searcher import Searcher

    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "secret.py").write_text('SECRET = "sk-live-CANARY"\n')
    bait = allowed / "bait.py"
    bait.write_text("def ok():\n    return 1\n")

    root_real = real_root(str(allowed))
    # Indexed normally: containment says yes while it is a real file inside the root.
    assert contained_path(root_real, bait) is not None

    bait.unlink()
    bait.symlink_to(outside / "secret.py")

    # The predicate now refuses it...
    assert contained_path(root_real, bait) is None
    # ...the raw read raises rather than returning bytes...
    with pytest.raises(ContainmentError):
        from codeintel.containment import open_contained
        open_contained(root_real, bait)
    # ...and the searcher's snippet path returns a refusal, never the canary.
    s = Searcher.__new__(Searcher)
    snippet = s._read_snippet(root_real, bait, 0)
    assert "CANARY" not in snippet
    assert "outside the indexed root" in snippet
    assert s._read_chunk(root_real, bait, 0) is None


def test_a_hardlink_planted_after_indexing_cannot_be_read_back(tmp_path):
    """The same swap using a hard link, whose realpath stays inside the root — so only the
    link-count check can see it."""
    from codeintel.containment import contained_path, real_root
    from codeintel.searcher import Searcher

    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "secret.py").write_text('SECRET = "sk-live-CANARY"\n')
    bait = allowed / "bait.py"
    bait.write_text("def ok():\n    return 1\n")
    root_real = real_root(str(allowed))
    assert contained_path(root_real, bait) is not None

    bait.unlink()
    os.link(outside / "secret.py", bait)          # realpath is INSIDE the root

    assert contained_path(root_real, bait) is None
    s = Searcher.__new__(Searcher)
    assert "CANARY" not in s._read_snippet(root_real, bait, 0)


def test_the_stale_row_of_a_swapped_file_is_reconciled_away(tmp_path):
    """`_cleanup_deleted` used `.exists()`, which follows a symlink to an existing target — so the
    row for a swapped file survived every reindex and kept the hole open indefinitely."""
    from codeintel.containment import contained_path, real_root

    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n")
    bait = allowed / "bait.py"
    bait.write_text("x = 1\n")
    bait.unlink(); bait.symlink_to(outside / "secret.py")

    # The old predicate says "still here" (this is the bug); the new one says "reconcile it away".
    assert (allowed / "bait.py").exists() is True
    assert contained_path(real_root(str(allowed)), allowed / "bait.py") is None


def test_containment_does_not_reject_legitimate_files(tmp_path):
    """The guard must not also drop ordinary in-root files, including through a symlinked ROOT —
    a repo reached via /var -> /private/var on macOS, or any symlinked home directory."""
    from codeintel.containment import contained_path, real_root

    real_dir = tmp_path / "real"; real_dir.mkdir()
    (real_dir / "fine.py").write_text("def ok(): pass\n")
    nested = real_dir / "pkg"; nested.mkdir()
    (nested / "deep.py").write_text("def deep(): pass\n")

    link_root = tmp_path / "via-link"
    link_root.symlink_to(real_dir)

    for root in (real_dir, link_root):
        rr = real_root(str(root))
        assert contained_path(rr, root / "fine.py") is not None
        assert contained_path(rr, root / "pkg" / "deep.py") is not None


def test_a_sibling_directory_sharing_a_prefix_is_not_inside_the_root(tmp_path):
    """`/srv/repo-secrets` must not count as inside `/srv/repo` — a plain string prefix compare
    would say it is."""
    from codeintel.containment import contained_path, real_root

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo-secrets").mkdir()
    (tmp_path / "repo-secrets" / "creds.py").write_text("TOKEN = 1\n")
    rr = real_root(str(tmp_path / "repo"))
    assert contained_path(rr, tmp_path / "repo-secrets" / "creds.py") is None


# --------------------------------------------------------------------------- #
# Root denials deny like op denials
#
# The two dimensions of the policy returned different HTTP statuses: a disallowed OP was 403, a
# disallowed ROOT was 200 with a null result. Data was withheld either way, so this was never a
# leak — but the docs promised 403, and a role scanning for roots it does not own produced a wall
# of 200s, so no 4xx-based alerting could see cross-tenant probing.
# --------------------------------------------------------------------------- #

@pytest.fixture
def root_scoped_server(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"; allowed.mkdir()
    (tmp_path / "forbidden").mkdir()
    (tmp_path / "auth.toml").write_text(
        '[roles]\nreader = ["search", "context"]\n'
        f'[roots]\nreader = ["{allowed}"]\n'
        '[tokens]\n"readertok" = "reader"\n'
    )
    monkeypatch.setenv("CODEINTEL_AUTH_CONFIG", str(tmp_path / "auth.toml"))
    from codeintel import server as srv
    srv._reset_gateway()
    s = CodeIntelHTTPServer(("127.0.0.1", 0), _Handler)
    s.token_auth = load_auth()
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1], str(allowed), str(tmp_path / "forbidden")
    s.shutdown()
    srv._reset_gateway()


def test_a_denied_root_returns_403_like_a_denied_op(root_scoped_server):
    port, _allowed, forbidden = root_scoped_server
    status, body = _query(
        port, {"op": "search", "target": "x", "project_root": forbidden}, token="readertok",
    )
    assert status == 403
    assert body["reason"] == "root-not-allowed-for-role"
    assert body["result"] is None


def test_an_allowed_root_is_not_denied(root_scoped_server):
    """The status must distinguish the two — a blanket 403 would be just as uninformative."""
    port, allowed, _forbidden = root_scoped_server
    status, body = _query(
        port, {"op": "search", "target": "x", "project_root": allowed}, token="readertok",
    )
    assert status == 200
    assert body.get("reason") != "root-not-allowed-for-role"
