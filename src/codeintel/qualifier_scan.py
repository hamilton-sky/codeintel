"""Does a name-matched row's file name the token the name match did not use?

A `callers StrategyChain.resolve` answer binds most of its rows by the LEAF name, `resolve`, which
in TypeScript is also what every `new Promise((resolve, reject) => …)` binds. The part of the target
the match did NOT use — `StrategyChain` — is what separates the two, and the answer has printed the
command that checks it since `#34`:

    _Settle it: `rg -n --fixed-strings 'StrategyChain' <root>`_

This module runs that command's substance. Nothing more: it reports, per file, whether the token
appears in it. That is a FACT about the text, and it is published as one.

WHY IT IS NOT A VERDICT, and why nothing here removes a row. Absence of the qualifier is strong
evidence and it is not proof:

  * a collaborator injected by interface — `constructor(private chain: ChainPort)` where
    `StrategyChain implements ChainPort` — genuinely reaches the method from a file that never
    writes the class's name;
  * a subclass inherits the method, and its callers name the subclass;
  * a re-export can rename it — `export { StrategyChain as Chain }` — so the caller writes `Chain`.

All three are ordinary TypeScript. Treating "the file does not name it" as "this is not a caller"
would answer "no callers" for code that has them, which is the deletion trap this project treats as
its worst outcome, and `bench/fixtures/corpus_ts` now prices the trade: filtering the three
fabricated rows off `FallbackChain.resolve` also empties an answer for a symbol that has a caller.

So the scan ranks and labels. The caller decides.
"""
from __future__ import annotations

import os
from collections.abc import Iterable

# Ceilings, so the cost of an answer is bounded by construction rather than by hoping the repository
# is small. A query that opened four thousand files to sharpen a note would be a worse defect than
# the one being sharpened.
_MAX_FILES = 300
_MAX_FILE_BYTES = 1_000_000
_MAX_TOTAL_BYTES = 32_000_000


def files_naming(root: str, token: str, files: Iterable[str]) -> dict[str, bool] | None:
    """Which of *files* contain *token*, as `{relative path: True/False}`.

    A file is ABSENT from the mapping when it could not be read, was too large, or fell past a
    budget — never recorded as `False`. "This file does not name `StrategyChain`" and "we did not
    look at this file" are different facts, and collapsing them is how a summary comes to be true of
    a proxy and read as a claim about the thing itself. The caller renders a missing key as unknown.

    Returns ``None`` when the scan did not run at all: no token, no readable root, or more candidate
    files than the ceiling above. Again the distinction is deliberate — an empty mapping would say
    every file was checked and none matched.
    """
    if not token or not root or not os.path.isdir(root):
        return None
    paths = [p for p in dict.fromkeys(files) if p]
    if not paths or len(paths) > _MAX_FILES:
        return None

    needle = token.encode("utf-8", "replace")
    seen: dict[str, bool] = {}
    spent = 0
    for rel in paths:
        if spent >= _MAX_TOTAL_BYTES:
            break
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > _MAX_FILE_BYTES:
                continue
            with open(full, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue                                          # unreadable stays unknown
        spent += len(blob)
        seen[rel] = needle in blob
    return seen or None
