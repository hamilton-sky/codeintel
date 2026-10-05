"""Doctor / preflight tests — real-boundary, never-raise, bounded.

Per this repo's philosophy (fabricated mocks hid the original contract drift), the semantic
probe is tested against a REAL temporary SemanticDb, the graph probe against captured
list_projects shapes, and the deep LSP probe against a REAL bogus subprocess (must fail-fast,
never hang). No engine check may raise, hang, load the embedding model, or mutate state.
"""
from __future__ import annotations

import io
import os
import time

import pytest

from codeintel import doctor
from codeintel.provider import safe_null_result
from codeintel.providers.graph import GraphProvider
from codeintel.providers.lsp import LspProvider, _State
from codeintel.providers.semantic import SemanticProvider

CAP_LIST_PROJECTS = {
    "projects": [
        {"name": "parent", "root_path": "/Users/x/Documents/project"},
        {"name": "codeintel", "root_path": "/Users/x/Documents/project/codeintel"},
    ]
}
INDEXED_ROOT = "/Users/x/Documents/project/codeintel"


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

def test_render_shows_marks_and_remediation():
    report = {
        "project_root": "/repo", "deep": False,
        "summary": {"ready": 1, "total": 3, "healthy": False},
        "engines": {
            "graph": {"engine": "graph", "status": "ok", "installed": True, "runnable": True,
                      "repo_indexed": True, "detail": "ok", "remediation": None},
            "lsp": {"engine": "lsp", "status": "warn", "installed": True, "runnable": None,
                    "repo_indexed": None, "detail": "not verified", "remediation": None},
            "semantic": {"engine": "semantic", "status": "fail", "installed": True, "runnable": True,
                         "repo_indexed": False, "detail": "0 chunks", "remediation": "codeintel index /repo"},
        },
    }
    text = doctor.render_doctor_text(report)
    assert "codeintel doctor" in text
    assert "✓" in text and "✗" in text and "▲" in text  # ▲ is the width-safe warn glyph (not ⚠)
    assert "n/a" in text  # lsp repo-indexed
    assert "codeintel index /repo" in text  # remediation surfaced (two-line fix:)
    assert "1 / 3 engines ready" in text
    assert "codeintel setup" in text  # actionable tip footer surfaces when not healthy


def test_setup_tip_footer_only_when_unhealthy():
    base = {"project_root": "/repo", "deep": False,
            "engines": {"semantic": {"engine": "semantic", "status": "ok", "installed": True,
                                     "runnable": True, "repo_indexed": True, "detail": "ok",
                                     "remediation": None}}}
    healthy = doctor.render_doctor_text({**base, "summary": {"ready": 1, "total": 1, "healthy": True}})
    unhealthy = doctor.render_doctor_text({**base, "summary": {"ready": 0, "total": 1, "healthy": False}})
    assert "codeintel setup" not in healthy   # no noise when everything is ready
    assert "codeintel setup" in unhealthy      # actionable guidance only when something is missing
    assert "codeintel prompt" not in healthy   # the agent-handoff pointer is gated the same way
    assert "codeintel prompt" in unhealthy     # → run doctor when something's off, get pointed to it


def test_treesitter_advisory_only_when_missing():
    # the silent tree-sitter fallback (found by dogfooding) must be visible: an advisory when
    # def-aligned chunking is OFF, nothing when it's on
    base = {"project_root": "/repo", "deep": False,
            "summary": {"ready": 3, "total": 3, "healthy": True},
            "engines": {"semantic": {"engine": "semantic", "status": "ok", "installed": True,
                                     "runnable": True, "repo_indexed": True, "detail": "ok",
                                     "remediation": None}}}
    off = doctor.render_doctor_text({**base, "treesitter": False})
    on = doctor.render_doctor_text({**base, "treesitter": True})
    assert "def-aligned chunking: OFF" in off and "tree-sitter-language-pack" in off
    assert "def-aligned chunking" not in on   # no noise when it's active


def test_run_doctor_reports_treesitter_availability():
    r = doctor.run_doctor("/tmp/x", deep=False)
    assert isinstance(r.get("treesitter"), bool)   # always reported (bool), never raises


def test_healthy_ignores_optional_graph_engine():
    # graph is optional (external binary) — its absence must NOT mark a repo unhealthy or exit 1.
    class _Stub:
        available = True
        def __init__(self, installed, runnable=True, indexed=True):
            self._d = {"installed": installed, "runnable": runnable, "repo_indexed": indexed,
                       "detail": "", "remediation": None}
        def probe(self, *a, **k):
            return dict(self._d)

    r = doctor.run_doctor("/repo", graph=_Stub(False, False, False),
                          lsp=_Stub(True), semantic=_Stub(True))
    assert r["engines"]["graph"]["status"] == "fail"
    assert r["summary"]["healthy"] is True          # semantic + lsp ready ⇒ healthy despite graph
    text = doctor.render_doctor_text(r)
    assert "optional" in text                        # render frames graph as optional, not a failure


# --------------------------------------------------------------------------- #
# never-raise + no-hang orchestration
# --------------------------------------------------------------------------- #

class _RaisingProvider:
    available = True

    def probe(self, *a, **k):
        raise RuntimeError("probe blew up")


def test_run_doctor_never_raises_when_a_probe_throws():
    report = doctor.run_doctor(INDEXED_ROOT, graph=_RaisingProvider(),
                               lsp=_RaisingProvider(), semantic=_RaisingProvider())
    assert report["ok"] is True
    for name in ("graph", "lsp", "semantic"):
        assert report["engines"][name]["status"] == "fail"
    assert report["summary"]["healthy"] is False


def test_run_doctor_handles_bad_project_root():
    # None / wrong-type root must not crash.
    for bad in (None, 123, object()):
        r = doctor.run_doctor(bad)
        assert r["ok"] is True and set(r["engines"]) == {"graph", "lsp", "semantic"}


def test_run_doctor_no_hang_when_graph_absent(monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    start = time.monotonic()
    r = doctor.run_doctor("/tmp/x", deep=False)  # shallow: no serena boot, no subprocess
    assert r["ok"] is True
    assert time.monotonic() - start < 10  # bounded


# --------------------------------------------------------------------------- #
# graph probe (captured list_projects shape)
# --------------------------------------------------------------------------- #

def _graph(monkeypatch, which="/fake/codebase-memory-mcp", run_ret=CAP_LIST_PROJECTS):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: which)
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda *a, **k: run_ret)
    return p


def test_graph_probe_indexed(monkeypatch):
    r = _graph(monkeypatch).probe(INDEXED_ROOT)
    assert r == {"installed": True, "runnable": True, "repo_indexed": True,
                 "project": "codeintel", "detail": r["detail"], "remediation": None}
    assert doctor._status_for(r) == "ok"


def test_graph_probe_not_indexed(monkeypatch):
    r = _graph(monkeypatch).probe("/some/other/repo")
    assert r["installed"] is True and r["runnable"] is True and r["repo_indexed"] is False
    assert "codeintel index" in r["remediation"]


def test_graph_probe_backend_unreachable(monkeypatch):
    r = _graph(monkeypatch, run_ret=None).probe(INDEXED_ROOT)  # _run None = timeout/crash
    assert r["installed"] is True and r["runnable"] is False


def test_graph_probe_not_installed(monkeypatch):
    r = _graph(monkeypatch, which=None).probe(INDEXED_ROOT)
    assert r["installed"] is False and r["runnable"] is False


# --------------------------------------------------------------------------- #
# semantic probe — REAL temporary SemanticDb (model-free, read-only)
# --------------------------------------------------------------------------- #

def _make_db(path, project_root_real, *, embedded=True):
    """A semantic index for `project_root_real`: one chunk, and by default a vector behind it.

    `embedded` exists because the two halves come apart in the wild — an index pass can chunk and
    then fail to embed — and `probe(deep=True)` now reports that state as unusable. Every test
    whose subject is something ELSE wants a complete index, or its verdict is dominated by an
    incompleteness it never meant to create.
    """
    import sqlite_vec

    from codeintel.semantic_db import SemanticDb
    db = SemanticDb(str(path))
    db.init()
    c = db.conn()
    c.execute(
        "INSERT INTO chunk_hashes(chunk_id, project_root, file_path, chunk_start, content_hash)"
        " VALUES (?,?,?,?,?)",
        ("id1", project_root_real, "f.py", 0, "hash"),
    )
    if embedded:
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS code_embeddings USING "
                  "vec0(chunk_id TEXT PRIMARY KEY, embedding float[4])")
        c.execute("INSERT INTO code_embeddings(chunk_id, embedding) VALUES (?, ?)",
                  ("id1", sqlite_vec.serialize_float32([0.1, 0.2, 0.3, 0.4])))
    c.commit()
    db.close()


def test_semantic_probe_real_db_indexed(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    db_path = tmp_path / "semantic.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_db(db_path, os.path.realpath(str(repo)))
    monkeypatch.setattr("codeintel.semantic_db.default_db_path", lambda *a, **k: str(db_path))

    r = SemanticProvider().probe(str(repo))
    assert r["installed"] is True and r["runnable"] is True and r["repo_indexed"] is True

    # A different repo shares the db file but has no rows → not indexed (project-scoped).
    other = tmp_path / "other"
    other.mkdir()
    r2 = SemanticProvider().probe(str(other))
    assert r2["runnable"] is True and r2["repo_indexed"] is False
    assert "codeintel index" in r2["remediation"]


def test_semantic_deep_probe_verifies_source_readability(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    db_path = tmp_path / "semantic.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("def readable():\n    return True\n")
    _make_db(db_path, os.path.realpath(str(repo)))
    monkeypatch.setattr("codeintel.semantic_db.default_db_path", lambda *a, **k: str(db_path))
    # This test's subject is SOURCE READABILITY, so every other input to the rollup has to be
    # pinned or it is not testing what it names. `model_cached` was not: on a machine that has
    # never downloaded the weights `_status_for` correctly returns "warn" (see its own comment —
    # an uncached model is a pending download, not a fault), and the assertion below failed for a
    # reason that has nothing to do with readability. Pin it to the state this test means.
    monkeypatch.setattr("codeintel.semantic_db.model_is_cached", lambda *a, **k: True)

    r = SemanticProvider().probe(str(repo), deep=True)

    assert r["source_readable"] is True
    assert r["source_sampled"] == 1
    assert r["source_unreadable"] == 0
    assert doctor._status_for(r) == "ok"


def test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    db_path = tmp_path / "semantic.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("def blocked():\n    return True\n")
    _make_db(db_path, os.path.realpath(str(repo)))
    monkeypatch.setattr("codeintel.semantic_db.default_db_path", lambda *a, **k: str(db_path))
    monkeypatch.setattr(
        "codeintel.containment.open_contained",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
    )

    r = SemanticProvider().probe(str(repo), deep=True)

    assert r["runnable"] is True and r["repo_indexed"] is True
    assert r["source_readable"] is False
    assert r["source_unreadable"] == 1
    assert doctor._status_for(r) == "warn"
    assert "permission" in r["remediation"].lower()


def test_semantic_probe_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    monkeypatch.setattr("codeintel.semantic_db.default_db_path", lambda *a, **k: str(tmp_path / "missing.db"))
    r = SemanticProvider().probe(str(tmp_path))
    assert r["runnable"] is True and r["repo_indexed"] is False
    assert "codeintel index" in r["remediation"]


def test_semantic_probe_corrupt_db(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    db_path = tmp_path / "semantic.db"
    db_path.write_bytes(b"this is not a sqlite database")
    monkeypatch.setattr("codeintel.semantic_db.default_db_path", lambda *a, **k: str(db_path))
    r = SemanticProvider().probe(str(tmp_path))
    assert r["installed"] is True and r["runnable"] is False  # unreadable → not runnable
    assert r["remediation"]


def test_semantic_probe_not_installed(monkeypatch):
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", False)
    r = SemanticProvider().probe("/x")
    assert r["installed"] is False and r["runnable"] is False


# --------------------------------------------------------------------------- #
# lsp probe — shallow (free) and deep (real bogus subprocess, bounded)
# --------------------------------------------------------------------------- #

def test_lsp_probe_shallow_not_installed(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: None)
    r = LspProvider().probe("/repo", deep=False)
    assert r["installed"] is False and r["runnable"] is False


def test_lsp_probe_shallow_not_checked(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    r = LspProvider().probe("/repo", deep=False)
    assert r["installed"] is True
    assert r["runnable"] is None  # no session yet → not checked (warn)
    assert r["repo_indexed"] is None  # n/a — serena has no persistent index
    assert doctor._status_for({**r, "engine": "lsp"}) == "warn"


def test_lsp_probe_shallow_reads_live_session_state(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    import threading
    fake = type("S", (), {})()
    fake.state = _State.READY
    fake._lock = threading.Lock()
    p._sessions["/repo"] = fake
    r = p.probe("/repo", deep=False)
    assert r["runnable"] is True and "READY" in r["detail"]


def test_lsp_probe_deep_boot_failure_is_bounded(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    p = LspProvider()
    p._cmd = "/nonexistent/serena-xyz-does-not-exist"  # real spawn will fail fast
    start = time.monotonic()
    r = p.probe("/repo", deep=True, timeout_s=8.0)
    elapsed = time.monotonic() - start
    assert elapsed < 8.5  # never hangs past the deadline
    assert r["installed"] is True
    assert r["runnable"] in (False, None)  # failed to boot (or timed out) — never True


def test_lsp_probe_reports_repository_access_before_blaming_network(monkeypatch):
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")
    monkeypatch.setattr(
        "codeintel.providers.lsp.os.scandir",
        lambda root: (_ for _ in ()).throw(PermissionError(1, "denied", str(root))),
    )
    p = LspProvider()
    monkeypatch.setattr(
        p, "_get_or_create_session",
        lambda root: pytest.fail("an unreadable repository must not launch serena"),
    )

    r = p.probe("/protected/repo", deep=True)

    assert r["runnable"] is False
    assert "PermissionError" in r["detail"]
    assert "/protected/repo" in r["detail"]
    assert "Privacy & Security" in r["remediation"]
    assert "network" not in r["remediation"].lower()


# --------------------------------------------------------------------------- #
# handler + hint plumbing
# --------------------------------------------------------------------------- #

def test_code_doctor_handler_envelope():
    from codeintel.server import code_doctor_handler
    r = code_doctor_handler({"project_root": os.getcwd()})
    assert r["ok"] is True
    assert set(r["engines"]) == {"graph", "lsp", "semantic"}
    assert "healthy" in r["summary"]


def test_safe_null_hint_is_optional():
    without = safe_null_result("op", "t", engine="graph", reason="x")
    assert "hint" not in without  # key absent unless set (envelope stability)
    with_hint = safe_null_result("op", "t", engine="graph", reason="x", hint="do this")
    assert with_hint["hint"] == "do this"


# --------------------------------------------------------------------------- #
# First-run failure modes: name the cause, don't send the user in a circle
# --------------------------------------------------------------------------- #

def test_an_unresolvable_home_is_not_reported_as_simply_not_indexed(monkeypatch):
    """Resolving the cache path fails when a process has no home directory — routine for a
    container running as a UID with no passwd entry, which is how coding agents are often run.
    Reporting that as "no semantic index database yet" sent the user to `codeintel index`, which
    fails the same way for the same reason: two commands, neither naming the problem."""
    from codeintel.providers.semantic import SemanticProvider

    def _boom(*a, **k):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr("codeintel.semantic_db.default_db_path", _boom)
    p = SemanticProvider()
    if not p.available:                       # fastembed/sqlite-vec absent in this environment
        pytest.skip("semantic engine not installed")
    report = p.probe("/some/repo")

    assert report["repo_indexed"] is False
    assert "no semantic index database yet" not in report["detail"]
    assert "home directory" in report["detail"] or "cache directory" in report["detail"]
    # The remediation must be actionable and must NOT be the command that fails identically.
    assert "CODEINTEL_HOME" in report["remediation"] or "HOME" in report["remediation"]
    assert report["remediation"] != "codeintel index /some/repo"


def test_codeintel_home_overrides_an_unusable_default(tmp_path, monkeypatch):
    """The override the remediation above points at has to actually exist, or the advice is a
    dead end of its own."""
    from codeintel.semantic_db import default_db_path

    monkeypatch.setenv("CODEINTEL_HOME", str(tmp_path / "cache"))
    assert default_db_path(None).startswith(str(tmp_path / "cache"))

    monkeypatch.delenv("CODEINTEL_HOME", raising=False)
    assert not default_db_path(None).startswith(str(tmp_path / "cache"))


def test_a_blank_codeintel_home_falls_back_rather_than_using_the_cwd(monkeypatch):
    """An empty env var is a common accident (`CODEINTEL_HOME=` in a shell profile); treating it
    as a path would scatter the cache into whatever directory the process started in."""
    from codeintel.semantic_db import default_db_path

    monkeypatch.setenv("CODEINTEL_HOME", "   ")
    path = default_db_path(None)
    assert path.endswith("semantic.db")
    assert not path.startswith("semantic.db")     # not a bare relative path in the cwd


def test_setup_names_the_reason_indexing_failed(tmp_path, monkeypatch):
    """The step table said only "indexer reported an unrecoverable failure" while the real cause
    sat in an unlinked stderr line above it."""
    from codeintel import onboarding

    class _FailingIndexer:
        def __init__(self, *a, **k):
            self.last_error = None

        def index(self, root):
            self.last_error = "ProxyError: 403 Forbidden"
            return -1

    monkeypatch.setattr("codeintel.indexer.Indexer", _FailingIndexer)
    monkeypatch.setattr("codeintel.semantic_db.default_db_path",
                        lambda *a, **k: str(tmp_path / "db.sqlite"))
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "a.py").write_text("def f(): pass\n")

    step = onboarding._bounded_index(str(repo), timeout_s=30.0, out=io.StringIO())
    assert step["status"] == "fail"
    assert "403 Forbidden" in step["detail"], step["detail"]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `--deep` asks a question
#
# Everything above verifies that engines BOOT. That is the shape of every instance of this
# project's recurring defect: `READY` is true of the process and false of answerability,
# `list_projects` answers in a dialect a dead index also answers in, and a chunk count is true of
# what was chunked and false of what a search can reach. Each of these drives one real query and
# requires content — and each asserts the SHALLOW probe disagrees, because a check that cannot
# separate the two states is not a check.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _graph_with_rows(rows, *, failure=None):
    """A GraphProvider that resolves cleanly and whose real queries return `rows`."""
    from codeintel.graph_backend import BackendClient
    from codeintel.graph_resolution import ProjectResolution

    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)           # type: ignore[attr-defined]
    gp.available = True                                          # type: ignore[attr-defined]
    gp._last_failure = failure                                   # type: ignore[attr-defined]
    gp._probe_wire_format = lambda name: True                    # type: ignore[method-assign]
    gp._match_project = lambda raw, root: ProjectResolution(      # type: ignore[method-assign]
        name="p", matched_root=root, scope="exact")

    def _run(method, payload, timeout_ms):
        if method == "list_projects":
            return {"projects": [{"name": "p", "root_path": "/repo"}]}
        return {"columns": ["a.name"], "rows": rows}

    gp._run = _run                                               # type: ignore[method-assign]
    return gp


def test_a_graph_project_that_resolves_but_holds_nothing_is_not_deep_runnable():
    """A registration whose index is empty answers `list_projects` and the wire-format probe
    perfectly, and then answers every real question with nothing.

    `_probe_wire_format` cannot catch it and is not supposed to: it asks whether the REPLY is
    readable, and an empty result set is a readable reply. The shallow verdict is kept as-is so
    the difference between the two is visible rather than asserted."""
    gp = _graph_with_rows([])

    assert gp.probe("/repo")["runnable"] is True, "the shallow probe is not the thing under test"

    deep = gp.probe("/repo", deep=True)
    assert deep["runnable"] is False, deep
    assert deep["repo_indexed"] is True, "the registration does exist — that part was true"
    assert "no rows" in deep["detail"], deep
    assert "codeintel index" in (deep["remediation"] or ""), deep


def test_a_graph_project_with_rows_stays_runnable_under_deep():
    """The positive half. A check that fires on healthy repositories is one nobody runs twice."""
    deep = _graph_with_rows([["something"]]).probe("/repo", deep=True)
    assert deep["runnable"] is True, deep
    assert "answered a real query" in deep["detail"], deep


def test_a_graph_verification_query_that_fails_is_unknown_not_empty():
    """`outcome.py`'s rule at the health check: could-not-ask and answered-nothing are different
    facts, and only one of them means the engine is broken."""
    from codeintel.outcome import Missing

    gp = _graph_with_rows([], failure=Missing("timeout", "the backend did not answer"))
    deep = gp.probe("/repo", deep=True)
    assert deep["runnable"] is None, deep
    assert "unknown" in deep["detail"], deep


def _ready_lsp(repo, *, overview):
    """An LspProvider with a READY session whose `get_symbols_overview` returns `overview`."""
    from codeintel.outcome import Ok

    lsp = LspProvider.__new__(LspProvider)
    lsp.available = True                                         # type: ignore[attr-defined]
    lsp._cmd = "uvx"                                             # type: ignore[attr-defined]

    class _Ready:
        state = _State.READY

        class _Lock:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        _lock = _Lock()

    session = _Ready()
    lsp._sessions = {str(repo): session}                         # type: ignore[attr-defined]
    lsp._get_or_create_session = lambda root: session            # type: ignore[method-assign]
    class _Block:
        def __init__(self, text): self.text = text

    class _Result:
        content = [_Block(overview)] if overview is not None else []

    lsp._call_tool = lambda s, tool, args, t: Ok(_Result())      # type: ignore[method-assign]
    return lsp


def _python_repo(tmp_path, n=6):
    repo = tmp_path / "py"
    repo.mkdir()
    for i in range(n):
        (repo / f"m{i}.py").write_text("def f():\n    return 1\n")
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text(
        "language_servers:\n  - python\n", encoding="utf-8")
    return repo


def test_a_ready_language_server_that_answers_nothing_is_not_deep_runnable(tmp_path):
    """The exact state that reported `3 / 3 engines ready` over a tree whose every reference
    lookup came back empty. The config is correct here — the language IS served — so neither
    configuration check fires and only asking catches it."""
    repo = _python_repo(tmp_path)
    lsp = _ready_lsp(repo, overview="{}")

    shallow = lsp.probe(str(repo), deep=False)
    assert shallow["runnable"] is True, "the shallow probe reads READY and stops — as before"

    deep = lsp.probe(str(repo), deep=True, timeout_s=2.0)
    assert deep["runnable"] is False, deep
    assert "no symbols" in deep["detail"] or "returned nothing" in deep["detail"], deep
    assert deep["remediation"], "an engine that answers nothing must name a next action"


def test_a_ready_language_server_that_answers_stays_runnable(tmp_path):
    repo = _python_repo(tmp_path)
    lsp = _ready_lsp(repo, overview='{"functions": ["f"]}')

    deep = lsp.probe(str(repo), deep=True, timeout_s=2.0)
    assert deep["runnable"] is True, deep
    assert "answered a real query" in deep["detail"], deep


def test_the_lsp_verification_picks_a_file_the_config_actually_serves(tmp_path):
    """The subject has to be knowable from the tree. Guessing a SYMBOL name would make an empty
    answer ambiguous — absent symbol, or absent engine — which is the ambiguity being removed."""
    repo = tmp_path / "poly"
    repo.mkdir()
    for i in range(6):
        (repo / f"m{i}.py").write_text("x = 1\n")
    for i in range(3):
        (repo / f"t{i}.ts").write_text("export const x = 1\n")
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text(
        "language_servers:\n  - python\n", encoding="utf-8")

    chosen = LspProvider.__new__(LspProvider)._a_served_source_file(str(repo))
    assert chosen and chosen.endswith(".py"), chosen

    # Nothing served ⇒ no question to ask, which is "could not ask", never "answered no".
    bare = tmp_path / "bare"
    bare.mkdir()
    assert LspProvider.__new__(LspProvider)._a_served_source_file(str(bare)) is None


def _semantic_db(tmp_path, *, with_vectors):
    """A real SemanticDb file holding one chunk for `/repo`, with or without a vector behind it."""
    import sqlite3

    import sqlite_vec

    path = str(tmp_path / f"s-{'with' if with_vectors else 'without'}.db")
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("CREATE TABLE chunk_hashes (chunk_id TEXT, project_root TEXT, file_path TEXT)")
    conn.execute("INSERT INTO chunk_hashes VALUES ('c1','/repo','a.py')")
    conn.execute("CREATE VIRTUAL TABLE code_embeddings USING "
                 "vec0(chunk_id TEXT PRIMARY KEY, embedding float[4])")
    if with_vectors:
        conn.execute("INSERT INTO code_embeddings(chunk_id, embedding) VALUES (?, ?)",
                     ("c1", sqlite_vec.serialize_float32([0.1, 0.2, 0.3, 0.4])))
    conn.commit()
    conn.close()
    return path


def test_semantic_chunks_without_vectors_are_not_a_working_index(tmp_path):
    """`N indexed chunks` counts `chunk_hashes`. A search reads `code_embeddings`. An index pass
    that chunked and then failed to embed leaves the first full and the second empty, and the
    count reports that as healthy."""
    pytest.importorskip("sqlite_vec")
    from codeintel.providers.semantic import _deep_answer

    answered, why = _deep_answer(_semantic_db(tmp_path, with_vectors=False), "/repo", "m", True)
    assert answered is False, why
    assert "code_embeddings" in why, why

    answered, why = _deep_answer(_semantic_db(tmp_path, with_vectors=True), "/repo", "m", True)
    assert answered is True, why


def test_semantic_without_cached_weights_reports_unknown_rather_than_broken(tmp_path):
    """The weights being absent says nothing about the index. Reporting it as a failure would send
    a user to `reset && index`, which cannot fix it."""
    pytest.importorskip("sqlite_vec")
    from codeintel.providers.semantic import _deep_answer

    answered, why = _deep_answer(
        _semantic_db(tmp_path, with_vectors=True), "/repo", "bge-small", model_cached=None)
    assert answered is None, why
    assert "not cached" in why, why


def test_doctor_deep_reaches_every_engine():
    """`--deep` used to reach two of the three: `run_doctor` called the graph probe with no `deep`
    at all, so the engine whose index can be empty was the one never asked."""
    import inspect

    source = inspect.getsource(doctor.run_doctor)
    for engine in ("graph", "lsp", "semantic"):
        call = next(line for line in source.splitlines()
                    if f'"{engine}", {engine}' in line or ("p.probe(root" in line and engine in line))
        assert call, engine
    assert source.count("deep=deep") >= 2 and "p.probe(root, deep=deep)" in source, source


# --------------------------------------------------------------------------- #
# `healthy` ignores the optional engine, so an installed-but-broken one needs its own field
#
# `healthy` answers "can this repo be worked on" and leaves the graph engine out on purpose: a
# machine with no graph binary is fully usable. That conflated "not installed" with "installed and
# refusing to run", and the second was reported as `healthy: true` with `graph: true` — an agent
# reading the status payload had no field that said the graph engine would answer every question
# with nothing. `degraded` is that field. These pin it, and pin that `healthy` did not move.
# --------------------------------------------------------------------------- #

class _Row:
    """A provider whose probe returns one fixed row — the doctor's own contract, no subprocess."""

    available = True

    def __init__(self, *, installed=True, runnable=True, indexed=True, detail="", remediation=None):
        self._row = {"installed": installed, "runnable": runnable, "repo_indexed": indexed,
                     "detail": detail, "remediation": remediation}

    def probe(self, *a, **k):
        return dict(self._row)


_REFUSING_GRAPH = {
    "installed": True, "runnable": False, "indexed": False,
    "detail": "codebase-memory-mcp is installed but refused to run `list_projects` (exit 1): "
              "CBM CLI could not start",
    "remediation": "close every codebase-memory-mcp process",
}


def test_an_installed_graph_that_refuses_to_run_is_degraded_while_the_repo_stays_healthy():
    r = doctor.run_doctor("/repo", graph=_Row(**_REFUSING_GRAPH), lsp=_Row(), semantic=_Row())

    assert r["engines"]["graph"]["status"] == "fail"
    assert r["degraded"] == ["graph"], r["degraded"]
    assert r["summary"]["healthy"] is True, "`healthy` ignores the optional engine, and still does"
    assert (r["summary"]["ready"], r["summary"]["total"]) == (2, 3)

    text = doctor.render_doctor_text(r)
    assert "healthy, but graph degraded" in text, text
    assert "CBM CLI could not start" in text, "the backend's own message is in the note"
    assert "works without it" not in text, (
        "the 'optional — codeintel works without it' reassurance is about an ABSENT backend; "
        "printed under one that refused to start it told the reader nothing true that mattered")


def test_nothing_is_degraded_when_every_engine_is_fine():
    r = doctor.run_doctor("/repo", graph=_Row(), lsp=_Row(), semantic=_Row())

    assert r["degraded"] == []
    assert r["summary"]["healthy"] is True
    assert "degraded" not in doctor.render_doctor_text(r)


def test_an_absent_optional_engine_is_not_degraded():
    r = doctor.run_doctor("/repo", graph=_Row(installed=False, runnable=False, indexed=False),
                          lsp=_Row(), semantic=_Row())

    assert r["degraded"] == [], "not installed is a different state, and the fine one"
    assert r["summary"]["healthy"] is True
    text = doctor.render_doctor_text(r)
    assert "optional" in text and "degraded" not in text


def test_a_required_engine_that_is_installed_and_failing_is_degraded_and_unhealthy():
    r = doctor.run_doctor("/repo", graph=_Row(), lsp=_Row(),
                          semantic=_Row(installed=True, runnable=True, indexed=False))

    assert r["degraded"] == ["semantic"]
    assert r["summary"]["healthy"] is False
    text = doctor.render_doctor_text(r)
    assert "semantic degraded" in text and "healthy, but" not in text, (
        "an unhealthy repo must not be introduced with 'healthy, but'")


def test_a_probe_that_raised_is_not_called_degraded():
    """A raising probe reports `installed: None` — nobody could tell. Calling it degraded would
    assert something this report has no evidence for."""
    r = doctor.run_doctor("/repo", graph=_RaisingProvider(), lsp=_Row(), semantic=_Row())
    assert r["engines"]["graph"]["status"] == "fail"
    assert r["degraded"] == []


def _gateway_with(graph, lsp, semantic):
    import types

    return types.SimpleNamespace(
        graph=graph, lsp=lsp, semantic=semantic,
        allows_root=lambda role, root: True, adopt_provider=lambda engine, provider: None,
    )


def test_code_status_carries_degraded_and_the_flat_flags_still_mean_installed(monkeypatch):
    from codeintel import server

    monkeypatch.setattr(server, "_get_gateway", lambda: _gateway_with(
        _Row(**_REFUSING_GRAPH), _Row(), _Row()))
    status = server.code_status_handler({"project_root": "/repo"})

    assert status["degraded"] == ["graph"], status
    assert status["healthy"] is True, "the meaning of `healthy` did not change"
    assert status["graph"] is True, "the flat flag is INSTALLED, which is what it always meant"
    assert status["readiness"]["graph"]["status"] == "fail", (
        "the row that contradicts the flat flag — and the reason `degraded` exists")
    assert "CBM CLI could not start" in status["readiness"]["graph"]["detail"]


def test_code_status_degraded_is_empty_when_every_engine_is_fine(monkeypatch):
    from codeintel import server

    monkeypatch.setattr(server, "_get_gateway", lambda: _gateway_with(_Row(), _Row(), _Row()))
    status = server.code_status_handler({"project_root": "/repo"})
    assert status["degraded"] == [] and status["healthy"] is True


def test_the_status_fallback_and_the_refused_doctor_report_carry_the_key():
    """Shape stability: a caller reading `degraded` must not KeyError on the degraded path, or the
    check stops running exactly when things are worst. The value is `None` — "could not tell" — and
    NOT `[]`, which is what a report that actually looked says when nothing is failing."""
    from codeintel import server

    assert "degraded" in server._STATUS_FALLBACK and server._STATUS_FALLBACK["degraded"] is None
    gw = server._get_gateway()
    gw.allows = lambda role, op: False              # type: ignore[method-assign]
    denied = server._code_doctor_handler_inner({"project_root": "/repo"})
    assert "degraded" in denied and denied["degraded"] is None


def _cli_args(**kw):
    import argparse

    return argparse.Namespace(project_root=None, json=False, deep=False, **kw)


def test_codeintel_status_says_healthy_but_graph_degraded(monkeypatch, capsys):
    from importlib import import_module

    monkeypatch.setattr("codeintel.server.code_status_handler", lambda args: {
        "readiness": {
            "graph": {"status": "fail", "detail": "refused to run (exit 1): CBM CLI could not start"},
            "lsp": {"status": "ok", "detail": "ok"},
            "semantic": {"status": "ok", "detail": "ok"},
        },
        "healthy": True, "degraded": ["graph"],
    })
    assert import_module("codeintel.commands.status").run(_cli_args()) == 0
    out = capsys.readouterr().out

    assert "healthy, but graph degraded" in out, out
    assert "codeintel doctor" in out, "the way to the reason"


def test_codeintel_status_prints_no_degraded_line_when_nothing_is(monkeypatch, capsys):
    from importlib import import_module

    monkeypatch.setattr("codeintel.server.code_status_handler", lambda args: {
        "readiness": {"graph": {"status": "ok", "detail": "ok"}}, "healthy": True, "degraded": [],
    })
    assert import_module("codeintel.commands.status").run(_cli_args()) == 0
    assert "degraded" not in capsys.readouterr().out


def test_codeintel_doctor_says_healthy_but_graph_degraded_and_exits_zero(monkeypatch, capsys):
    """The exit code is the other half: `healthy` gates scripts and CI, and it is unchanged — the
    degradation is for the reader, not a new way to fail a pipeline."""
    from importlib import import_module

    real = doctor.run_doctor
    monkeypatch.setattr(
        doctor, "run_doctor",
        lambda root, deep=False, **k: real(
            root, deep=deep, graph=_Row(**_REFUSING_GRAPH), lsp=_Row(), semantic=_Row()))
    args = _cli_args()
    args.project_root = "/repo"
    assert import_module("codeintel.commands.doctor").run(args) == 0
    assert "healthy, but graph degraded" in capsys.readouterr().out
