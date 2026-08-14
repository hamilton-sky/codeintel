"""Doctor / preflight tests — real-boundary, never-raise, bounded.

Per this repo's philosophy (fabricated mocks hid the original contract drift), the semantic
probe is tested against a REAL temporary SemanticDb, the graph probe against captured
list_projects shapes, and the deep LSP probe against a REAL bogus subprocess (must fail-fast,
never hang). No engine check may raise, hang, load the embedding model, or mutate state.
"""
from __future__ import annotations

import os
import time

import pytest

from codeintel import doctor
from codeintel.providers.graph import GraphProvider
from codeintel.providers.lsp import LspProvider, _State
from codeintel.providers.semantic import SemanticProvider
from codeintel.provider import safe_null_result

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

def _make_db(path, project_root_real):
    from codeintel.semantic_db import SemanticDb
    db = SemanticDb(str(path))
    db.init()
    c = db.conn()
    c.execute(
        "INSERT INTO chunk_hashes(chunk_id, project_root, file_path, chunk_start, content_hash)"
        " VALUES (?,?,?,?,?)",
        ("id1", project_root_real, "f.py", 0, "hash"),
    )
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
