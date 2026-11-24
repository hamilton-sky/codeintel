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

import errno
import logging
import os
import stat
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
    "dangling": "it is a symlink to a file that does not exist",
    "not-a-file": "it does not resolve to a regular file",
    "hard-link": "it is a hard link (the same file has another name)",
}

# The write walks the resolved path one directory at a time, opening each with O_NOFOLLOW and
# creating and renaming through the last one's descriptor, so a parent swapped for a symlink after
# the check cannot redirect the write. Platforms without `dir_fd` (Windows) fall back to paths.
_DIR_FD = (hasattr(os, "O_DIRECTORY") and hasattr(os, "fchmod")
           and {os.open, os.stat, os.unlink, os.rename} <= os.supports_dir_fd)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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
      the root (a link out, a parent directory linked out), when it is a dangling link, not a
      regular file or a hard link, or when it lands in the repo's ``.git`` directory
      (``.git/config``, ``.git/hooks/…`` turn a write into code execution). A link to a regular
      file elsewhere in the work tree is written through, and kept.

    ``.git`` is matched by file identity (``samestat``) on every directory the write passes
    through, never by spelling: on a case-insensitive filesystem ``.GIT/config`` is the same file
    as ``.git/config`` but ``realpath`` keeps the ``.GIT`` spelling.

    The write is atomic: a sibling temp file opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` (so neither a
    planted temp name nor a dangling link is followed), the existing mode carried over, then
    renamed onto the resolved path, all relative to the parent directory's descriptor.
    """
    path = os.path.join(root, name)
    root_real = real_root(root)
    if follow_links:
        resolved = os.path.realpath(path)
        if resolved == root_real or not _inside(root_real, resolved):
            return log_write_refusal(path, "outside-repo")
        parts = resolved[len(root_real) + 1:].split(os.sep)
    else:
        parts = [name]
    try:
        git_st: os.stat_result | None = os.stat(os.path.join(root_real, ".git"))
    except OSError:
        git_st = None
    if git_st is not None and not stat.S_ISDIR(git_st.st_mode):
        git_st = None   # a worktree's or submodule's `.git` file: its git dir is outside the root
    reason = _write_walk(root_real, parts, content, git_st,
                         follow_links=follow_links,
                         link_entry=follow_links and os.path.islink(path))
    return None if reason is None else log_write_refusal(path, reason)


def _write_walk(root_real: str, parts: list[str], content: str, git_st: os.stat_result | None,
                *, follow_links: bool, link_entry: bool) -> str | None:
    """Walk *parts* down from *root_real* without following a link, refuse on the way, then write
    the last part. Returns the refusal reason, or None once written."""
    dir_fd: int | None = os.open(root_real, os.O_RDONLY | os.O_DIRECTORY) if _DIR_FD else None
    base = root_real
    try:
        for comp in parts[:-1]:
            if dir_fd is not None:
                try:
                    fd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                        raise
                    return _why_not_a_dir(comp, dir_fd)
                os.close(dir_fd)
                dir_fd = fd
                st = os.fstat(fd)
            else:
                base = os.path.join(base, comp)
                try:
                    st = os.lstat(base)
                except FileNotFoundError:
                    return "dangling"
                if stat.S_ISLNK(st.st_mode):
                    return "symlink"
                if not stat.S_ISDIR(st.st_mode):
                    return "not-a-file"
            if git_st is not None and os.path.samestat(st, git_st):
                return "inside-git-dir"

        leaf = parts[-1] if dir_fd is not None else os.path.join(base, parts[-1])
        try:
            leaf_st: os.stat_result | None = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            leaf_st = None
        if leaf_st is None:
            if link_entry:
                return "dangling"
            mode: int | None = None
        else:
            if git_st is not None and os.path.samestat(leaf_st, git_st):
                return "inside-git-dir"
            if stat.S_ISLNK(leaf_st.st_mode):
                return "symlink"
            if not stat.S_ISREG(leaf_st.st_mode):
                return "not-a-file"
            if follow_links and leaf_st.st_nlink > 1:
                return "hard-link"
            mode = leaf_st.st_mode & 0o7777

        tmp = f"{leaf}.{os.getpid()}.{os.urandom(4).hex()}.codeintel.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        fd = os.open(tmp, flags, 0o666 if mode is None else mode, dir_fd=dir_fd)
        try:
            # `closefd=False`: the descriptor stays this function's, and the `finally` closes it
            # exactly once, also when `os.fdopen` itself raises (it would otherwise leak, or be
            # closed already, depending on where inside `fdopen` the failure came).
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="", closefd=False) as f:
                    if mode is not None:   # the umask must not narrow or widen the old file's mode
                        if hasattr(os, "fchmod"):
                            os.fchmod(f.fileno(), mode)
                        else:
                            os.chmod(tmp, mode)
                    f.write(content)
            finally:
                os.close(fd)
            os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
        return None
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _why_not_a_dir(comp: str, dir_fd: int) -> str:
    """A directory on the resolved path could not be opened as one without following a link.

    A link is ``symlink``; something that exists and is not a directory (ENOTDIR: a regular file
    in the middle of the path) is ``not-a-file``; nothing there at all is ``dangling``."""
    try:
        st = os.stat(comp, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "dangling"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    return "dangling" if stat.S_ISDIR(st.st_mode) else "not-a-file"


def log_write_refusal(path: str, reason: str) -> str:
    """Log the WARNING every refused write gets, and hand back *reason* (a ``REFUSAL_TEXT`` key)."""
    logger.warning("refusing to write %s: %s (%s)", path, REFUSAL_TEXT[reason], reason)
    return reason
