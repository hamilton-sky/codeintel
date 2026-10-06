"""Does a name-matched row's file write the token the name match did not use?

A `callers StrategyChain.resolve` answer binds most of its rows by the LEAF name, `resolve`, which
in TypeScript is also what every `new Promise((resolve, reject) => …)` binds. The part of the target
the match did NOT use — `StrategyChain` — is what separates the two, and the answer has printed the
command that checks it since the settle note was added:

    _Settle it: `rg -n --fixed-strings 'StrategyChain' <root>`_

This module runs that command's substance. Nothing more: it reports, per file, whether the token
appears in it. That is a FACT about the text, and it is published as one.

WHY IT IS NOT A VERDICT, and why nothing here removes a row. Absence of the qualifier is strong
evidence and it is not proof:

  * a caller holds an instance it got from elsewhere and never writes the class — a module
    singleton (`chain = StrategyChain()` in `registry.py`, `import { chain }` in the caller), an
    untyped constructor argument, a factory call (`getChain().resolve()`), an alias
    (`Chain = StrategyChain`). Ordinary everywhere, and where untyped injection is the default —
    Python — no edge case;
  * a collaborator injected by interface — `constructor(private chain: ChainPort)` where
    `StrategyChain implements ChainPort` — genuinely reaches the method from a file that never
    writes the class's name;
  * a subclass inherits the method, and its callers name the subclass;
  * a re-export can rename it — `export { StrategyChain as Chain }` — so the caller writes `Chain`.

All four are ordinary code. Treating "the file does not name it" as "this is not a caller" would
answer "no callers" for code that has them, which is the deletion trap this project treats as its
worst outcome. So the scan ranks and labels, and the caller decides. Whatever it cannot judge
reliably is left unknown, never refuted.

WHAT IT READS, and how little. Only files a row already names, only through the same containment
check every other reader of the tree goes through (`containment.contained_path`: a symlink or hard
link that leaves the root is never opened), only REGULAR files (a FIFO planted at an indexed path
would block `open` forever), and a file larger than `MAX_SOURCE_BYTES` is not read at all
(`changed_range`'s own bound, so the two readers cannot disagree about what is too large to read).
A file that is not plain text — a UTF-16 or UTF-32 byte-order mark, or a NUL byte anywhere in it — is
unknown too: the printed `rg` decodes a wide encoding, and stops searching a file at its first NUL,
while this scan matches raw UTF-8 bytes end to end, so the two would disagree about the same file.
The ceilings below bound the cost of an answer by construction, rather than by hoping the repository
is small: a query that opened four thousand files to sharpen a note would be a worse defect than the
one being sharpened. They are a file count, a byte total and the seconds spent reading, and a file not
read before any of them is reached is unknown, not clean. The seconds are a real deadline: each read
runs on its own daemon thread and is waited on only for the time left, so a read stalled on a slow
mount cannot hold the answer past it — it is abandoned, and it and every file after it are unknown.
It never raises — it is an improvement on an answer that is already whole.
"""
from __future__ import annotations

import codecs
import functools
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable
from time import monotonic as _clock
from typing import TypeVar

from codeintel.changed_range import MAX_SOURCE_BYTES
from codeintel.containment import contained_path, real_root
from codeintel.provider import log_swallowed

# `callers` prints at most `_EDGE_ENDPOINT_CAP` distinct callers, and a caller can hold more than one
# edge, so in practice a scan sees well under a hundred files. The ceiling is for the day that stops
# being true, and past it the files are left unjudged rather than the answer left slow.
_MAX_FILES = 200
_MAX_TOTAL_BYTES = 16_000_000
# The seconds ONE answer may spend reading files. Reads of a warm local tree take milliseconds; this is
# for the tree on a slow or stalled mount, where two hundred small reads are not small.
_MAX_SECONDS = 5.0

# What marks a file as not plain text: a wide-encoding byte-order mark, or a NUL ANYWHERE in it. Not
# only in the first 8 KB: `rg` stops searching a file at the first NUL it meets, so a file that writes
# the token only after a late NUL is "no match" to the printed command and would be `true` here. The
# whole file is at most `MAX_SOURCE_BYTES`, so looking at all of it costs nothing worth saving.
_WIDE_BOMS = (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


class Budget:
    """What one ANSWER may spend reading files — seconds, files and bytes — shared by every scan it runs.

    `changed` asks `callers` once per changed symbol, forty at most, each on a pool worker, so a bound
    per scan is forty times the bound the answer was promised. A deadline set when the op starts is no
    better: the lookups it spends most of its time on are backend round trips that take seconds each,
    so by the time the later symbols are scanned the instant has passed and every one of them would
    come back unknown. What is bounded here is the time spent READING, which is the thing that stalls,
    summed over the threads that read — conservative for a pool, and exact for `callers`, which is one
    scan.

    The file and byte ceilings are the answer's for the same reason. Counted per scan, `changed`'s
    forty scans would each get the full two hundred files and sixteen megabytes, and on a fast local
    tree finish all of it inside the time allowance — forty times the I/O the answer was promised."""

    def __init__(self, seconds: float | None = None, *, files: int | None = None,
                 total_bytes: int | None = None) -> None:
        # The module's ceilings are read here, not bound as defaults at import, so they stay in one
        # place and a test that lowers one lowers it for every budget made after.
        self._left = _MAX_SECONDS if seconds is None else seconds
        self._files = _MAX_FILES if files is None else files
        self._bytes = _MAX_TOTAL_BYTES if total_bytes is None else total_bytes
        self._lock = threading.Lock()

    def take_file(self) -> bool:
        """Claim one file read; False once the answer's files or bytes are used up."""
        with self._lock:
            if self._files <= 0 or self._bytes <= 0:
                return False
            self._files -= 1
            return True

    def spend_bytes(self, count: int) -> None:
        with self._lock:
            self._bytes -= count

    def spend(self, seconds: float) -> None:
        with self._lock:
            self._left -= seconds

    def exhaust(self) -> None:
        """Nothing more is to be read on this budget: a read outlived it, and is still stalled."""
        with self._lock:
            self._left = 0.0

    @property
    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._left)

    @property
    def exhausted(self) -> bool:
        return self._left <= 0


# Reads abandoned past their deadline and still blocked, across the whole PROCESS. Each answer leaves
# at most one behind, but a server queried again and again about a tree on a stalled mount would leave
# one per answer, until it ran out of threads or memory. So while `_MAX_STALLED` of them are still
# blocked, no scan starts another read: it judges nothing, and the answer prints the `rg` command
# instead — what it printed before the scan existed. Reading resumes on its own once they return.
_MAX_STALLED = 1
_stalled: set[threading.Thread] = set()
_stalled_lock = threading.Lock()
# That check alone is not atomic: scans arriving together — concurrent requests, `changed`'s pool —
# can all see nothing stalled and each start a read that then stalls. So every blocking call also
# holds one of `_MAX_READERS` process-wide slots from before its thread starts until the call RETURNS.
# A stalled call keeps its slot, so however scans race, no more than `_MAX_READERS` threads can ever
# be left blocked; a scan that finds every slot held judges nothing, as above.
_MAX_READERS = 4
_slots = threading.BoundedSemaphore(_MAX_READERS)

_T = TypeVar("_T")


def _stalled_reads() -> int:
    """How many abandoned reads are still blocked, forgetting the ones that have since returned."""
    with _stalled_lock:
        _stalled.difference_update([t for t in _stalled if not t.is_alive()])
        return len(_stalled)


def _plain_text(blob: bytes) -> bool:
    return not blob.startswith(_WIDE_BOMS) and b"\0" not in blob


def files_naming(
    root: str, token: str, files: Iterable[str], *, budget: Budget | None = None,
) -> dict[str, bool] | None:
    """Which of *files* contain *token*, as `{relative path: True/False}`.

    A file is ABSENT from the mapping when it could not be read, was too large, is not a regular file
    or not plain text, lies outside the root, or fell past a budget (files, bytes or the seconds in
    *budget*, by default a fresh `_MAX_SECONDS` for this call alone) — never recorded as
    `False`. "This file does not write `StrategyChain`" and "we did not look at this file" are
    different facts, and collapsing them is how a summary comes to be true of a proxy and read as a
    claim about the thing itself. The caller renders a missing key as unknown.

    Returns ``None`` when the scan judged nothing: no token, no readable root, no file to look at, or
    not one file that could be read. An empty mapping would say every file was checked and none
    matched, which is the opposite of what happened.

    The match is a substring, exactly what the printed `rg --fixed-strings` would match, so the
    note that states this result and the command that reproduces it cannot disagree. A substring is
    the lenient direction: `FallbackStrategyChain` satisfies a scan for `StrategyChain`, which can
    only keep a row, never refute one.
    """
    if not token or not root:
        return None
    try:
        return _scan(root, token, files, budget if budget is not None else Budget())
    except Exception as exc:                        # an embedded NUL in a path, a vanished mount
        log_swallowed("qualifier_scan.files_naming", exc)
        return None


def _root(root: str) -> str | None:
    return real_root(root) if os.path.isdir(root) else None


def _scan(root: str, token: str, files: Iterable[str], budget: Budget) -> dict[str, bool] | None:
    if budget.exhausted:
        return None                                 # nothing left to read with: start no call at all
    if _stalled_reads() >= _MAX_STALLED:
        return None                                 # see `_MAX_STALLED`: judged nothing, prints the command
    # Checking and canonicalising the root are filesystem calls too, and on a stale mount they block
    # like a read does, so they run under the same deadline.
    began = _clock()
    root_real, finished = _within(functools.partial(_root, root), budget.remaining)
    budget.spend(_clock() - began)
    if not finished:
        budget.exhaust()
        return None
    if root_real is None:
        return None
    needle = token.encode("utf-8", "replace")
    seen: dict[str, bool] = {}
    for rel in dict.fromkeys(f for f in files if f):
        if budget.exhausted or not budget.take_file():
            break
        began = time.monotonic()
        blob, finished = _within(functools.partial(_read, root_real, os.path.join(root, rel)),
                                 budget.remaining)
        budget.spend(time.monotonic() - began)
        if not finished:
            # The read outlived what was left of the allowance and is still blocked — a stalled mount.
            # Charging it afterwards would not bound anything, so it is abandoned here: the budget is
            # spent, and this file and every one after it are unknown.
            budget.exhaust()
            break
        if blob is None:
            continue
        budget.spend_bytes(len(blob))
        if _plain_text(blob):
            seen[rel] = needle in blob
    return seen or None


def _within(call: Callable[[], _T], seconds: float) -> tuple[_T | None, bool]:
    """*call* on a daemon thread holding one of `_MAX_READERS` slots, waited on for at most *seconds*
    in all: `(result, finished)`.

    A filesystem call cannot be interrupted, so the thread is what bounds it: when it has not returned
    in time, `finished` is False and the thread is left to end whenever the stalled call does — still
    holding its slot, which is what keeps the number left behind finite however scans race. A scan
    that cannot get a slot in time is treated the same way: every slot is held, most likely by calls
    still blocked. An exception in the call is re-raised here, so a path the OS refuses (an embedded
    NUL) still abandons the whole scan exactly as it did when the read ran inline."""
    if seconds <= 0:
        # Nothing can finish in no time, and a thread started only to be abandoned at once would be
        # recorded as stalled while it is merely running — shutting every other scan out until it ends.
        return None, False
    deadline = _clock() + seconds
    slots = _slots                                  # the semaphore this call takes is the one it gives back
    if not slots.acquire(timeout=seconds):
        return None, False
    box: list[tuple[_T | None, BaseException | None]] = []

    def work() -> None:
        try:
            box.append((call(), None))
        except BaseException as exc:               # carried to the caller, never lost on the thread
            box.append((None, exc))
        finally:
            slots.release()

    worker = threading.Thread(target=work, name="codeintel-qualifier-read", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - _clock()))
    if worker.is_alive() or not box:
        with _stalled_lock:
            _stalled.add(worker)
        return None, False
    value, exc = box[0]
    if exc is not None:
        raise exc
    return value, True


def _read(root_real: str, path: str) -> bytes | None:
    """The bytes of one file, or ``None`` when it is not to be judged — outside the root, not a regular
    file, too large, unreadable, or grown past the limit between the stat and the read."""
    safe = contained_path(root_real, path)
    if safe is None:
        return None
    try:
        info = os.stat(safe)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            return None                             # a FIFO would block the open; a big file is unknown
        with open(safe, "rb") as fh:
            blob = fh.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        return None                                 # unreadable stays unknown
    return blob if len(blob) <= MAX_SOURCE_BYTES else None
