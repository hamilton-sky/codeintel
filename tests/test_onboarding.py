"""Onboarding / setup tests — real-boundary, never-raise, bounded.

Mirrors tests/test_doctor.py's philosophy: pip install and semantic indexing are exercised
against a real subprocess / a real SemanticDb rather than fabricated mocks, so a genuine
contract drift (e.g. pip's actual CLI shape) cannot hide behind a mock that agrees with itself.
"""
from __future__ import annotations

import io
import os
import time

import pytest

from codeintel import onboarding


def test_pip_install_nonexistent_never_raises():
    result = onboarding._pip_install(["codeintel-nonexistent-pkg-xyz-999"], timeout_s=30)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert "detail" in result


def test_run_setup_never_raises_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    start = time.monotonic()
    report = onboarding.run_setup(tmp_path, do_index=False, warm_lsp=False)
    elapsed = time.monotonic() - start
    assert isinstance(report, dict)
    assert "doctor" in report
    assert elapsed < 12


def test_setup_index_timeout_bounded(tmp_path, monkeypatch):
    def _slow_index(self, project_root):
        time.sleep(5)
        return 0

    monkeypatch.setattr("codeintel.indexer.Indexer.index", _slow_index)
    start = time.monotonic()
    result = onboarding._bounded_index(str(tmp_path), timeout_s=1, out=io.StringIO())
    elapsed = time.monotonic() - start
    assert result["status"] == "timeout"
    assert elapsed < 2.5


def test_guidance_for_missing_graph():
    probe = {"installed": False}
    guidance = onboarding._guidance_for("graph", probe)
    assert "codebase-memory-mcp" in guidance


def test_setup_index_real_db(tmp_path, monkeypatch):
    pytest.importorskip("fastembed")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("def greet():\n    return 'hello'\n")

    db_path = tmp_path / "semantic.db"
    monkeypatch.setattr("codeintel.semantic_db.default_db_path", lambda *a, **k: str(db_path))

    report = onboarding.run_setup(str(repo), do_index=True, out=io.StringIO())
    assert report["ok"] is True

    from codeintel.semantic_db import SemanticDb

    db = SemanticDb(str(db_path))
    try:
        real_repo = os.path.realpath(str(repo))
        row = db.conn().execute(
            "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?", (real_repo,)
        ).fetchone()
        count = row[0] if row else 0
    finally:
        db.close()
    assert count > 0


# --------------------------------------------------------------------------- #
# one-command setup (--all): idempotency, optional-graph framing, next steps
# --------------------------------------------------------------------------- #

def test_guidance_for_graph_is_optional_and_platform_aware():
    g = onboarding._guidance_for("graph", {"installed": False})
    assert "codebase-memory-mcp" in g   # (kept from the original guidance test)
    assert "OPTIONAL" in g              # framed as the optional add-on it is
    assert "/" in g                     # carries a platform tag like Darwin/arm64


def test_run_setup_skips_install_when_engine_already_installed(tmp_path, monkeypatch):
    # Idempotency: `--all`/re-runs must NOT reinstall uv when the LSP engine already reports installed.
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    monkeypatch.setattr("codeintel.providers.lsp.shutil.which", lambda x: "/fake/uvx")  # lsp installed

    def _boom(*a, **k):
        raise AssertionError("_pip_install must not run when the engine is already satisfied")
    monkeypatch.setattr(onboarding, "_pip_install", _boom)

    report = onboarding.run_setup(tmp_path, install_uv=True, do_index=False, warm_lsp=False)
    step = next(s for s in report["steps"] if s["name"] == "install uv")
    assert step["status"] == "ok"
    assert "already satisfied" in step["detail"]


def test_next_steps_lists_remaining_actions():
    doctor_report = {"engines": {
        "semantic": {"installed": True, "repo_indexed": False},
        "lsp": {"installed": False},
        "graph": {"installed": False},
    }}
    joined = " | ".join(onboarding._next_steps(doctor_report, "/repo"))
    assert "codeintel index /repo" in joined                    # indexed=False → index step
    assert "pip install uv" in joined                           # lsp missing
    assert "codebase-memory-mcp" in joined and "OPTIONAL" in joined  # graph = optional
    assert "codeintel install" in joined                        # always the last mile


def test_next_steps_all_ready_is_just_agent_registration():
    doctor_report = {"engines": {
        "semantic": {"installed": True, "repo_indexed": True},
        "lsp": {"installed": True},
        "graph": {"installed": True},
    }}
    assert onboarding._next_steps(doctor_report, "/repo") == ["Make your AI agent use it — codeintel install"]


def test_render_diagnose_only_points_to_all(monkeypatch):
    # Bare setup (no action flags) must point the user at the one-command form + show Next steps.
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    report = onboarding.run_setup("/tmp/x", do_index=False, warm_lsp=False)
    text = onboarding.render_setup_text(report)
    assert "setup --all" in text
    assert "Next:" in text
    assert "codeintel prompt" in text   # the Next block also points at the paste-to-agent handoff


def test_warm_lsp_reprobes_after_install_not_stale_preflight(tmp_path, monkeypatch):
    # The warm step must re-probe (deep) AFTER installs, not reuse the pre-install shallow preflight —
    # else a fresh machine prints "warm lsp: fail" directly under "install uv: ok".
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)

    def _probe(self, root, deep=False, timeout_s=0):
        if deep:  # the dedicated warm boot, run AFTER the install loop
            return {"installed": True, "runnable": True, "repo_indexed": None,
                    "detail": "READY (warmed)", "remediation": None}
        return {"installed": False, "runnable": False, "repo_indexed": None,
                "detail": "no uvx (preflight)", "remediation": None}
    monkeypatch.setattr("codeintel.providers.lsp.LspProvider.probe", _probe)

    report = onboarding.run_setup(tmp_path, warm_lsp=True, do_index=False, lsp_warm_timeout_s=5)
    warm = next(s for s in report["steps"] if s["name"] == "warm lsp")
    assert "READY (warmed)" in warm["detail"]   # reflects the fresh deep probe, not the stale preflight


def test_next_steps_never_raises_on_malformed_doctor():
    # Hardened like every sibling helper — malformed doctor input degrades to a list, never raises.
    for bad in ({"engines": None}, {"engines": {"semantic": "x"}}, {}, None,
                {"engines": 5}, {"engines": {"lsp": 7}}):
        assert isinstance(onboarding._next_steps(bad, "/repo"), list)
