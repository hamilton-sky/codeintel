"""Unit tests for Reindexer: debounce, off-thread, config gate, never-raise."""
from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from codeintel.gateway.reindexer import Reindexer

# The background pass IS the subject here, so this module opts out of the suite-wide guard that
# stubs `maybe_reindex` (see `tests/conftest.py::_no_background_reindex`). Safe because every test
# below drives a Reindexer it constructed itself, against `/tmp/test` or a nonexistent path, with
# `Indexer` and `SemanticDb` patched — and `_drain` joins the executor rather than leaving a daemon
# thread running, which is the property the guard exists to enforce everywhere else.
pytestmark = pytest.mark.usefixtures("background_reindex")


@pytest.fixture(autouse=True)
def _clear_reindex_env(monkeypatch):
    monkeypatch.delenv("CODEINTEL_REINDEX", raising=False)


def _drain(r: Reindexer) -> None:
    """Wait for all submitted background work to finish."""
    r._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Test 1: background thread fires and calls Indexer.index
# ---------------------------------------------------------------------------

def test_maybe_reindex_fires_background_thread(tmp_path):
    root = str(tmp_path)
    with patch("codeintel.engines.semantic.indexer.Indexer") as MockIndexer, \
         patch("codeintel.engines.semantic.db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        r = Reindexer(debounce_seconds=0)
        r.maybe_reindex(root)
        _drain(r)

        MockIndexer.return_value.index.assert_called_once_with(root)


# ---------------------------------------------------------------------------
# Test 2: second call within debounce window is a no-op
# ---------------------------------------------------------------------------

def test_debounce_suppresses_second_call(tmp_path):
    root = str(tmp_path)
    with patch("codeintel.engines.semantic.indexer.Indexer") as MockIndexer, \
         patch("codeintel.engines.semantic.db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        r = Reindexer(debounce_seconds=1000)
        r.maybe_reindex(root)
        r.maybe_reindex(root)
        _drain(r)

        assert MockIndexer.return_value.index.call_count <= 1


# ---------------------------------------------------------------------------
# Test 3: CODEINTEL_REINDEX=off disables indexing entirely
# ---------------------------------------------------------------------------

def test_disabled_via_env():
    with patch.dict(os.environ, {"CODEINTEL_REINDEX": "off"}), \
         patch("codeintel.engines.semantic.indexer.Indexer") as MockIndexer, \
         patch("shutil.which", return_value=None):
        r = Reindexer()
        r.maybe_reindex("/tmp/test")
        _drain(r)

        MockIndexer.return_value.index.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: bad path does not raise
# ---------------------------------------------------------------------------

def test_never_raises_on_bad_path():
    with patch("codeintel.engines.semantic.db.SemanticDb") as MockDb, \
         patch("codeintel.engines.semantic.indexer.Indexer"), \
         patch("shutil.which", return_value=None):
        MockDb.return_value.init.side_effect = FileNotFoundError("no such path")

        r = Reindexer(debounce_seconds=0)
        result = r.maybe_reindex("/nonexistent/path/xyz")
        _drain(r)

        assert result is False


# ---------------------------------------------------------------------------
# Test 5: Indexer.index raising does not propagate
# ---------------------------------------------------------------------------

def test_never_raises_on_indexer_exception(tmp_path):
    root = str(tmp_path)
    with patch("codeintel.engines.semantic.indexer.Indexer") as MockIndexer, \
         patch("codeintel.engines.semantic.db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        MockIndexer.return_value.index.side_effect = RuntimeError("boom")

        r = Reindexer(debounce_seconds=0)
        r.maybe_reindex(root)
        _drain(r)
        # reaching here without exception confirms the never-raise contract


# ---------------------------------------------------------------------------
# Test 6: background reindex threads are DAEMON — a one-shot CLI must not hang on
# exit waiting for a repo-wide reindex to finish.
# ---------------------------------------------------------------------------

def test_reindex_runs_on_daemon_threads(tmp_path):
    root = str(tmp_path)
    import threading as _threading

    slow_started = _threading.Event()
    release = _threading.Event()

    def _slow_index(_root):
        slow_started.set()
        release.wait(5)  # simulate a long reindex still in flight

    with patch("codeintel.engines.semantic.indexer.Indexer") as MockIndexer, \
         patch("codeintel.engines.semantic.db.SemanticDb"), \
         patch("shutil.which", return_value=None):
        MockIndexer.return_value.index.side_effect = _slow_index
        r = Reindexer(debounce_seconds=0)
        r.maybe_reindex(root)
        assert slow_started.wait(2), "reindex thread did not start"
        with r._executor._lock:
            live = [t for t in r._executor._threads if t.is_alive()]
        assert live, "expected a running reindex thread"
        assert all(t.daemon for t in live), "reindex threads must be daemon (must not block CLI exit)"
        release.set()
        _drain(r)


# --------------------------------------------------------------------------- graph reindex

class _FakeGraph:
    """Records what the reindexer asks the backend to do."""

    available = True
    # The real provider always carries this: `_run` records WHY it returned None, and the
    # reindexer quotes it. A fake without it would test an object the reindexer is never handed.
    _last_failure = None

    def __init__(self, reply=None):
        self.calls: list[tuple] = []
        self._reply = reply

    def _run(self, tool, payload, timeout_ms):
        self.calls.append((tool, payload, timeout_ms))
        return self._reply


def test_graph_reindex_calls_the_tool_that_actually_reindexes(monkeypatch):
    """This called `detect_changes` with a `project_root` argument, and was wrong twice over: the
    backend takes `project` (a name from list_projects), so every call returned an argument error
    that `_run` folded into None — and `detect_changes` only REPORTS uncommitted drift, it never
    reindexes. The graph therefore never refreshed, silently, and queries kept answering from
    whatever the index held when it was first built.
    """
    from codeintel.gateway.reindexer import Reindexer

    fake = _FakeGraph({"status": "ok"})
    monkeypatch.setattr("codeintel.engines.graph.provider.GraphProvider", lambda: fake)
    Reindexer()._graph_reindex("/repo")

    assert [c[0] for c in fake.calls] == ["index_repository"]
    # `repo_path` is the backend's actual parameter name. A mock cannot tell you that — this
    # assertion is only as good as the live check in test_graph_stdin.py that pins it.
    assert fake.calls[0][1] == {"repo_path": "/repo"}
    assert fake.calls[0][2] >= 120_000          # indexing a real repo outlasts a query budget


def test_graph_reindex_does_nothing_without_a_backend(monkeypatch):
    from codeintel.gateway.reindexer import Reindexer

    fake = _FakeGraph()
    fake.available = False
    monkeypatch.setattr("codeintel.engines.graph.provider.GraphProvider", lambda: fake)
    Reindexer()._graph_reindex("/repo")

    assert fake.calls == []


@pytest.mark.parametrize("reply,expected", [
    ({"status": "error", "hint": "worker crashed on a file"}, "worker crashed on a file"),
    (None, "returned nothing"),
])
def test_graph_reindex_logs_a_backend_failure_instead_of_swallowing_it(monkeypatch, caplog, reply,
                                                                      expected):
    """Swallowing the backend's own error is what let a broken reindex look exactly like a working
    one for as long as nobody compared the graph to the source."""
    from codeintel.gateway.reindexer import Reindexer

    monkeypatch.setattr("codeintel.engines.graph.provider.GraphProvider", lambda: _FakeGraph(reply))
    with caplog.at_level(logging.WARNING):
        Reindexer()._graph_reindex("/repo")

    assert expected in caplog.text


def test_graph_reindex_never_raises(monkeypatch):
    """It runs on a background thread after an index; an exception here must not take that down."""
    from codeintel.gateway.reindexer import Reindexer

    monkeypatch.setattr("codeintel.engines.graph.provider.GraphProvider",
                        lambda: (_ for _ in ()).throw(RuntimeError("backend exploded")))
    Reindexer()._graph_reindex("/repo")          # must return normally


# --------------------------------------------------------------------------- freshness generation

def test_a_semantic_failure_does_not_block_the_graph_pass_or_freeze_freshness(monkeypatch):
    """These shared one try block, semantic first. A semantic failure — a blocked model download
    on an air-gapped host, a full disk, a corrupt vector DB — skipped the graph pass AND skipped
    the generation bump. That bump is the only cache invalidation for non-file targets
    (`callers`, `impact`, `hotspots`: the hash of a symbol name never changes), so the counter
    stayed pinned at 0 and every cached answer came back `ok: true, cached: true` forever, however
    far the code moved on. A persistent cause retries and fails identically, so it never recovered.
    """
    r = Reindexer()
    ran = {"graph": False}
    monkeypatch.setattr(r, "_semantic_reindex",
                        lambda root: (_ for _ in ()).throw(RuntimeError("model download blocked")))
    monkeypatch.setattr(r, "_graph_reindex", lambda root: ran.__setitem__("graph", True))

    before = r.generation("/repo")
    r._do_reindex("/repo")

    assert ran["graph"] is True, "the graph pass must not be collateral damage"
    assert r.generation("/repo") > before, "freshness must advance so caches still invalidate"


def test_a_graph_failure_also_advances_freshness(monkeypatch):
    r = Reindexer()
    monkeypatch.setattr(r, "_semantic_reindex", lambda root: None)
    monkeypatch.setattr(r, "_graph_reindex",
                        lambda root: (_ for _ in ()).throw(RuntimeError("backend down")))

    before = r.generation("/repo")
    r._do_reindex("/repo")
    assert r.generation("/repo") > before


def test_both_engines_failing_still_advances_freshness(monkeypatch, caplog):
    """Worst case: a cache that never invalidates is more dangerous than one that misses."""
    r = Reindexer()
    for name in ("_semantic_reindex", "_graph_reindex"):
        monkeypatch.setattr(r, name, lambda root: (_ for _ in ()).throw(RuntimeError("down")))

    before = r.generation("/repo")
    with caplog.at_level(logging.WARNING):
        r._do_reindex("/repo")

    assert r.generation("/repo") > before
    assert "semantic reindex failed" in caplog.text and "graph reindex failed" in caplog.text


def test_repeated_failures_keep_advancing_rather_than_pinning_at_zero(monkeypatch):
    """The permanence was the harm: a persistent failure retried every debounce window and left
    the counter at 0 for the life of the process."""
    r = Reindexer()
    for name in ("_semantic_reindex", "_graph_reindex"):
        monkeypatch.setattr(r, name, lambda root: (_ for _ in ()).throw(RuntimeError("down")))

    for _ in range(3):
        r._do_reindex("/repo")
    assert r.generation("/repo") == 3


def test_a_reindex_already_running_is_not_submitted_again(monkeypatch, tmp_path):
    """The debounce timestamp is set when a pass is SUBMITTED, not when it finishes, so on a repo
    whose reindex outlasts the window every later query stacked another concurrent pass —
    overlapping writers against one SQLite file and one graph subprocess, for no benefit."""
    root = str(tmp_path)
    r = Reindexer(debounce_seconds=0)
    submitted = []
    monkeypatch.setattr(r._executor, "submit", lambda fn, root: submitted.append(root))

    r.maybe_reindex(root)
    assert submitted == [root]

    r.maybe_reindex(root)          # still in flight (nothing cleared it)
    r.maybe_reindex(root)
    assert submitted == [root], "a second pass was stacked on a running one"

    r._in_flight.discard(root)     # the running pass completes
    r.maybe_reindex(root)
    assert submitted == [root, root]


# --------------------------------------------------------------------------- change detection
#
# Finding `reindexing-flag-self-triggered`: the debounce was on time alone, so every query on an
# unchanged tree submitted a pass once the window had passed, and the gateway then read
# `reindex_pending` and flagged its own pass as "a reindex is in progress". A git root now submits
# only when its tree fingerprint (HEAD plus the stat of every tracked and untracked file) moved.

def _git_repo(path) -> str:
    import subprocess

    path.mkdir()
    (path / "core.py").write_text("def helper():\n    return 1\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "--no-gpg-sign", "-m", "init")):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=path, env=env,
                       check=True, capture_output=True)
    return str(path)


def _counting_passes(r: Reindexer, monkeypatch) -> list[str]:
    passes: list[str] = []
    monkeypatch.setattr(r, "_graph_reindex", lambda root: passes.append(root))
    monkeypatch.setattr(r, "_semantic_reindex", lambda root: None)
    return passes


class _AnsweringGraph:
    available = True

    def build_result(self, op, target, files, budget, project_root):
        return {"ok": True, "op": op, "target": target, "engine": "graph", "cached": False,
                "result": f"Callers of {target} (1)"}


def test_an_unchanged_git_tree_submits_nothing_and_flags_nothing(tmp_path, monkeypatch):
    from codeintel.gateway.gateway import Gateway

    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=0)
    passes = _counting_passes(r, monkeypatch)

    assert r.maybe_reindex(root) is True          # nothing recorded yet for this root
    _drain(r)
    assert passes == [root]

    assert r.maybe_reindex(root) is False
    _drain(r)
    assert passes == [root], "an unchanged tree submitted a pass"

    answer = Gateway(graph=_AnsweringGraph(), reindexer=r).query(
        op="callers", target="helper", engine="graph", role="", project_root=root)
    _drain(r)
    assert answer["result"] is not None
    assert "reindexing" not in answer, answer
    assert passes == [root]


def test_an_edited_file_submits_a_pass_and_returns_true(tmp_path, monkeypatch):
    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=0)
    passes = _counting_passes(r, monkeypatch)
    r.maybe_reindex(root)
    _drain(r)

    edited = os.path.join(root, "core.py")
    with open(edited, "w") as fh:
        fh.write("def helper():\n    return 2\n")
    os.utime(edited, (os.stat(edited).st_atime, os.stat(edited).st_mtime + 5))

    assert r.maybe_reindex(root) is True
    _drain(r)
    assert passes == [root, root]


def test_a_new_untracked_file_submits_a_pass(tmp_path, monkeypatch):
    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=0)
    passes = _counting_passes(r, monkeypatch)
    r.maybe_reindex(root)
    _drain(r)

    with open(os.path.join(root, "b.py"), "w") as fh:
        fh.write("def c2():\n    return 0\n")

    assert r.maybe_reindex(root) is True
    _drain(r)
    assert passes == [root, root]


def test_a_staged_edit_submits_a_pass(tmp_path, monkeypatch):
    """`edit` then `git add` leaves nothing for `ls-files -m` to list; the worktree still moved."""
    import subprocess

    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=0)
    passes = _counting_passes(r, monkeypatch)
    r.maybe_reindex(root)
    _drain(r)

    edited = os.path.join(root, "core.py")
    with open(edited, "w") as fh:
        fh.write("def helper():\n    return 2\n")
    os.utime(edited, (os.stat(edited).st_atime, os.stat(edited).st_mtime + 5))
    subprocess.run(["git", "add", "core.py"], cwd=root, check=True, capture_output=True)

    assert r.maybe_reindex(root) is True
    _drain(r)
    assert passes == [root, root]


def test_the_fingerprint_never_runs_the_repositorys_own_clean_filter(tmp_path, monkeypatch):
    """A repository is untrusted input. `git ls-files -m` compares the content of a tracked file
    whose stat moved but whose size did not, and that comparison runs the `filter.<x>.clean`
    command the repository's own config and `.gitattributes` name, on every query."""
    import subprocess

    root = _git_repo(tmp_path / "repo")
    marker = tmp_path / "filter-ran"
    with open(os.path.join(root, ".gitattributes"), "w") as fh:
        fh.write("*.py filter=evil\n")
    subprocess.run(["git", "config", "filter.evil.clean", f"touch {marker}; cat"], cwd=root,
                   check=True, capture_output=True)
    edited = os.path.join(root, "core.py")
    with open(edited, "w") as fh:
        fh.write("def helper():\n    return 2\n")          # same size as the committed text
    os.utime(edited, (os.stat(edited).st_atime, os.stat(edited).st_mtime + 5))

    r = Reindexer(debounce_seconds=0)
    _counting_passes(r, monkeypatch)
    r.maybe_reindex(root)
    _drain(r)

    assert not marker.exists(), "the fingerprint ran the repository's clean filter"


def test_an_unchanged_tree_is_fingerprinted_at_most_once_per_debounce_window(tmp_path,
                                                                             monkeypatch):
    """Past the window, an unchanged tree must not cost git calls on every query."""
    import codeintel.gateway.reindexer as reindexer_mod

    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=1000)
    passes = _counting_passes(r, monkeypatch)
    assert r.maybe_reindex(root) is True
    _drain(r)
    r._last_fired[root] -= 2000                         # the window has passed

    checks: list[str] = []
    real = reindexer_mod._tree_fingerprint
    monkeypatch.setattr(reindexer_mod, "_tree_fingerprint",
                        lambda root: checks.append(root) or real(root))

    assert r.maybe_reindex(root) is False
    assert r.maybe_reindex(root) is False
    assert r.maybe_reindex(root) is False

    assert checks == [root], checks
    assert passes == [root]


def test_the_first_pass_of_a_root_is_not_held_back_by_host_uptime(tmp_path, monkeypatch):
    """A root that never fired is outside the window, even on a host up for less than it."""
    import codeintel.gateway.reindexer as reindexer_mod

    monkeypatch.setattr(reindexer_mod.time, "monotonic", lambda: 5.0)   # five seconds after boot
    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=1000)
    passes = _counting_passes(r, monkeypatch)

    assert r.maybe_reindex(root) is True
    _drain(r)
    assert passes == [root]


def test_do_reindex_runs_the_graph_pass_before_the_semantic_pass(monkeypatch):
    """A blocked model download must not delay graph freshness."""
    r = Reindexer()
    ran: list[str] = []
    monkeypatch.setattr(r, "_semantic_reindex", lambda root: ran.append("semantic"))
    monkeypatch.setattr(r, "_graph_reindex", lambda root: ran.append("graph"))

    r._do_reindex("/repo")

    assert ran == ["graph", "semantic"]


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_a_git_failure_in_the_fingerprint_submits_no_more_than_the_debounce(
        tmp_path, monkeypatch, failure):
    """A fingerprint git could not compute counts as unchanged and falls back to the time
    debounce: two calls inside the window submit once, never once per call."""
    import subprocess

    root = _git_repo(tmp_path / "repo")

    def _broken_git(argv, *a, **k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 5)
        return subprocess.CompletedProcess(argv, 128, b"", b"fatal: broken")

    monkeypatch.setattr("codeintel.gateway.reindexer.subprocess.run", _broken_git)
    r = Reindexer(debounce_seconds=5)
    submitted: list[str] = []
    monkeypatch.setattr(r._executor, "submit", lambda fn, root: submitted.append(root))

    r.maybe_reindex(root)
    r._in_flight.discard(root)          # the first pass finished: only the debounce may stop it
    r.maybe_reindex(root)

    assert submitted == [root]


def test_the_fingerprint_git_calls_are_bounded_and_shell_free(tmp_path, monkeypatch):
    import subprocess

    root = _git_repo(tmp_path / "repo")
    seen: list[tuple] = []
    real_run = subprocess.run

    def _spy(argv, *a, **k):
        seen.append((argv, k))
        return real_run(argv, *a, **k)

    monkeypatch.setattr("codeintel.gateway.reindexer.subprocess.run", _spy)
    r = Reindexer(debounce_seconds=0)
    monkeypatch.setattr(r._executor, "submit", lambda fn, root: None)
    r.maybe_reindex(root)

    assert seen, "no git call was made for a git root"
    for argv, kwargs in seen:
        assert isinstance(argv, list) and argv[0] == "git"
        assert kwargs.get("cwd") == root
        assert 0 < kwargs.get("timeout", 0) <= 5
        assert not kwargs.get("shell")



def _edit(root: str) -> None:
    edited = os.path.join(root, "core.py")
    with open(edited, "w") as fh:
        fh.write("def helper():\n    return 2\n")
    os.utime(edited, (os.stat(edited).st_atime, os.stat(edited).st_mtime + 5))


def test_a_lock_skipped_pass_is_retried_while_the_tree_stays_the_same(tmp_path, monkeypatch):
    """A pass that never ran (another process held the lock) indexed nothing, so its fingerprint
    must not count as indexed: the next query on the same tree submits again."""
    from codeintel.core.filelock import exclusive, lock_path

    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=0)
    passes = _counting_passes(r, monkeypatch)
    r.maybe_reindex(root)
    _drain(r)
    _edit(root)

    with exclusive(lock_path(os.path.realpath(root))) as held:
        assert held
        assert r.maybe_reindex(root) is True
        _drain(r)
    assert passes == [root], "the pass ran although the lock was held"

    assert r.maybe_reindex(root) is True, "a skipped pass was never retried"
    _drain(r)
    assert passes == [root, root]
    assert r.maybe_reindex(root) is False
    _drain(r)
    assert passes == [root, root]


def test_a_pass_that_raised_is_retried_while_the_tree_stays_the_same(tmp_path, monkeypatch):
    root = _git_repo(tmp_path / "repo")
    r = Reindexer(debounce_seconds=0)
    calls: list[str] = []

    def _failing_graph(project_root):
        calls.append(project_root)
        raise RuntimeError("backend down")

    monkeypatch.setattr(r, "_graph_reindex", _failing_graph)
    monkeypatch.setattr(r, "_semantic_reindex", lambda project_root: None)

    assert r.maybe_reindex(root) is True
    _drain(r)
    assert r.maybe_reindex(root) is True, "a failed pass was never retried"
    _drain(r)
    assert calls == [root, root]


def test_a_tree_listing_too_many_files_falls_back_to_the_debounce(tmp_path, monkeypatch):
    """The stat walk runs on the request thread, so a huge listing is not fingerprinted."""
    import codeintel.gateway.reindexer as reindexer_mod

    root = _git_repo(tmp_path / "repo")
    with open(os.path.join(root, "b.py"), "w") as fh:
        fh.write("x = 1\n")
    monkeypatch.setattr(reindexer_mod, "_FINGERPRINT_MAX_FILES", 1)
    stats: list[str] = []
    real_stat = os.stat
    monkeypatch.setattr(reindexer_mod.os, "stat",
                        lambda path, *a, **k: stats.append(path) or real_stat(path, *a, **k))

    assert reindexer_mod._tree_fingerprint(root) is None
    assert stats == [], "the files were stat'ed although the listing was over the cap"


def test_the_remembered_fingerprints_are_bounded(tmp_path, monkeypatch):
    import codeintel.gateway.reindexer as reindexer_mod

    monkeypatch.setattr(reindexer_mod, "_FINGERPRINT_ROOTS_MAX", 2)
    r = Reindexer(debounce_seconds=0)
    _counting_passes(r, monkeypatch)
    roots = [_git_repo(tmp_path / f"repo{i}") for i in range(4)]
    for root in roots:
        assert r.maybe_reindex(root) is True
        _drain(r)

    assert len(r._last_fingerprint) <= 2
    assert roots[-1] in r._last_fingerprint      # the newest root is kept

# --------------------------------------------------------------------------- #
# Lifecycle truth: what `status` and `doctor` report about an index
#
# Both of these reported healthy while being wrong, which is the shape of nearly every bug this
# project has shipped — the health layer answering from something other than the fact it claims.
# --------------------------------------------------------------------------- #

def test_index_age_is_per_project_not_the_shared_database_mtime(tmp_path):
    """Every project writes into one per-model database file, so its mtime is the age of the most
    recent index of ANY repository. Indexing an unrelated repo made a stale one look fresh."""
    import time as _time

    from codeintel.engines.semantic.db import SemanticDb

    db = SemanticDb(str(tmp_path / "db.sqlite"))
    db.init()
    try:
        old, new = "/repos/stale-app", "/repos/busy-app"
        db.mark_indexed(old, when=_time.time() - 90 * 24 * 3600)   # 90 days ago
        db.mark_indexed(new)                                        # now

        stale_at, fresh_at = db.indexed_at(old), db.indexed_at(new)
        assert stale_at is not None and fresh_at is not None
        # The stale project must still read as stale after the busy one was indexed.
        assert fresh_at - stale_at > 89 * 24 * 3600
        # A project nobody indexed reports unknown, not "now".
        assert db.indexed_at("/repos/never-touched") is None
    finally:
        db.close()


def test_a_project_whose_root_is_gone_is_reconciled_away(tmp_path):
    """`_index` returned 0 before reaching cleanup when the root was missing — and 0 also means
    "nothing new", so a deleted or moved repository kept every row it ever had and went on being
    reported as indexed and healthy."""
    from codeintel.engines.semantic.db import SemanticDb
    from codeintel.engines.semantic.indexer import Indexer

    db = SemanticDb(str(tmp_path / "db.sqlite"))
    db.init()
    try:
        gone = tmp_path / "deleted-repo"
        gone_real = os.path.realpath(str(gone))
        conn = db.conn()
        conn.execute(
            "INSERT INTO chunk_hashes(chunk_id, project_root, file_path, chunk_start, content_hash)"
            " VALUES (?, ?, ?, ?, ?)", ("c1", gone_real, "app.py", 0, "h"),
        )
        conn.commit()
        db.mark_indexed(gone_real)
        assert db.conn().execute(
            "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?", (gone_real,),
        ).fetchone()[0] == 1

        Indexer(db)._index(str(gone))        # the root does not exist

        assert db.conn().execute(
            "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?", (gone_real,),
        ).fetchone()[0] == 0, "a deleted repo's rows must not survive"
        assert db.indexed_at(gone_real) is None
    finally:
        db.close()


def test_reconciling_one_deleted_project_leaves_others_alone(tmp_path):
    """The counterpart guard: cleanup is scoped by project_root, so removing one repo must never
    touch another's index."""
    from codeintel.engines.semantic.db import SemanticDb
    from codeintel.engines.semantic.indexer import Indexer

    db = SemanticDb(str(tmp_path / "db.sqlite"))
    db.init()
    try:
        live = tmp_path / "live-repo"; live.mkdir()
        live_real, gone_real = os.path.realpath(str(live)), os.path.realpath(str(tmp_path / "gone"))
        conn = db.conn()
        for cid, root in (("a", live_real), ("b", gone_real)):
            conn.execute(
                "INSERT INTO chunk_hashes(chunk_id, project_root, file_path, chunk_start,"
                " content_hash) VALUES (?, ?, ?, ?, ?)", (cid, root, "app.py", 0, "h"),
            )
        conn.commit()

        Indexer(db)._index(str(tmp_path / "gone"))

        assert db.conn().execute(
            "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?", (live_real,),
        ).fetchone()[0] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The semantic pass writes and clears the persisted model-failure record
# ---------------------------------------------------------------------------

class _BlockedEmbedding:
    """fastembed's model class on a host where the weights cannot be fetched."""

    def __init__(self, *args, **kwargs):
        raise ValueError("Could not load model BAAI/bge-small-en-v1.5 from any source.")

    @classmethod
    def list_supported_models(cls):
        return [{"model": "BAAI/bge-small-en-v1.5"}]


class _WorkingEmbedding:
    """fastembed's model class once the weights are reachable."""

    def __init__(self, *args, **kwargs):
        pass

    def embed(self, texts):
        import numpy as np
        return [np.full(384, 0.1, dtype=np.float32) for _ in list(texts)]


@pytest.fixture
def blocked_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core.py").write_text("def helper():\n    return 1\n")
    cache = tmp_path / "fastembed-empty"
    cache.mkdir()
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
    monkeypatch.setattr("fastembed.TextEmbedding", _BlockedEmbedding)
    return str(repo)


def _failure_record():
    from codeintel.core.paths import codeintel_home
    return codeintel_home() / "semantic_failure.json"


def test_a_failed_semantic_pass_writes_the_model_failure_record(blocked_repo):
    import json

    Reindexer()._semantic_reindex(blocked_repo)

    record = json.loads(_failure_record().read_text())
    assert "could not load embedding model" in record["cause"]
    assert record["model"] == "BAAI/bge-small-en-v1.5"


def test_a_successful_semantic_pass_clears_the_model_failure_record(blocked_repo, monkeypatch):
    Reindexer()._semantic_reindex(blocked_repo)
    assert _failure_record().exists()
    monkeypatch.setattr("fastembed.TextEmbedding", _WorkingEmbedding)

    Reindexer()._semantic_reindex(blocked_repo)

    assert not _failure_record().exists()


def test_the_fingerprint_does_not_follow_a_tracked_symlink(tmp_path):
    """A tracked link into a hung network mount would block the request thread on `stat`; the
    fingerprint dates the link itself, so a change behind it does not move it."""
    import subprocess

    import codeintel.gateway.reindexer as reindexer_mod

    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    root = _git_repo(tmp_path / "repo")
    os.symlink(str(outside), os.path.join(root, "link.py"))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (("add", "-A"), ("commit", "-q", "--no-gpg-sign", "-m", "link")):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, env=env,
                       check=True, capture_output=True)
    before = reindexer_mod._tree_fingerprint(root)

    outside.write_text("x = 1000000\n")
    os.utime(outside, (os.stat(outside).st_atime, os.stat(outside).st_mtime + 50))

    assert before is not None
    assert reindexer_mod._tree_fingerprint(root) == before


def test_a_root_without_a_fingerprint_is_not_flagged_reindexing_for_a_debounce_pass(
        tmp_path, monkeypatch):
    """A root git cannot fingerprint still gets a pass each window, but nothing says its tree
    changed, so the query that submitted it does not report `reindexing`."""
    from codeintel.gateway.gateway import Gateway

    root = str(tmp_path / "plain")
    os.mkdir(root)
    r = Reindexer(debounce_seconds=0)
    passes = _counting_passes(r, monkeypatch)

    assert r.maybe_reindex(root) is False
    _drain(r)
    assert passes == [root], "the debounce pass was not submitted"

    answer = Gateway(graph=_AnsweringGraph(), reindexer=r).query(
        op="callers", target="helper", engine="graph", role="", project_root=root)
    _drain(r)
    assert answer["result"] is not None
    assert "reindexing" not in answer, answer
    assert passes == [root, root]
