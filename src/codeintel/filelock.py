"""A cross-process advisory lock, used to stop two codeintel processes reindexing one repo at once.

MEASURED 2026-09-18: two `codeintel index` passes started together on a 600-file repository each
embedded all 600 chunks and each reported "Indexed 600 chunks". There is no natural serialisation —
both read the same "what is new" answer from the database before either had written anything, so
the whole embedding cost is paid twice. On a large repository that is ten minutes of CPU doubled,
and the two passes contend for the same cores while doing it.

WHY `flock` AND NOT A FLAG FILE. The obvious way to record "a reindex is running" is to write a
file and delete it at the end. That is strictly WORSE than keeping the state in memory: a process
that is killed never runs its cleanup, so the flag survives forever and the tool reports a reindex
in progress that is not. A signal true of a FILE and false of the WORLD is the defect this
repository catalogues in `tests/test_summary_integrity.py`, and buying one to save some CPU would
be a poor trade.

`flock` has no such failure mode, and that is the entire reason it is the primitive here: the lock
belongs to an open file descriptor, and **the kernel releases it when the process dies**, however it
dies. There is no cleanup to skip and no staleness to age out. The lock file itself is left behind,
which is fine — it is an empty file whose existence means nothing; only the lock on it does.

DEGRADES, NEVER BLOCKS THE WORK. `fcntl` is POSIX and this package claims `OS Independent`, so
every entry point here has a fallback, and the two fallbacks are deliberately asymmetric:

* failing to ACQUIRE degrades to "you hold it" — indexing still happens, exactly as it did before
  this module existed. A locking bug must never be able to stop a repository being indexed.
* failing to PROBE degrades to "nobody holds it" — we do not claim a reindex is running on evidence
  we could not gather. That is the same rule the envelope's `gaps` follow one level up.
"""
from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import re
from collections.abc import Iterator

logger = logging.getLogger(__name__)

try:                                    # POSIX only; Windows has no fcntl
    import fcntl
    _HAVE_FLOCK = True
except Exception:                       # pragma: no cover - platform dependent
    fcntl = None                        # type: ignore[assignment]
    _HAVE_FLOCK = False


def lock_path(canonical_root: str) -> pathlib.Path:
    """The lock file for one repository root.

    Slugged the same way the graph cache names its files — every run of non-alphanumeric characters
    collapses to a single `-` — which also flattens away every path separator, so a crafted root can
    never place the lock outside the lock directory.
    """
    from codeintel.paths import codeintel_home

    slug = re.sub(r"[^A-Za-z0-9]+", "-", canonical_root).strip("-") or "root"
    return codeintel_home() / "locks" / f"reindex-{slug}.lock"


@contextlib.contextmanager
def exclusive(path: pathlib.Path) -> Iterator[bool]:
    """Hold an exclusive, non-blocking lock on *path*. Yields whether we got it.

    `False` means another LIVE process holds it — not that one once did. Callers use that to skip
    work somebody else is already doing, so the answer has to be about the world rather than about
    a file that happens to exist.
    """
    if not _HAVE_FLOCK:
        # No advisory locking available: behave exactly as the code did before this module.
        yield True
        return

    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except Exception as exc:
        # An unwritable home is not a reason to stop indexing.
        logger.debug("reindex lock unavailable at %s: %s", path, exc)
        if fd is not None:
            with contextlib.suppress(Exception):
                os.close(fd)
        yield True
        return

    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False            # somebody else is mid-pass
        yield acquired
    finally:
        with contextlib.suppress(Exception):
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)                # closing would release it anyway; explicit is clearer


def held_by_another_process(path: pathlib.Path) -> bool:
    """True when some other live process holds this lock.

    Implemented by trying to take it and letting go immediately, because that is the only question
    the kernel will answer — "is this lock free right now". The momentary acquisition is harmless:
    a real pass that loses this race retries on the next debounce window, and the alternative
    (recording holders in a file) is the stale-state design this module exists to avoid.

    Degrades to False. Reporting "a reindex is running" on evidence we could not gather would be a
    claim about the world made from a failed syscall.
    """
    if not _HAVE_FLOCK or not path.exists():
        return False
    try:
        with exclusive(path) as got:
            return not got
    except Exception as exc:
        logger.debug("reindex lock probe failed for %s: %s", path, exc)
        return False
