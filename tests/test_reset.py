"""Reset command tests — real-boundary (actual temp SemanticDb files, no seam mocks), per
this repo's philosophy. Reset is a recovery command: it must never raise, even against a
corrupt db, since a corrupt db is exactly the scenario it exists to fix.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

from codeintel import reset as _reset
from codeintel.reset import run_reset
from codeintel.semantic_db import default_db_path


def _seed(db_path, project_root_real, n=1):
    from codeintel.semantic_db import SemanticDb
    db = SemanticDb(str(db_path))
    db.init()
    c = db.conn()
    for i in range(n):
        c.execute(
            "INSERT INTO chunk_hashes(chunk_id,project_root,file_path,chunk_start,content_hash)"
            " VALUES (?,?,?,?,?)",
            (f"{project_root_real}:{i}", project_root_real, "f.py", i, "h"),
        )
    c.commit()
    db.close()


def _row_count(db_path, project_root_real):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM chunk_hashes WHERE project_root=?", (project_root_real,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


# --------------------------------------------------------------------------- #
# scoped reset
# --------------------------------------------------------------------------- #

def test_scoped_reset_deletes_only_target(tmp_path):
    db_path = tmp_path / "semantic.db"
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    real_a = os.path.realpath(str(repo_a))
    real_b = os.path.realpath(str(repo_b))
    _seed(db_path, real_a, n=2)
    _seed(db_path, real_b, n=3)

    r = run_reset(str(repo_a), apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert r["mode"] == "scoped"
    assert r["applied"] is True
    assert r["count"] == 2
    assert _row_count(db_path, real_a) == 0
    assert _row_count(db_path, real_b) == 3


def test_dry_run_counts_without_deleting(tmp_path):
    db_path = tmp_path / "semantic.db"
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    real_a = os.path.realpath(str(repo_a))
    _seed(db_path, real_a, n=4)

    r = run_reset(str(repo_a), apply=False, db_path=str(db_path))

    assert r["ok"] is True
    assert r["count"] > 0
    assert r["applied"] is False
    assert _row_count(db_path, real_a) == 4


def test_scoped_reset_on_corrupt_db_never_raises(tmp_path):
    db_path = tmp_path / "semantic.db"
    db_path.write_bytes(b"this is not a sqlite database")
    repo = tmp_path / "repo"
    repo.mkdir()

    r = run_reset(str(repo), apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert "error" in r["detail"].lower()


# --------------------------------------------------------------------------- #
# --all reset
# --------------------------------------------------------------------------- #

def test_reset_all_removes_db_files(tmp_path):
    db_path = tmp_path / "semantic.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(db_path, os.path.realpath(str(repo)))
    wal_path = tmp_path / "semantic.db-wal"
    shm_path = tmp_path / "semantic.db-shm"
    wal_path.write_bytes(b"")
    shm_path.write_bytes(b"")

    r = run_reset(str(repo), all_projects=True, apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert r["mode"] == "all"
    assert r["applied"] is True
    assert r["count"] == 3
    assert not db_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_reset_all_on_corrupt_db(tmp_path):
    db_path = tmp_path / "semantic.db"
    db_path.write_bytes(b"\x00\x01garbage-not-a-db\xff")

    r = run_reset("/any/project", all_projects=True, apply=True, db_path=str(db_path))

    assert r["ok"] is True
    assert not db_path.exists()


# --------------------------------------------------------------------------- #
# missing db — idempotent
# --------------------------------------------------------------------------- #

def test_reset_missing_db_idempotent(tmp_path):
    db_path = tmp_path / "does-not-exist.db"

    scoped = run_reset("/any/project", apply=True, db_path=str(db_path))
    assert scoped["ok"] is True
    assert scoped["count"] == 0

    all_reset = run_reset("/any/project", all_projects=True, apply=True, db_path=str(db_path))
    assert all_reset["ok"] is True
    assert all_reset["count"] == 0


def test_reset_actually_recovers_from_a_corrupt_cache(tmp_path, monkeypatch):
    """`reset` is documented as "recover from a corrupt or stale DB", and this is the corrupt
    case — sqlite refuses to open the file, so there are no rows to DELETE. It reported
    "removed 0 indexed chunk(s)" and exited 0, leaving the user in a loop: doctor diagnoses the
    corruption and prescribes reset, reset no-ops and claims success, doctor repeats forever.
    """
    from codeintel import reset as _reset

    db = tmp_path / "semantic.db"
    db.write_bytes(os.urandom(4096))                    # not a database
    monkeypatch.setattr(_reset, "_cache_files", lambda: [str(db)])

    report = _reset.run_reset(str(tmp_path), apply=True)

    assert not db.exists(), "the unreadable cache must be removed so it can be rebuilt"
    assert "unreadable" in report["detail"]
    assert "codeintel index" in report["detail"]        # names the way forward


def test_a_reset_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    """The aggregate summed per-file counts and synthesized its own success line, discarding the
    per-file error — which is how a failed reset became indistinguishable from a clean one."""
    from codeintel import reset as _reset

    monkeypatch.setattr(_reset, "_reset_scoped",
                        lambda root, path, apply: {"count": 0, "error": "DatabaseError: nope",
                                                   "detail": "reset-error: db unreadable"})
    monkeypatch.setattr(_reset, "_cache_files", lambda: ["/tmp/whatever.db"])

    report = _reset.run_reset(str(tmp_path), apply=True)
    assert report["failed"] is True
    assert "reset-error" in report["detail"]


def test_a_healthy_reset_still_reports_plainly(tmp_path, monkeypatch):
    from codeintel import reset as _reset
    monkeypatch.setattr(_reset, "_reset_scoped", lambda root, path, apply: {"count": 7})
    monkeypatch.setattr(_reset, "_cache_files", lambda: ["/tmp/whatever.db"])

    report = _reset.run_reset(str(tmp_path), apply=True)
    assert report["failed"] is False
    assert report["detail"] == "removed 7 indexed chunk(s) for this project"


# --------------------------------------------------------------------------- #
# the destructive paths, watched deleting
#
# `reset --all` removes the semantic cache AND every graph project index, and it is the command a
# user reaches for when things are already broken. On an irreversible path, coverage is less "how
# much of the intended behaviour runs" than "how much of this deletion have we ever watched
# happen" — and for `_reset_graph_cache` and the whole `--all` branch the answer was none of it.
#
# Everything below deletes real files: paths resolve through the two overrides the product already
# honours (`CODEINTEL_HOME` for the semantic cache, `CODEBASE_MEMORY_HOME` for the graph backend's),
# so these exercise production resolution rather than route around it with a seam.
# --------------------------------------------------------------------------- #


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Confine reset to *tmp_path*, and audit every deletion it attempts.

    A test for a deletion command that escapes its sandbox is worse than the gap it closes: one
    mis-resolved path and it is the developer's own semantic cache and the graph backend's real
    indexes that go, which no later assertion can undo. Two guards, because the up-front one is
    only as complete as its list:

    1. Before anything runs, ask the module where it is about to look and refuse if the answer is
       outside `tmp_path`.
    2. Wrap `os.remove`/`os.unlink` for the duration and record — never perform — any deletion
       aimed outside. Derived from what reset actually calls rather than from a list of resolvers
       that a new code path could silently outgrow. The wrapper records instead of only raising
       because reset's never-raise contract swallows exceptions from its deletion attempts: an
       `AssertionError` in there would vanish into an `except Exception: pass`.
    """
    home = tmp_path / "home"
    graph = tmp_path / "graph-cache"
    home.mkdir()
    graph.mkdir()
    monkeypatch.setenv("CODEINTEL_HOME", str(home))
    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(graph))
    monkeypatch.delenv("CODEBASE_MEMORY_CACHE_DIR", raising=False)

    root = os.path.realpath(str(tmp_path)) + os.sep
    for resolved in (default_db_path(), _reset._graph_cache_dir()):
        assert os.path.realpath(resolved).startswith(root), (
            f"refusing to run a deletion test: reset resolves {resolved}, outside {root}")

    escaped: list[str] = []

    def _audit(real):
        def guarded(path, *args, **kwargs):
            if not os.path.realpath(path).startswith(root):
                escaped.append(str(path))
                raise AssertionError(f"deletion outside the sandbox: {path}")
            return real(path, *args, **kwargs)
        return guarded

    monkeypatch.setattr(os, "remove", _audit(os.remove))
    monkeypatch.setattr(os, "unlink", _audit(os.unlink))

    yield SimpleNamespace(home=pathlib.Path(home), graph=pathlib.Path(graph), escaped=escaped)

    assert not escaped, f"reset attempted to delete outside the sandbox: {escaped}"


def _files_under(root) -> dict[str, str]:
    """Every file below *root*, with a content digest. Walked, never enumerated: the point is to
    catch a deletion or a rewrite nobody thought to list, so the population has to come from the
    filesystem itself."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                found[path] = hashlib.sha256(handle.read()).hexdigest()
    return found


def _plant_a_full_cache(sandbox, repo_real, *, journals: bool) -> dict[str, bool]:
    """One file of every shape `reset --all` has to make a decision about, each mapped to whether
    it is expected to be removed. This map is the specification; every post-condition below is read
    back off the filesystem and compared against it.

    ``journals=False`` omits sqlite's own ``-wal``/``-shm`` siblings. They are not durable cache
    state and they are not reset's to preserve: merely opening a WAL database checkpoints and
    clears them, so a dry-run — which opens the db to COUNT — legitimately removes a stale one.
    Measured, not assumed. `_reset_all`'s sweep of those siblings is asserted where it applies.
    """
    home, graph = sandbox.home, sandbox.graph
    primary = default_db_path()                                    # the default model's cache
    orphan = default_db_path("some/other-embedding-model")         # what a model switch leaves
    _seed(primary, repo_real, n=2)
    _seed(orphan, repo_real, n=1)
    expected = {primary: True, orphan: True}
    if journals:
        for sibling in (primary + "-wal", primary + "-shm"):
            pathlib.Path(sibling).write_bytes(b"")
            expected[sibling] = True

    graph_plan = {
        "proj_a.db": True,          # a per-project graph index — the thing `--all` is for
        "proj_b.db": True,
        "torn.db.corrupt": True,    # what a torn write leaves behind
        "_config.db": False,        # registration, not an index: a different, worse deletion
        "_config.db-wal": False,
        "notes.txt": False,         # not a db at all
    }
    for name, doomed in graph_plan.items():
        path = graph / name
        path.write_bytes(b"graph-cache-payload")
        expected[str(path)] = doomed
    nested = graph / "subdir.db"    # a directory that merely looks like an index
    nested.mkdir()
    (nested / "inner.bin").write_bytes(b"x")
    expected[str(nested / "inner.bin")] = False
    assert str(home) in str(primary)                                # the plant landed in the sandbox
    return expected


def test_a_dry_run_removes_nothing(tmp_path, sandbox):
    """The single property that matters most on this command: ``apply=False`` reports what it would
    remove and removes nothing. Asserted against the whole tree rather than the files the report
    happens to mention, and against digests rather than existence, so a truncate-in-place counts as
    a failure too.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    expected = _plant_a_full_cache(sandbox, os.path.realpath(str(repo)), journals=False)

    before = _files_under(tmp_path)
    assert before, "nothing was planted — a dry-run over an empty tree proves nothing"
    assert set(before) == set(expected)

    scoped = run_reset(str(repo), apply=False)
    everything = run_reset(str(repo), all_projects=True, apply=False)

    # Non-vacuity: "it deleted nothing" is only evidence if it also FOUND something. An engine
    # that returns zero passes the deletion assertion trivially, and that state has shipped here.
    assert scoped["count"] == 3, scoped["detail"]           # 2 chunks + 1, across both model files
    assert everything["count"] == sum(1 for doomed in expected.values() if doomed)
    assert scoped["applied"] is False and everything["applied"] is False
    assert "would remove" in everything["detail"]

    assert _files_under(tmp_path) == before


def test_reset_all_clears_both_caches_and_spares_the_backends_config(tmp_path, sandbox):
    """`--all` is the nuke-everything path: every per-model semantic file plus its WAL siblings,
    and every graph project index — but not `_config.db`, which is the backend's registration
    rather than an index, and not a directory that merely ends in `.db`."""
    graph = sandbox.graph
    repo = tmp_path / "repo"
    repo.mkdir()
    expected = _plant_a_full_cache(sandbox, os.path.realpath(str(repo)), journals=True)

    report = run_reset(str(repo), all_projects=True, apply=True)

    survivors = set(_files_under(tmp_path))
    assert survivors == {path for path, doomed in expected.items() if not doomed}
    assert (graph / "subdir.db").is_dir(), "a directory named *.db is not an index file"
    assert report["mode"] == "all"
    assert report["applied"] is True
    assert report["count"] == sum(1 for doomed in expected.values() if doomed)
    # The detail string is the only thing a user sees, and `--all` silently leaving every graph
    # project in place is the defect it was rewritten to fix — so it has to name both caches.
    assert "removed 4 semantic index file(s) across all models" in report["detail"]
    assert "removed 3 graph project file(s)" in report["detail"]
    assert "including 1 previously-corrupt file(s)" in report["detail"]


def test_reset_all_says_so_when_the_graph_cache_cannot_be_read(tmp_path, monkeypatch, sandbox):
    """An unreadable graph directory must not be reported as a cleared one. `--all` claiming a
    clean index while every graph project survives is precisely the failure the graph sweep was
    added for, one level down."""
    home = sandbox.home
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(default_db_path(), os.path.realpath(str(repo)), n=1)
    wall = tmp_path / "not-a-directory"
    wall.write_bytes(b"")
    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(wall))   # listdir → NotADirectoryError

    report = run_reset(str(repo), all_projects=True, apply=True)

    assert report["count"] == 1, "the semantic side still has to be cleared"
    assert not (home / "semantic.db").exists()
    assert "graph projects were NOT cleared" in report["detail"]
    assert "NotADirectoryError" in report["detail"]
    assert wall.exists()


def test_graph_cache_reset_counts_before_it_deletes(tmp_path, sandbox):
    """`_reset_graph_cache` — the whole function was untested. Dry-run and apply must agree on
    the count, which is the only thing that makes the `--all` preview honest."""
    graph = sandbox.graph
    for name in ("proj_a.db", "proj_b.db", "torn.db.corrupt", "_config.db", "notes.txt"):
        (graph / name).write_bytes(b"x")

    preview = _reset._reset_graph_cache(apply=False)
    assert preview == {"count": 3, "corrupt": 1}
    assert len(_files_under(graph)) == 5, "a dry-run must leave the graph cache alone"

    applied = _reset._reset_graph_cache(apply=True)
    assert applied == preview
    assert set(_files_under(graph)) == {str(graph / "_config.db"), str(graph / "notes.txt")}


def test_graph_cache_reset_survives_a_cache_that_is_not_there(tmp_path, monkeypatch, sandbox):
    """A machine that never installed the graph backend has no such directory. Reset is the
    recovery command; "the thing I clean up is missing" is a no-op, not a failure."""
    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(tmp_path / "never-created"))
    assert _reset._reset_graph_cache(apply=True) == {"count": 0, "corrupt": 0}


def test_graph_cache_dir_honours_the_backends_own_overrides(tmp_path, monkeypatch):
    """codeintel does not own this directory, so it has to look where the backend was told to
    look — otherwise `reset --all` reports a clean index while a relocated cache survives intact.
    `_HOME` wins over `_CACHE_DIR`; with neither, the backend's documented default."""
    monkeypatch.delenv("CODEBASE_MEMORY_HOME", raising=False)
    monkeypatch.delenv("CODEBASE_MEMORY_CACHE_DIR", raising=False)
    assert _reset._graph_cache_dir() == os.path.join(
        os.path.expanduser("~"), ".cache", "codebase-memory-mcp")

    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(tmp_path / "relocated"))
    assert _reset._graph_cache_dir() == str(tmp_path / "relocated")

    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(tmp_path / "home-wins"))
    assert _reset._graph_cache_dir() == str(tmp_path / "home-wins")

    monkeypatch.setenv("CODEBASE_MEMORY_HOME", "~/tilde-cache")
    assert _reset._graph_cache_dir() == os.path.join(os.path.expanduser("~"), "tilde-cache")


def test_a_locked_cache_is_reported_not_deleted(tmp_path, sandbox):
    """The difference between "unreadable" and "busy" is the difference between a cache worth
    discarding and one another process is mid-write on. Discarding the second would destroy a
    healthy index to recover from a lock, so a lock has to come back as an error the caller can
    read — and the rows have to survive.

    Takes ~2s: `_reset_scoped` sets `busy_timeout=2000`, and this waits out a real lock rather
    than asserting against a hand-made exception string.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    real = os.path.realpath(str(repo))
    db = default_db_path()
    _seed(db, real, n=2)

    holder = sqlite3.connect(db)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute(
            "INSERT INTO chunk_hashes(chunk_id,project_root,file_path,chunk_start,content_hash)"
            " VALUES (?,?,?,?,?)", ("held:0", "/somewhere/else", "f.py", 0, "h"))
        report = run_reset(str(repo), apply=True)
    finally:
        holder.rollback()
        holder.close()

    assert report["ok"] is True
    assert report["failed"] is True
    assert "db unreadable/locked" in report["detail"]
    assert "database is locked" in report["detail"]
    assert os.path.exists(db), "a busy database is not a corrupt one — it must not be discarded"
    assert _row_count(db, real) == 2, "and its rows must still be there"


def test_a_dry_run_against_a_corrupt_cache_still_deletes_nothing(tmp_path, sandbox):
    """The corrupt path is the one place reset removes a whole FILE rather than rows, and it is
    reached from an exception handler — the easiest place for `apply` to be forgotten."""
    home = sandbox.home
    db = pathlib.Path(default_db_path())
    db.write_bytes(os.urandom(4096))                     # not a database
    before = _files_under(home)

    report = run_reset(str(tmp_path), apply=False)

    assert report["applied"] is False
    assert report["failed"] is True
    assert "reset-error: db unreadable/locked" in report["detail"]
    assert _files_under(home) == before, "a dry-run must not discard even an unreadable cache"


def test_reset_works_when_sqlite_vec_cannot_load(tmp_path, sandbox, monkeypatch):
    """The vec0 extension is only needed to DELETE from the embeddings table; the count and the
    `chunk_hashes` delete work without it. On a python without extension support that load fails,
    and reset — the recovery command — still has to do its job rather than degrade to zero."""
    repo = tmp_path / "repo"
    repo.mkdir()
    real = os.path.realpath(str(repo))
    db = default_db_path()
    _seed(db, real, n=3)

    def _no_extensions(_conn):
        raise RuntimeError("sqlite3 built without loadable extension support")

    monkeypatch.setattr(_reset.sqlite_vec, "load", _no_extensions)

    report = run_reset(str(repo), apply=True)

    assert report["count"] == 3
    assert report["failed"] is False
    assert _row_count(db, real) == 0


def test_cache_file_discovery_degrades_to_empty_instead_of_raising(tmp_path, sandbox, monkeypatch):
    """`_cache_files` runs before anything else in the un-seamed path, so if it can raise, reset
    cannot keep its never-raise promise. Non-vacuity first: prove it finds the planted files, THEN
    break the glob."""
    _seed(default_db_path(), os.path.realpath(str(tmp_path)), n=1)
    assert _reset._cache_files() == [default_db_path()]

    def _boom(_pattern):
        raise OSError("cache directory vanished mid-scan")

    monkeypatch.setattr(_reset.glob, "glob", _boom)
    assert _reset._cache_files() == []


def test_a_close_failure_does_not_undo_a_successful_reset(tmp_path, sandbox, monkeypatch):
    """The rows are already deleted and committed by the time the connection closes. A raise from
    `close()` there would throw away a completed reset and report a failure that did not happen."""
    repo = tmp_path / "repo"
    repo.mkdir()
    real = os.path.realpath(str(repo))
    db = default_db_path()
    _seed(db, real, n=2)

    real_connect = sqlite3.connect

    class _RefusesToClose:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def close(self):
            self._wrapped.close()
            raise sqlite3.ProgrammingError("connection already closed by the pool")

    monkeypatch.setattr(sqlite3, "connect",
                        lambda *args, **kwargs: _RefusesToClose(real_connect(*args, **kwargs)))

    report = run_reset(str(repo), apply=True)

    monkeypatch.undo()
    assert report["ok"] is True
    assert report["count"] == 2
    assert report["failed"] is False
    assert _row_count(db, real) == 0, "the delete committed before close() failed"


def test_reset_all_never_raises_on_a_file_it_cannot_remove(tmp_path, sandbox):
    """A cache path that is not a removable file — a directory left where a db belongs, a file the
    user does not own — must not take down the command that exists to recover from breakage."""
    stuck = pathlib.Path(default_db_path())
    stuck.mkdir()                                   # matches semantic*.db, refuses os.remove
    assert _reset._cache_files() == [str(stuck)]

    report = run_reset(str(tmp_path), all_projects=True, apply=True)

    assert report["ok"] is True
    assert report["count"] == 0
    assert stuck.is_dir(), "nothing was removed, and nothing pretended otherwise"


def test_discarding_an_unreadable_cache_reports_failure_instead_of_claiming_success(tmp_path,
                                                                                   caplog):
    """`_discard_cache_file` is the last resort for a corrupt cache, and its caller phrases the
    user's next step from the boolean it returns ("removed it" vs "delete it by hand"). Returning
    True on a failed unlink would send the user to `codeintel index` against a file that is still
    there, and the operator would have no record of why."""
    gone = tmp_path / "absent.db"
    assert _reset._discard_cache_file(str(gone)) is False

    stuck = tmp_path / "stuck.db"
    stuck.mkdir()                                   # unlink on a directory is an OSError
    with caplog.at_level("WARNING", logger="codeintel"):
        assert _reset._discard_cache_file(str(stuck)) is False
    assert stuck.is_dir()
    assert any("could not remove" in record.message for record in caplog.records), caplog.text


def test_unreadable_is_told_apart_from_merely_busy(tmp_path):
    """The classification that decides whether a cache file gets deleted, checked against real
    sqlite exceptions from real files rather than against strings typed to match the predicate."""
    seen = {}
    for name, payload in (("garbage.db", os.urandom(4096)),
                          ("prose.db", b"this is not a sqlite database"),
                          ("truncated.db", b"SQLite format 3\x00truncated-here")):
        path = tmp_path / name
        path.write_bytes(payload)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("SELECT COUNT(*) FROM chunk_hashes")
        except Exception as exc:
            seen[name] = (type(exc).__name__, _reset._looks_unreadable(exc))
        finally:
            conn.close()

    assert len(seen) == 3, f"a corrupt file failed to raise at all: {seen}"
    assert all(unreadable for _kind, unreadable in seen.values()), seen

    healthy = tmp_path / "healthy.db"
    _seed(healthy, os.path.realpath(str(tmp_path)), n=1)
    holder = sqlite3.connect(str(healthy))
    try:
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO chunk_hashes(chunk_id,project_root,file_path,chunk_start,"
                       "content_hash) VALUES ('x','y','f.py',0,'h')")
        busy = sqlite3.connect(str(healthy))
        busy.execute("PRAGMA busy_timeout=50")
        try:
            busy.execute("DELETE FROM chunk_hashes")
            raise AssertionError("expected the exclusive lock to block this write")
        except sqlite3.OperationalError as exc:
            assert _reset._looks_unreadable(exc) is False, f"a lock is not corruption: {exc}"
        finally:
            busy.close()
    finally:
        holder.rollback()
        holder.close()


def test_run_reset_never_raises_even_if_discovery_fails(tmp_path, monkeypatch):
    """The outer handler. `run_reset` is called from a `@never_raise` CLI command, but this module's
    own docstring promises it, and doctor's remediation text points here."""
    def _boom():
        raise RuntimeError("no home directory for this uid")

    monkeypatch.setattr(_reset, "_cache_files", _boom)

    report = run_reset(str(tmp_path), apply=True)
    assert report["ok"] is True
    assert report["applied"] is True
    assert report["detail"].startswith("reset-error: RuntimeError:")


def test_graph_cache_reset_survives_files_it_cannot_remove(tmp_path, sandbox):
    """A readable cache directory it has no write permission on — what a `sudo` run leaves behind.
    `os.listdir` and `isfile` both succeed, so every file looks removable right up to the syscall.
    Reset must report what it actually removed (nothing) rather than raising or claiming the
    count it hoped for."""
    graph = sandbox.graph
    for name in ("proj_a.db", "proj_b.db"):
        (graph / name).write_bytes(b"x")

    os.chmod(graph, 0o500)                       # readable, listable, not writable
    try:
        canary = graph / "proj_a.db"
        try:
            os.remove(canary)
        except PermissionError:
            pass
        else:
            pytest.skip("this user can delete inside a read-only directory (root?)")

        result = _reset._reset_graph_cache(apply=True)
    finally:
        os.chmod(graph, 0o700)

    assert result == {"count": 0, "corrupt": 0}
    assert set(_files_under(graph)) == {str(graph / "proj_a.db"), str(graph / "proj_b.db")}


def test_the_sandbox_guard_would_actually_catch_an_escape(tmp_path, sandbox):
    """"Nothing escaped" is exactly the shape that passes trivially when the check never runs —
    and an unarmed guard on a deletion test is worth less than no guard, because it reads as one.
    Aim a deletion outside the sandbox and prove it is REFUSED rather than merely noticed.

    The sentinel is written to `tmp_path.parent` — pytest's own numbered temp root, which it
    garbage-collects — because it has to live outside the boundary being tested, and this test
    cannot delete it afterwards: `os.remove` is the very function under audit here.
    """
    outside = tmp_path.parent / "sentinel-outside-the-sandbox"
    outside.write_bytes(b"a file no reset test may touch")
    try:
        with pytest.raises(AssertionError, match="outside the sandbox"):
            os.remove(str(outside))
        assert outside.exists(), "the audit must refuse the deletion, not just record it"
        assert sandbox.escaped == [str(outside)]
    finally:
        sandbox.escaped.clear()      # the escape was this test's own; teardown must not fail on it
