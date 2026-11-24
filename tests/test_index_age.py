"""`changed` checks the graph index's age in every mode, against HEAD's commit time, with no slack.

Each test reproduces one red-team finding (`.feature-flow/state/redblue-findings.md`) on a REAL
throwaway git repository, a fake graph db file whose mtime is set with `os.utime`, and a fake
backend (`_run`/`_query_rows` monkeypatched). The index's age is the subject here, so every test
asks for the real `graph_index_age` through `real_index_age` instead of the suite's fresh stub.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from codeintel.engines.graph.provider import GraphProvider

pytestmark = pytest.mark.usefixtures("real_index_age")

PROJECT = "tmp-age"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

# The symbols the fake index holds, per file. A file that is not here has 0 indexed symbols.
_INDEXED = {
    "pkg/core.py": ["helper", "compute"],
    "pkg/app.py": ["main"],
}


def _git(repo: Path, *args: str, when: float | None = None) -> str:
    env = {**os.environ, **_GIT_ENV}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], capture_output=True,
        text=True, check=True, env=env)
    return done.stdout


def _write(repo: Path, rel: str, text: str, mtime: float | None = None) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _age_all(repo: Path, when: float) -> None:
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            os.utime(os.path.join(dirpath, f), (when, when))


def _files(repo: Path) -> None:
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/core.py", "def helper():\n    return 1\n\n\ndef compute():\n    return helper()\n")
    _write(repo, "pkg/app.py", "from pkg.core import helper\n\n\ndef main():\n    return helper()\n")


@pytest.fixture
def repo(tmp_path) -> Path:
    """A committed repo whose files are two minutes old, committed ten minutes ago."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _files(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base", when=time.time() - 600)
    _age_all(root, time.time() - 120)
    return root


def _stamp_index(cache: Path, when: float) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / f"{PROJECT}.db"
    if not db.exists():
        db.write_bytes(b"")
    os.utime(db, (when, when))
    return db


def _edge(callee: str, callee_file: str, caller_qn: str, caller_file: str) -> dict:
    callee_qn = f"{PROJECT}.{callee_file[:-3].replace('/', '.')}.{callee}"
    return {
        "a.name": caller_qn.rsplit(".", 1)[-1], "a.qualified_name": f"{PROJECT}.{caller_qn}",
        "a.file_path": caller_file, "labels(a)": "Function", "type(c)": "CALLS",
        "c.confidence": "0.95", "strategy": "lsp_direct",
        "b.name": callee, "b.qualified_name": callee_qn, "b.file_path": callee_file,
    }


_EDGES = {"helper": [_edge("helper", "pkg/core.py", "pkg.app.main", "pkg/app.py"),
                     _edge("helper", "pkg/core.py", "pkg.core.compute", "pkg/core.py")]}


def _detect_changes(root: Path) -> dict:
    """What the backend reports for uncommitted changes: the files `git status` lists (nothing
    outside a git work tree) and the INDEXED symbols defined in them."""
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True)
    files = [ln[3:].strip() for ln in out.stdout.splitlines()] if out.returncode == 0 else []
    syms = [{"name": n, "qualified_name": f"{PROJECT}.{f[:-3].replace('/', '.')}.{n}",
             "file_path": f} for f in files for n in _INDEXED.get(f, [])]
    return {"changed_files": files, "changed_count": len(files), "impacted_symbols": syms}


def _provider(monkeypatch, tmp_path: Path, root: Path) -> GraphProvider:
    monkeypatch.setattr(
        "codeintel.engines.graph.provider.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(tmp_path / "graph-cache"))
    p = GraphProvider()
    listing = {"projects": [{"name": PROJECT, "root_path": str(root)}]}

    def run(method, payload, timeout_ms):
        if method == "list_projects":
            return listing
        if method == "detect_changes":
            return _detect_changes(root)
        return None

    def rows(cypher, project, timeout_ms):
        if "-[" in cypher:
            m = re.search(r'b\.name="([^"]+)"', cypher)
            return [dict(r) for r in _EDGES.get(m.group(1), [])] if m else []
        m = re.search(r'n\.name="([^"]+)"', cypher)
        return [] if not m else [
            {"n.qualified_name": f"{PROJECT}.{f[:-3].replace('/', '.')}.{m.group(1)}",
             "n.file_path": f} for f, names in _INDEXED.items() if m.group(1) in names]

    monkeypatch.setattr(p, "_run", run)
    monkeypatch.setattr(p, "_query_rows", rows)
    return p


def _ask(p: GraphProvider, root: Path, target: str = "") -> dict[str, Any]:
    return p.build_result("changed", target, [], 30000, str(root))


def _gap_kinds(env: dict) -> list[str]:
    return [g["kind"] for g in env.get("gaps") or []]


# ============================================================ uncommitted-changed-no-staleness

def test_an_uncommitted_edit_with_no_reindex_is_stale_and_partial(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo)
    _stamp_index(tmp_path / "graph-cache", time.time() - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef brand_new():\n    return 3\n")

    env = _ask(p, repo)

    assert env["result"] is not None, env
    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert env["confidence"] == "partial", env.get("confidence")
    assert (env.get("evidence") or {}).get("safe_for_destructive") is not True


def test_an_untracked_file_the_index_has_no_symbols_for_is_stale(monkeypatch, tmp_path, repo):
    """Older than the index on purpose: it is the 0 indexed symbols, not the mtime, that tells."""
    p = _provider(monkeypatch, tmp_path, repo)
    written = time.time() - 60
    _stamp_index(tmp_path / "graph-cache", written)
    _write(repo, "pkg/brandnew.py", "def untracked_fn():\n    return 1\n", mtime=written - 30)

    env = _ask(p, repo)

    assert env["result"] is not None, env
    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert "pkg/brandnew.py" in next(g["detail"] for g in env["gaps"] if g["kind"] == "stale-index")
    assert env["confidence"] == "partial"


def test_an_uncommitted_edit_older_than_the_index_raises_no_staleness_gap(
        monkeypatch, tmp_path, repo):
    """The negative control: an index written after the edit is fresh."""
    p = _provider(monkeypatch, tmp_path, repo)
    now = time.time()
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n",
           mtime=now - 90)
    _stamp_index(tmp_path / "graph-cache", now - 60)

    env = _ask(p, repo)

    assert "Changes impact" in env["result"], env
    kinds = _gap_kinds(env)
    assert "stale-index" not in kinds and "index-age-unknown" not in kinds, kinds


def test_an_uncommitted_answer_with_no_graph_db_says_its_age_is_unknown(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n")

    env = _ask(p, repo)

    assert "index-age-unknown" in _gap_kinds(env), env.get("gaps")
    assert env["confidence"] == "partial"


def test_a_clean_git_tree_still_says_working_tree_clean(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo)
    _stamp_index(tmp_path / "graph-cache", time.time() - 60)

    env = _ask(p, repo)

    assert env["result"] == "## Changes impact\n(working tree clean — no uncommitted changes)"


# ============================================================ nongit-uncommitted-claims-clean

def test_uncommitted_changed_outside_a_git_repo_is_not_a_git_repo(monkeypatch, tmp_path):
    plain = tmp_path / "plain"
    _files(plain)
    p = _provider(monkeypatch, tmp_path, plain)
    _stamp_index(tmp_path / "graph-cache", time.time() - 60)

    env = _ask(p, plain)

    assert env.get("reason") == "not-a-git-repo", env
    assert env.get("outcome") == "unavailable", env
    assert env["result"] is None
    assert "git work tree" in (env.get("hint") or ""), env.get("hint")


# ===================================================== index-age-misses-out-of-diff-commits

def test_a_commit_after_the_index_is_stale_even_when_no_file_in_the_diff_is(
        monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo)
    now = time.time()
    _write(repo, "pkg/core.py", "def helper():\n    return 5\n\n\ndef compute():\n    return helper()\n")
    _write(repo, "pkg/app3.py", "from pkg.core import helper\n\n\ndef new_caller():\n    return helper()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "after the index", when=now - 10)
    _age_all(repo, now - 120)
    db = _stamp_index(tmp_path / "graph-cache", now - 60)
    assert int(_git(repo, "log", "-1", "--format=%ct").strip()) > os.path.getmtime(db)

    env = _ask(p, repo, "HEAD~1")

    assert "## Changes since" in env["result"], env
    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert "HEAD (a commit)" in next(g["detail"] for g in env["gaps"] if g["kind"] == "stale-index")
    assert env["evidence"]["safe_for_destructive"] is False


# ==================================================================== index-age-2s-slack

def test_an_edit_a_tenth_of_a_second_after_the_index_is_stale(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo)
    written = time.time() - 60
    _stamp_index(tmp_path / "graph-cache", written)
    _write(repo, "pkg/core.py", "def helper():\n    return 9\n\n\ndef compute():\n    return helper()\n",
           mtime=written + 0.1)

    env = _ask(p, repo, "HEAD")

    assert "## Changes since" in env["result"], env
    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is False


# ================================================================ one place finds the graph db

def test_the_stamp_is_zero_when_the_cache_directory_is_missing(monkeypatch, tmp_path):
    from codeintel.core.paths import graph_db_stamp
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(tmp_path / "nowhere"))
    assert graph_db_stamp(str(tmp_path)) == 0


def test_the_stamp_is_the_newest_of_the_db_and_its_wal(monkeypatch, tmp_path):
    from codeintel.core.paths import graph_db_stamp, graph_slug
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    root = tmp_path / "some-repo"
    root.mkdir()
    db = cache / f"{graph_slug(os.path.realpath(root))}.db"
    db.write_bytes(b"")
    os.utime(db, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"")
    os.utime(wal, ns=(1_000_000_000_500_000_000, 1_000_000_000_500_000_000))
    (cache / "other-project.db").write_bytes(b"")
    os.utime(cache / "other-project.db", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))

    assert graph_db_stamp(str(root)) == 1_000_000_000_500_000_000


def test_a_root_answered_from_an_ancestor_project_still_gets_a_stamp(monkeypatch, tmp_path):
    from codeintel.core.paths import graph_db_stamp
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    (cache / "parent.db").write_bytes(b"")
    os.utime(cache / "parent.db", ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    (cache / "parent.db-wal").write_bytes(b"")
    os.utime(cache / "parent.db-wal", ns=(1_200_000_000_000_000_000, 1_200_000_000_000_000_000))

    assert graph_db_stamp(str(tmp_path / "sub")) == 1_200_000_000_000_000_000


def test_the_db_mtime_of_a_project_id(monkeypatch, tmp_path):
    from codeintel.core.paths import graph_db_mtime
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    assert graph_db_mtime(PROJECT) is None
    _stamp_index(cache, 1_000_000.0)
    assert graph_db_mtime(PROJECT) == 1_000_000.0


def test_the_reset_slug_is_the_shared_one():
    from codeintel.core.paths import graph_slug
    from codeintel.setup.reset import _graph_slug
    assert _graph_slug("/tmp/-Weird/repo") == graph_slug("/tmp/-Weird/repo") == "tmp-Weird-repo"


# ========================================================== git status fails (review round 1)

def test_a_failed_git_status_is_index_age_unknown_never_fresh(monkeypatch, tmp_path, repo):
    """Without the list of uncommitted files there is nothing to date them by, so the answer
    cannot say the index is current: a timeout on a large repo must not read as "fresh"."""
    from codeintel.engines.graph import changed_range
    real_git = changed_range._git

    def git(root, *args):
        if args and args[0] == "status":
            return 1, b"", "fatal: simulated status failure"
        return real_git(root, *args)

    p = _provider(monkeypatch, tmp_path, repo)
    _stamp_index(tmp_path / "graph-cache", time.time() - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n")
    monkeypatch.setattr(changed_range, "_git", git)

    env = _ask(p, repo)

    assert env["result"] is not None, env
    assert "index-age-unknown" in _gap_kinds(env), env.get("gaps")
    detail = next(g["detail"] for g in env["gaps"] if g["kind"] == "index-age-unknown")
    assert "git status" in detail and "simulated status failure" in detail, detail
    assert env["confidence"] == "partial"


def test_an_edit_outside_the_answered_project_does_not_make_its_index_stale(
        monkeypatch, tmp_path, repo):
    """The project is a subdirectory of the repository: an edit beside it is not in its index."""
    sub = repo / "pkg"
    now = time.time()
    _stamp_index(tmp_path / "graph-cache", now - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n",
           mtime=now - 90)
    _write(repo, "elsewhere.py", "def other():\n    return 1\n", mtime=now - 10)
    _git(repo, "add", "elsewhere.py")
    p = _provider(monkeypatch, tmp_path, sub)

    env = _ask(p, sub)

    assert "Changes impact" in env["result"], env
    kinds = _gap_kinds(env)
    assert "stale-index" not in kinds and "index-age-unknown" not in kinds, env.get("gaps")


def test_an_edit_inside_the_answered_project_still_makes_its_index_stale(
        monkeypatch, tmp_path, repo):
    sub = repo / "pkg"
    now = time.time()
    _stamp_index(tmp_path / "graph-cache", now - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n",
           mtime=now - 10)
    p = _provider(monkeypatch, tmp_path, sub)

    env = _ask(p, sub)

    assert "stale-index" in _gap_kinds(env), env.get("gaps")


# ============================================================ the stamp never raises (round 1)

def test_an_unreadable_cache_directory_gives_a_zero_stamp(monkeypatch, tmp_path):
    from codeintel.core import paths
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(paths.os, "scandir", denied)
    assert paths.graph_db_stamp(str(tmp_path / "repo")) == 0


def test_an_unreadable_db_file_does_not_raise_from_the_stamp(monkeypatch, tmp_path):
    from codeintel.core import paths
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    root = tmp_path / "repo"
    root.mkdir()
    db = cache / f"{paths.graph_slug(os.path.realpath(root))}.db"
    db.write_bytes(b"")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path).endswith((".db", ".db-wal")):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(paths.os, "stat", stat)
    assert paths.graph_db_stamp(str(root)) == 0


# ============================================== HEAD's commit time is read safely (review round 2)

def _signed_commit_after_the_index(repo: Path, when: float, marker: Path) -> None:
    """Make HEAD a commit carrying a (fake) PGP signature, dated *when*, and give the repository's
    own config `log.showSignature=true` and a `gpg.program` that leaves *marker* when git runs it.
    The commit is hand-made with `hash-object`, so no real signing key is needed."""
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    parent = _git(repo, "rev-parse", "HEAD").strip()
    body = (f"tree {tree}\nparent {parent}\nauthor t <t@t> {int(when)} +0000\n"
            f"committer t <t@t> {int(when)} +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----\n"
            "\nsigned after the index\n")
    obj = repo.parent / "signed-commit.txt"
    obj.write_text(body)
    sha = _git(repo, "hash-object", "-t", "commit", "-w", str(obj)).strip()
    _git(repo, "update-ref", "-m", "commit: signed after the index", "HEAD", sha)
    gpg = repo.parent / "fake-gpg"
    gpg.write_text(f"#!/bin/sh\ntouch '{marker}'\necho 'gpg: Signature made by nobody' >&2\nexit 1\n")
    gpg.chmod(0o755)
    _git(repo, "config", "gpg.program", str(gpg))
    _git(repo, "config", "log.showSignature", "true")


def test_a_signed_commit_after_the_index_is_stale_under_log_show_signature(
        monkeypatch, tmp_path, repo):
    """`log.showSignature=true` puts verification output before `%ct` on stdout. Read without
    `--no-show-signature`, the time did not parse and was skipped, so a commit after the index read
    as fresh; and git ran the verifier the repository's own config names."""
    marker = tmp_path / "verifier-ran"
    now = time.time()
    _signed_commit_after_the_index(repo, now - 10, marker)
    _stamp_index(tmp_path / "graph-cache", now - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n",
           mtime=now - 90)
    p = _provider(monkeypatch, tmp_path, repo)

    env = _ask(p, repo)

    assert "Changes impact" in env["result"], env
    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert "HEAD (a commit)" in next(g["detail"] for g in env["gaps"] if g["kind"] == "stale-index")
    assert not marker.exists(), "git ran the repository's gpg.program to read HEAD's commit time"


def test_an_unreadable_head_commit_time_is_unknown_never_skipped(monkeypatch, tmp_path, repo):
    from codeintel.engines.graph import changed_range
    real_git = changed_range._git

    def git(root, *args):
        if args and args[0] == "log":
            return 0, b"something that is not a timestamp\n", ""
        return real_git(root, *args)

    now = time.time()
    _stamp_index(tmp_path / "graph-cache", now - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n",
           mtime=now - 90)
    p = _provider(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(changed_range, "_git", git)

    env = _ask(p, repo)

    assert "index-age-unknown" in _gap_kinds(env), env.get("gaps")
    assert "HEAD" in next(g["detail"] for g in env["gaps"] if g["kind"] == "index-age-unknown")
    assert env["confidence"] == "partial"


def test_a_repo_with_no_commit_yet_still_dates_its_files(monkeypatch, tmp_path):
    """No HEAD is not a failure to read HEAD: the staged files alone date the tree."""
    root = tmp_path / "unborn"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _files(root)
    _git(root, "add", "-A")
    now = time.time()
    _age_all(root, now - 120)
    _stamp_index(tmp_path / "graph-cache", now - 60)
    p = _provider(monkeypatch, tmp_path, root)

    env = _ask(p, root)

    assert "Changes impact" in env["result"], env
    kinds = _gap_kinds(env)
    assert "stale-index" not in kinds and "index-age-unknown" not in kinds, env.get("gaps")


# ===================================== "working tree clean" only when git agrees (review round 2)

def _backend_reports_nothing(monkeypatch, p: GraphProvider) -> None:
    run = p._run

    def quiet(method, payload, timeout_ms):
        if method == "detect_changes":
            return {"changed_files": [], "changed_count": 0, "impacted_symbols": []}
        return run(method, payload, timeout_ms)

    monkeypatch.setattr(p, "_run", quiet)


def test_a_failed_git_status_never_says_working_tree_clean(monkeypatch, tmp_path, repo):
    from codeintel.engines.graph import changed_range
    real_git = changed_range._git

    def git(root, *args):
        if args and args[0] == "status":
            return 1, b"", "fatal: simulated status failure"
        return real_git(root, *args)

    _stamp_index(tmp_path / "graph-cache", time.time() - 60)
    p = _provider(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(changed_range, "_git", git)

    env = _ask(p, repo)

    assert env["result"] is not None, env
    assert "working tree clean" not in env["result"], env["result"]
    assert "index-age-unknown" in _gap_kinds(env), env.get("gaps")
    assert env["confidence"] == "partial"


def test_a_source_edit_git_lists_but_the_backend_misses_is_stale_not_clean(
        monkeypatch, tmp_path, repo):
    now = time.time()
    _stamp_index(tmp_path / "graph-cache", now - 60)
    _write(repo, "pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return 0\n",
           mtime=now - 90)
    p = _provider(monkeypatch, tmp_path, repo)
    _backend_reports_nothing(monkeypatch, p)

    env = _ask(p, repo)

    assert env["result"] is not None, env
    assert "working tree clean" not in env["result"], env["result"]
    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert "pkg/core.py" in next(g["detail"] for g in env["gaps"] if g["kind"] == "stale-index")
    assert env["confidence"] == "partial"


def test_a_non_source_edit_the_backend_misses_is_not_called_clean(monkeypatch, tmp_path, repo):
    now = time.time()
    _stamp_index(tmp_path / "graph-cache", now - 60)
    _write(repo, "NOTES.txt", "a note\n", mtime=now - 90)
    p = _provider(monkeypatch, tmp_path, repo)
    _backend_reports_nothing(monkeypatch, p)

    env = _ask(p, repo)

    assert "working tree clean" not in env["result"], env["result"]
    assert "non-source" in env["result"], env["result"]
    kinds = _gap_kinds(env)
    assert "stale-index" not in kinds and "index-age-unknown" not in kinds, env.get("gaps")
