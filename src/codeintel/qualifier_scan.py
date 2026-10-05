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
A file that is not plain text — a UTF-16 or UTF-32 byte-order mark, or a NUL byte near the start — is
unknown too: the printed `rg` decodes a wide encoding and this scan matches raw UTF-8, so the two
would disagree about the same file.
The ceilings below bound the cost of an answer by construction, rather than by hoping the repository
is small: a query that opened four thousand files to sharpen a note would be a worse defect than the
one being sharpened. They are a file count, a byte total and the seconds spent reading, and a file not
read before any of them is reached is unknown, not clean. It never raises — it is an improvement on an
answer that is already whole.
"""
from __future__ import annotations

import codecs
import os
import stat
import threading
import time
from collections.abc import Iterable

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

# What marks a file as not plain text: a wide-encoding byte-order mark, or a NUL in the first 8 KB.
_WIDE_BOMS = (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_SNIFF_BYTES = 8192


class Budget:
    """The time one ANSWER may spend reading files, shared by every scan that answer runs.

    `changed` asks `callers` once per changed symbol, forty at most, each on a pool worker, so a bound
    per scan is forty times the bound the answer was promised. A deadline set when the op starts is no
    better: the lookups it spends most of its time on are backend round trips that take seconds each,
    so by the time the later symbols are scanned the instant has passed and every one of them would
    come back unknown. What is bounded here is the time spent READING, which is the thing that stalls,
    summed over the threads that read — conservative for a pool, and exact for `callers`, which is one
    scan."""

    def __init__(self, seconds: float = _MAX_SECONDS) -> None:
        self._left = seconds
        self._lock = threading.Lock()

    def spend(self, seconds: float) -> None:
        with self._lock:
            self._left -= seconds

    @property
    def exhausted(self) -> bool:
        return self._left <= 0


def _plain_text(blob: bytes) -> bool:
    return not blob.startswith(_WIDE_BOMS) and b"\0" not in blob[:_SNIFF_BYTES]


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


def _scan(root: str, token: str, files: Iterable[str], budget: Budget) -> dict[str, bool] | None:
    if not os.path.isdir(root):
        return None
    root_real = real_root(root)
    needle = token.encode("utf-8", "replace")
    seen: dict[str, bool] = {}
    spent = 0
    for rel in list(dict.fromkeys(f for f in files if f))[:_MAX_FILES]:
        if spent >= _MAX_TOTAL_BYTES or budget.exhausted:
            break
        began = time.monotonic()
        blob = _read(root_real, os.path.join(root, rel))
        budget.spend(time.monotonic() - began)
        if blob is None:
            continue
        spent += len(blob)
        if _plain_text(blob):
            seen[rel] = needle in blob
    return seen or None


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
