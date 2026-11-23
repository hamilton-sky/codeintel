"""`callers`, `callees` and `impact` say when the graph index is older than the files they cite.

Finding `callers-callees-stale-complete`: on a stale tree, `callers helper` listed a caller in a
file edited since the index as verified, `callees compute` was complete and safe for a function
whose file no longer existed, and rows still cited a file that had been renamed.

Each test runs on a REAL throwaway git repository, a fake graph db file whose mtime is set with
`os.utime`, and the in-memory `_Graph` backend from `test_dispatch_callers.py`. The index's age is
the subject, so every test asks for the real `graph_index_age` through `real_index_age`.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from codeintel.engines.graph import changed_range
from tests.test_dispatch_callers import PROJECT, _gap_kinds, _Graph, _provider

pytestmark = pytest.mark.usefixtures("real_index_age")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str, when: float | None = None) -> None:
    env = {**os.environ, **_GIT_ENV}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                   capture_output=True, text=True, check=True, env=env)


def _write(repo: Path, rel: str, text: str, mtime: float | None = None) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def repo(tmp_path) -> Path:
    """A committed repo, committed ten minutes ago, whose files and directories are two minutes old."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py",
           "from pkg.util import leaf\n\n\ndef helper():\n    return leaf()\n\n\n"
           "def compute():\n    return helper() + leaf()\n")
    _write(root, "pkg/app.py", "from pkg.core import helper\n\n\ndef main():\n    return helper()\n")
    _write(root, "pkg/util.py", "def leaf():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base", when=time.time() - 600)
    old = time.time() - 120
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in [*filenames, *dirnames]:
            os.utime(os.path.join(dirpath, name), (old, old))
    return root


@pytest.fixture
def cache(tmp_path, monkeypatch) -> Path:
    where = tmp_path / "graph-cache"
    where.mkdir()
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(where))
    return where


def _stamp_index(cache: Path, when: float) -> None:
    db = cache / f"{PROJECT}.db"
    if not db.exists():
        db.write_bytes(b"")
    os.utime(db, (when, when))


def _graph() -> _Graph:
    g = _Graph()
    helper = f"{PROJECT}.pkg.core.helper"
    compute = f"{PROJECT}.pkg.core.compute"
    leaf = f"{PROJECT}.pkg.util.leaf"
    g.call("pkg.app.main", "pkg/app.py", helper, "pkg/core.py", callee_label="Function",
           strategy="import_map")
    g.call("pkg.core.compute", "pkg/core.py", helper, "pkg/core.py", callee_label="Function",
           strategy="same_module")
    g.call("pkg.core.helper", "pkg/core.py", leaf, "pkg/util.py", callee_label="Function",
           strategy="import_map")
    g.call("pkg.core.compute", "pkg/core.py", leaf, "pkg/util.py", callee_label="Function",
           strategy="import_map")
    return g


def _ask(monkeypatch, repo: Path, op: str, target: str) -> dict[str, Any]:
    return _provider(monkeypatch, _graph(), str(repo)).build_result(op, target, [], 30000, str(repo))


def _rows(env: dict) -> dict[tuple[str, str], dict]:
    return {(r["relation"], r["qualified_name"]): r for r in env.get("rows") or []}


def _stale(env: dict) -> dict:
    found = [g for g in env.get("gaps") or [] if g["kind"] == "stale-index"]
    assert len(found) == 1, f"want exactly one stale-index gap: {_gap_kinds(env)}"
    return found[0]


# ----------------------------------------------------------------------------------------- callers

def test_callers_row_in_a_file_edited_after_the_index_is_unstated(monkeypatch, repo, cache):
    _stamp_index(cache, time.time() - 60)
    _write(repo, "pkg/app.py", "def main():\n    return 0\n")          # edited now, after the index

    env = _ask(monkeypatch, repo, "callers", "helper")

    gap = _stale(env)
    assert "`pkg/app.py`" in gap["detail"], gap
    assert f"codeintel index {repo}" in gap["detail"], gap
    assert env["confidence"] == "partial"
    rows = _rows(env)
    main = rows[("caller", "pkg.app.main")]
    assert main["evidence"] == "unstated", main
    assert main["verified"] is False, main
    assert "changed after the index" in main["why"], main
    # A row in a file the index still describes keeps its evidence.
    assert rows[("caller", "pkg.core.compute")]["evidence"] == "resolved"
    assert env["evidence"]["safe_for_destructive"] is False
    assert env["evidence"]["unstated"] == 1


def test_callers_row_in_a_file_renamed_after_the_index_is_unstated(monkeypatch, repo, cache):
    written = time.time() - 60
    _stamp_index(cache, written)
    os.rename(repo / "pkg/app.py", repo / "pkg/application.py")
    os.utime(repo / "pkg/application.py", (written - 30, written - 30))

    env = _ask(monkeypatch, repo, "callers", "helper")

    assert "`pkg/app.py`" in _stale(env)["detail"]
    assert _rows(env)[("caller", "pkg.app.main")]["evidence"] == "unstated"
    assert env["evidence"]["safe_for_destructive"] is False


# ----------------------------------------------------------------------------------------- callees

def test_callees_of_a_target_whose_file_was_deleted_are_stale_and_not_complete(
        monkeypatch, repo, cache):
    _stamp_index(cache, time.time() - 60)
    os.remove(repo / "pkg/core.py")

    env = _ask(monkeypatch, repo, "callees", "compute")

    gap = _stale(env)
    assert "`pkg/core.py`" in gap["detail"], gap
    assert env["confidence"] != "complete"
    rows = _rows(env)
    assert rows[("callee", "pkg.core.helper")]["evidence"] == "unstated"
    # `pkg/util.py` is older than the index and still there: its row keeps its evidence.
    assert rows[("callee", "pkg.util.leaf")]["evidence"] == "resolved"
    assert env["evidence"]["safe_for_destructive"] is False


# ------------------------------------------------------------------------------------------ impact

def test_impact_has_the_stale_index_gap_once_not_once_per_half(monkeypatch, repo, cache):
    _stamp_index(cache, time.time() - 60)
    _write(repo, "pkg/app.py", "def main():\n    return 0\n")

    env = _ask(monkeypatch, repo, "impact", "helper")

    assert _gap_kinds(env).count("stale-index") == 1, _gap_kinds(env)
    rows = _rows(env)
    assert rows[("caller", "pkg.app.main")]["evidence"] == "unstated"
    assert rows[("callee", "pkg.util.leaf")]["evidence"] == "resolved"
    assert env["evidence"]["safe_for_destructive"] is False


# ------------------------------------------------------------------------------- fresh and unknown

@pytest.mark.parametrize("op, target", [("callers", "helper"), ("callees", "compute"),
                                        ("impact", "helper")])
def test_a_fresh_index_changes_nothing_in_the_answer(monkeypatch, repo, cache, op, target):
    _stamp_index(cache, time.time() - 60)

    env = _ask(monkeypatch, repo, op, target)

    assert "stale-index" not in _gap_kinds(env)
    assert "index-age-unknown" not in _gap_kinds(env)
    assert env["confidence"] == "complete", _gap_kinds(env)
    assert all(r["evidence"] == "resolved" for r in env["rows"])

    # The same question with the age check answering `fresh` outright: the body is byte-identical.
    monkeypatch.setattr(changed_range, "graph_index_age",
                        lambda project, root, paths: changed_range.IndexAge("fresh", ""))
    assert _ask(monkeypatch, repo, op, target) == env


def test_an_index_whose_age_cannot_be_read_is_index_age_unknown(monkeypatch, repo, cache):
    # No db file in the cache at all.
    env = _ask(monkeypatch, repo, "callers", "helper")

    assert "index-age-unknown" in _gap_kinds(env), _gap_kinds(env)
    assert "stale-index" not in _gap_kinds(env)
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False
