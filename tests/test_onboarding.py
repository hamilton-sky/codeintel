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
