"""The single definition of "this file is safely inside the indexed root".

Containment used to live entirely inside ``Indexer._walk_files`` — that is, it was a property of
the indexing *walk* rather than of the data path. Everything that read indexed content afterwards
re-derived the path itself and opened it directly. That left a standing hole rather than a race:

  1. A file inside an allowed root is indexed normally, and its path is stored in ``chunk_hashes``.
  2. The file is later replaced with a symlink pointing outside the root.
  3. ``_cleanup_deleted`` asked ``(root / stored_path).exists()``, which FOLLOWS the symlink and
     answers True, so the row was never reconciled away.
  4. ``Searcher._read_snippet`` re-opened ``root / stored_path`` to build the result snippet, with
     no check of its own, and served the outside file's bytes.

The precondition is only "can write inside their own root" — exactly the tenant the RBAC docstring
names. The fix is not another per-site ``realpath`` call: that is the pattern that produced the
"a third and fourth renderer were still leaking" bug. Containment is asserted HERE, and the
indexer, the searcher, and the cleanup pass all ask this module rather than reimplementing it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class ContainmentError(Exception):
    """A path resolved outside its root, or is reachable from outside it."""


def real_root(project_root: str) -> str:
    """The canonical form a root is compared against. Trailing separators are stripped so that
    ``/repo/`` and ``/repo`` cannot disagree about whether ``/repo-secrets`` is inside them."""
    return os.path.realpath(project_root).rstrip(os.sep)


def contained_path(root_real: str, candidate: str | Path) -> str | None:
    """Resolve *candidate* and return it only if it is genuinely inside *root_real*.

    ``None`` means "do not read this", and the caller must treat that as a hard skip rather than
    falling back to opening the raw path. Two distinct escapes are checked:

    * **Symlinks** — ``realpath`` resolves them, so a link pointing outside lands outside the root
      prefix. The prefix compare appends a separator so ``/repo-secrets`` is not inside ``/repo``.
    * **Hard links** — a second directory entry for the same inode is *physically* inside the root,
      so its realpath is inside and the prefix check passes; ``realpath`` cannot see it. There is
      no portable way to ask "does this inode also live outside?", so extra links are disqualifying.
      Measured at 0 occurrences across 3213 source files of a real repository, so the
      false-positive cost is nil.
    """
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return None
    if real != root_real and not real.startswith(root_real + os.sep):
        return None
    try:
        if os.stat(real).st_nlink > 1:
            return None
    except OSError:
        return None
    return real


def open_contained(root_real: str, candidate: str | Path, **kwargs):
    """``open()`` for indexed content, refusing anything that escapes *root_real*.

    Raises ``ContainmentError`` rather than returning a sentinel, so a caller that wraps reads in a
    broad ``except Exception`` cannot quietly downgrade an escape to "file not found" — the escape
    is logged at WARNING here regardless of what the caller does with the exception.

    A path that simply DOES NOT EXIST is reported as ``FileNotFoundError``, not as an escape.
    ``contained_path`` answers the single question "may this be read", and a deleted file is a
    legitimate no — but it is also the most ordinary thing that can happen to an indexed file, and
    reporting it as a refusal meant every deletion between indexing and a query logged a WARNING
    asking whether a symlink had been "planted after indexing" and rendered the hit as
    ``[refused: resolves outside the indexed root]``. That is a false security alarm on a routine
    event, and it drowns the real ones. The distinction is made HERE rather than by relaxing
    ``contained_path``, whose ``None`` also drives ``_cleanup_deleted``'s reconciliation of deleted
    files — loosening it there would leave their rows in the index forever.
    """
    safe = contained_path(root_real, candidate)
    if safe is None:
        # lexists(), not exists(): a dangling symlink still EXISTS as a link, and a link whose
        # target vanished is a construct worth refusing loudly rather than calling "not found".
        if not os.path.lexists(candidate):
            logger.debug("indexed file no longer exists: %s", candidate)
            raise FileNotFoundError(str(candidate))
        logger.warning(
            "refusing to read %s — it does not resolve to a file inside %s (symlink or hard link "
            "planted after indexing?)", candidate, root_real,
        )
        raise ContainmentError(str(candidate))
    return open(safe, **kwargs)


# What each refusal reason from ``write_contained`` means, worded for the line a user reads.
REFUSAL_TEXT = {
    "symlink": "it is a symlink",
    "outside-repo": "it resolves outside the repo",
    "inside-git-dir": "it resolves inside the repo's .git directory",
}


def _inside(root_real: str, real: str) -> bool:
    return real == root_real or real.startswith(root_real + os.sep)


def write_contained(root: str, name: str, content: str, *, follow_links: bool) -> str | None:
    """The one write path for files codeintel writes into a repo (``CODE_INTEL.md``, ``AGENTS.md``,
    ``CLAUDE.md``, ``USING_CODEINTEL.md``). Returns ``None`` on success, or the refusal reason
    (a key of ``REFUSAL_TEXT``). I/O errors raise.

    A cloned, untrusted repo can ship ``CODE_INTEL.md -> ~/.bashrc``; a plain ``open(path, "w")``
    then overwrites the user's file on a routine ``codeintel map``. So:

    * ``follow_links=False`` (codeintel's own generated file): any symlink is refused, wherever it
      points — outside, inside, or nowhere.
    * ``follow_links=True`` (files the user may share by symlink): refused when the realpath leaves
      the root (a link out, a parent directory linked out, a dangling link) or lands in the repo's
      ``.git`` directory (``.git/config``, ``.git/hooks/…`` turn a write into code execution).
      A link to a regular file elsewhere in the work tree is written through, and kept.

    The write is atomic: a sibling temp file opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` (so neither a
    planted temp name nor a dangling link is followed), the existing mode carried over, then
    ``os.replace`` onto the resolved path.
    """
    path = os.path.join(root, name)
    if not follow_links:
        if os.path.islink(path):
            return _refuse_write(path, "symlink")
        resolved = path
    else:
        root_real = real_root(root)
        if os.path.lexists(path):
            safe = contained_path(root_real, path)
            if safe is None:
                return _refuse_write(path, "outside-repo")
            resolved = safe
        else:
            # Not there yet: realpath resolves any linked parent, and the file is created below.
            resolved = os.path.realpath(path)
            if not _inside(root_real, resolved):
                return _refuse_write(path, "outside-repo")
        git_dir = os.path.realpath(os.path.join(root_real, ".git"))
        if os.path.isdir(git_dir) and _inside(git_dir, resolved):
            return _refuse_write(path, "inside-git-dir")

    try:
        mode: int | None = os.stat(resolved).st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    tmp = f"{resolved}.{os.getpid()}.{os.urandom(4).hex()}.codeintel.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if mode is not None:
            os.chmod(tmp, mode)   # the umask must not narrow or widen the old file's mode
        os.replace(tmp, resolved)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return None


def _refuse_write(path: str, reason: str) -> str:
    logger.warning("refusing to write %s: %s (%s)", path, REFUSAL_TEXT[reason], reason)
    return reason
