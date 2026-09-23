"""One reindex per repository, across processes — and why the lock is a `flock` and not a file.

MEASURED 2026-09-18, before this existed: two `codeintel index` passes started together on a
600-file repository each embedded all 600 chunks and each reported "Indexed 600 chunks", in ~15s
apiece. There is no natural serialisation — both read the same "what is new" answer from the
database before either had written anything, so the entire embedding cost is paid twice while the
two passes contend for the same cores. On a large repository that is ten minutes of CPU doubled.

After: one pass does the work and the other returns immediately (4.1s and 0.0s through the
background path). Losing the race is not an error and not reported — whoever holds the lock is
already doing precisely this work.

THE DESIGN CHOICE THIS FILE EXISTS TO PIN. The obvious way to record "a reindex is running" is to
write a file and delete it at the end, and that is strictly WORSE than keeping the state in memory:
a killed process never runs its cleanup, so the flag survives forever and the tool reports a reindex
in progress that is not. `flock` has no such failure mode, because the lock belongs to an open file
descriptor and **the kernel releases it when the process dies**. There is no cleanup to skip and no
staleness to age out. `test_a_killed_holder_leaves_the_lock_free` is that property, proven against a
real SIGKILLed process rather than asserted in a comment — it is the whole reason this is safe.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from codeintel.filelock import (
    _HAVE_FLOCK,
    exclusive,
    exclusive_waiting,
    held_by_another_process,
    lock_path,
)
from codeintel.reindexer import Reindexer

pytestmark = pytest.mark.skipif(not _HAVE_FLOCK, reason="advisory locking needs POSIX fcntl")


@pytest.fixture
def home(tmp_path, monkeypatch) -> pathlib.Path:
    monkeypatch.setenv("CODEINTEL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The primitive
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_a_second_holder_is_refused_while_the_first_holds_it(home):
    """`flock` binds to the open file DESCRIPTION, so two separate opens conflict even inside one
    process — which is what makes this testable without spawning anything."""
    path = lock_path("/repo/one")

    with exclusive(path) as first:
        assert first is True
        with exclusive(path) as second:
            assert second is False, "two holders at once — the lock is not exclusive"


def test_the_lock_is_released_when_the_block_exits(home):
    path = lock_path("/repo/one")
    with exclusive(path) as got:
        assert got
    with exclusive(path) as got_again:
        assert got_again, "the lock was not released on exit"


def test_probing_reports_held_only_while_it_is_held(home):
    path = lock_path("/repo/one")

    assert held_by_another_process(path) is False, "a lock nobody has taken reads as held"
    with exclusive(path):
        assert held_by_another_process(path) is True
    assert held_by_another_process(path) is False, "still reads held after release"


def test_two_roots_do_not_block_each_other(home):
    """Per repository, not global. A reindex of one repo must not stop a reindex of another."""
    with exclusive(lock_path("/repo/one")) as a, exclusive(lock_path("/repo/two")) as b:
        assert a and b


def test_the_slug_cannot_escape_the_lock_directory(home):
    """Every non-alphanumeric run collapses to `-`, which flattens away separators — so a crafted
    root cannot place a lock file outside the directory it belongs in."""
    path = lock_path("/../../etc/../../tmp/evil")

    assert path.parent.name == "locks"
    assert ".." not in path.name


def test_a_killed_holder_leaves_the_lock_free(home):
    """THE property, and the reason this is a `flock` rather than a flag file.

    A real child takes the lock and is SIGKILLed — the most hostile exit there is, with no chance to
    clean up. A flag-file design would leave the repository permanently marked "reindexing" after
    this. The kernel drops a `flock` with the process, so the next pass simply proceeds."""
    path = lock_path("/repo/killed")
    path.parent.mkdir(parents=True, exist_ok=True)   # the child only opens; it does not create dirs
    # `with` closes the pipe: an unclosed one is a ResourceWarning, which this suite makes an error.
    with subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, os, sys, time
            fd = os.open({str(path)!r}, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
            sys.stdout.write("held\\n"); sys.stdout.flush()
            time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "held"
            assert held_by_another_process(path) is True, "the child's lock is not visible"
        finally:
            child.kill()
            child.wait(timeout=10)

    assert held_by_another_process(path) is False, (
        "the lock survived the holder — this is exactly the stale-flag failure the design avoids")
    with exclusive(path) as got:
        assert got, "a new pass cannot acquire a lock whose holder was killed"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The reindexer using it
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _instrumented(monkeypatch) -> tuple[Reindexer, list[str]]:
    r = Reindexer(debounce_seconds=0)
    ran: list[str] = []
    monkeypatch.setattr(r, "_semantic_reindex", lambda root: ran.append("semantic"))
    monkeypatch.setattr(r, "_graph_reindex", lambda root: ran.append("graph"))
    return r, ran


def test_a_pass_runs_when_the_lock_is_free(home, monkeypatch):
    """The positive control. Without it, every assertion below passes for a reindexer that never
    does anything at all."""
    r, ran = _instrumented(monkeypatch)

    r._do_reindex(str(home))

    assert ran == ["semantic", "graph"]


def test_a_pass_skips_entirely_when_another_process_holds_the_lock(home, monkeypatch):
    """The whole point: not a shorter pass, no pass. The expensive half is embedding, which happens
    before any write, so a pass that 'starts anyway and notices later' would already have paid."""
    r, ran = _instrumented(monkeypatch)

    with exclusive(lock_path(os.path.realpath(str(home)))):
        r._do_reindex(str(home))

    assert ran == [], "work was done while another process held the lock"


def test_a_skipped_pass_still_advances_the_generation(home, monkeypatch):
    """Skipping means somebody else is rebuilding this index, so it IS about to move. Leaving the
    generation pinned would keep serving cached structural answers across that change — the exact
    staleness the counter exists to prevent, arriving through the dedupe added to save CPU."""
    r, _ran = _instrumented(monkeypatch)
    before = r.generation(str(home))

    with exclusive(lock_path(os.path.realpath(str(home)))):
        r._do_reindex(str(home))

    assert r.generation(str(home)) == before + 1
    assert r.reindex_pending(str(home)) is False, "the in-flight entry was not cleared"


def test_reindex_pending_sees_a_lock_held_outside_this_process(home):
    """The cross-process half. Before this, each process reported only on its own set, so a
    terminal `codeintel query` said nothing was happening while the MCP server rebuilt the repo —
    and a restarted server said the same about its predecessor's pass."""
    r = Reindexer(debounce_seconds=0)
    root = str(home)

    assert r.reindex_pending(root) is False
    with exclusive(lock_path(os.path.realpath(root))):
        assert r.reindex_pending(root) is True, "another holder's pass is invisible"
    assert r.reindex_pending(root) is False


def test_an_unusable_lock_directory_never_stops_indexing(home, monkeypatch, tmp_path):
    """Asymmetric degradation, and this is the direction that matters. A locking failure must not
    be able to prevent a repository being indexed — the fallback is the behaviour this module
    replaced, not an outage."""
    import codeintel.filelock as fl

    monkeypatch.setattr(fl, "lock_path", lambda _root: tmp_path / "no" / "such" / "dir" / "x.lock")
    monkeypatch.setattr(fl.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    r, ran = _instrumented(monkeypatch)

    r._do_reindex(str(home))

    assert ran == ["semantic", "graph"], "a lock failure stopped the indexing it was meant to guard"


def test_a_probe_that_fails_does_not_claim_a_reindex_is_running(home, monkeypatch):
    """The other direction of the same asymmetry: we do not report a reindex in progress on
    evidence we could not gather. That is the rule `gaps` follow one level up."""
    import codeintel.filelock as fl

    path = lock_path("/repo/one")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    monkeypatch.setattr(fl, "exclusive", lambda _p: (_ for _ in ()).throw(OSError("nope")))

    assert fl.held_by_another_process(path) is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Waiting — for work a person asked for
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_a_free_lock_is_taken_at_once_without_announcing_a_wait(home):
    waits: list[int] = []
    with exclusive_waiting(lock_path("/repo/one"), 5, on_wait=lambda: waits.append(1)) as got:
        assert got is True
    assert waits == []


def test_a_held_lock_is_waited_for_and_then_taken(home):
    """The foreground case: another pass is running, so wait for it rather than skip or duplicate."""
    path = lock_path("/repo/one")
    waits: list[int] = []
    holding, release = threading.Event(), threading.Event()

    def holder() -> None:
        with exclusive(path) as got:
            assert got
            holding.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    holding.wait(5)
    threading.Timer(0.4, release.set).start()

    started = time.monotonic()
    with exclusive_waiting(path, 10, poll_s=0.05, on_wait=lambda: waits.append(1)) as got:
        waited = time.monotonic() - started
        assert got is True
        assert held_by_another_process(path), "yielded True without actually holding the lock"
    t.join(5)

    assert waits == [1], "the wait must be announced exactly once"
    assert waited >= 0.3, f"took the lock after {waited:.2f}s while it was still held"


def test_a_lock_held_past_the_timeout_yields_false_so_the_work_still_happens(home):
    """A hung holder must not block indexing forever: time out, say so, proceed."""
    path = lock_path("/repo/one")
    waits: list[int] = []
    with exclusive(path) as first:
        assert first
        with exclusive_waiting(path, 0.2, poll_s=0.05, on_wait=lambda: waits.append(1)) as got:
            assert got is False
    assert waits == [1]
