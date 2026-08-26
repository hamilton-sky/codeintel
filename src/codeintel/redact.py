"""Strip the user's home directory out of anything the tool says.

codeintel answers travel: into agent transcripts, into `CODE_INTEL.md` files that get committed and
pushed, and across the HTTP transport to callers who are not the machine's owner. An absolute path
like `/Users/alice/Documents/work/client-project/backend` discloses the account name and the
directory layout of everything the author is working on — including, by name, projects the reader
was never meant to know exist.

This had been swept by hand before, and the sweep missed channels: the graph provider's own comments
record that `_display` was fixed first, `_render_scan` was missed and shipped, and `chain` and
`pattern` turned out to be a third and fourth. A per-renderer sweep cannot converge, because each
new renderer is a new chance to forget. So this runs once, at the envelope boundary, over every
field that reaches a caller.

`~` is used rather than a placeholder because the hints contain runnable commands
(`codeintel index ~/Documents/project/app`), and a shell expands `~` back to the right thing. The
path stays actionable for the person who owns it and says nothing about who that is.

**Scope, stated so it is not mistaken for an oversight.** This strips THIS process's own home
directory prefix. It deliberately does not try to erase the account name everywhere it might
appear:

* A sibling directory that shares the username (`/Users/alice-backup` next to `/Users/alice`) is a
  different directory and is left intact. Rewriting it is what the pre-boundary substring match
  did, and it produced paths that do not exist.
* Another account's home (`/Users/bob/...`, reachable over the HTTP transport when the server
  answers for a repo it does not own) cannot be mapped to `~` — `~` would claim it as the reader's.
  Nor can it be blanket-rewritten: `/home/runner/work/...` on CI is home-shaped, carries nothing
  sensitive, and mangling it would break the actionability this module exists to preserve.

`looks_like_any_home_path` is the detector for what falls outside that scope; the tests assert on
it, so a NEW leak channel of either shape fails the suite rather than shipping quietly.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

_MAX_DEPTH = 40


def _home() -> str:
    try:
        h = os.path.expanduser("~")
    except Exception:
        return ""
    return h.rstrip(os.sep) if h and h != "~" else ""


def _flattened_home() -> str:
    """The home path as the graph backend spells it in a project id: separators become dashes.

    `/Users/alice` becomes `Users-alice`. Redaction matched only the slash form, so a project id —
    which for a path-slug registration IS the flattened absolute path — sailed straight through.
    Found on a Go repository, where flat qualified names meant the project prefix was not stripped
    either, and every result row read
    `private-tmp-...-Users-alice-...-cobra.Execute`. Two independent defences both missed it, which
    is the argument for having two.
    """
    home = _home()
    if not home:
        return ""
    # BOTH separators, and the drive colon. This was written `home.strip("/").replace("/", "-")`,
    # which is POSIX-only: on Windows `C:\\Users\\alice` came back unchanged, so the detector
    # returned False for a leak that plainly contained the username. The macOS/Linux form of this
    # bug was found by evaluating a third LANGUAGE; this one is the same defect one PLATFORM over,
    # and nothing in CI would have caught it — the matrix is ubuntu-only.
    flat = home.replace("\\", "/").strip("/").replace("/", "-").replace(":", "")
    # A slug is only a slug when the path had MORE THAN ONE segment. A single-segment home
    # (`/root`, overwhelmingly the container case) flattens to a bare, extremely common English
    # word, and blanket-replacing it rewrote ordinary prose inside the answer:
    #
    #     "root cause analysis"       -> "<home> cause analysis"
    #     "the root of the call tree" -> "the <home> of the call tree"
    #
    # Nothing is protected by that — `root` as a word discloses no account name and no layout —
    # while the corruption lands in comments, docstrings and every snippet the tool prints. The
    # flattened form exists to catch backend project ids like `Users-alice-Documents-project`;
    # a one-segment home produces no such id, so there is nothing here to match.
    return flat if "-" in flat else ""


# A home path is only a home path at a PATH BOUNDARY. Plain substring replacement corrupted any
# path that merely STARTED with the home string and continued into a different directory name:
#
#     HOME=/root      /rootfs/etc/config.py   ->  ~fs/etc/config.py
#     HOME=/root      /root_cause.md          ->  ~_cause.md
#     HOME=/home/sh   /home/shammai/app.py    ->  ~ammai/app.py
#
# The last one is the general form — nothing about `/root` is special, any home that is a string
# prefix of a real sibling path does it. This does not merely over-redact: it emits a path that
# does not exist, in the field the agent reads as the answer, with nothing marking it as damaged.
# Containers running as root make the `/root` case routine, which is why it was reported from one.
#
# The boundary differs by form because the SEPARATOR differs. In a slashed path `/` separates, so
# `-` and `.` can continue a directory name (`/Users/alice-backup` must not match `/Users/alice`).
# In the flattened project-id form `-` IS the separator, so it must be allowed to follow.
_SLASHED_TAIL = r"(?![\w.-])"
_FLAT_TAIL = r"(?![\w.])"


@lru_cache(maxsize=32)
def _bounded(needle: str, tail: str) -> re.Pattern[str]:
    """``needle`` matched only where a path token genuinely ends. Cached: the home path is fixed
    for the process, and this runs over every field of every answer."""
    return re.compile(re.escape(needle) + tail)


def redact_text(text: str) -> str:
    """Replace occurrences of the home directory with `~`, including its /private-prefixed form.

    macOS resolves `/tmp` to `/private/tmp` and reports realpaths accordingly, so a leak can arrive
    with a `/private` prefix that a plain home-prefix comparison would miss.

    Matching is boundary-anchored (see above): a path that only shares a prefix with the home
    directory is a DIFFERENT path and is left exactly as it is."""
    home = _home()
    if not home or not text:
        return text
    out = text
    for prefix in (f"/private{home}", home):
        out = _bounded(prefix, _SLASHED_TAIL).sub("~", out)
    flat = _flattened_home()
    if flat:
        # No `~` here: this appears mid-identifier, where a tilde would read as part of the name.
        out = _bounded(flat, _FLAT_TAIL).sub("<home>", out)
    return out


def redact(value: object, _depth: int = 0) -> object:
    """Redact a whole envelope in place-by-copy: strings, and the containers that hold them.

    Applied to the finished `Result`, so a field added later is covered without anyone remembering
    to route it through here — which is the failure mode the hand-sweeps kept hitting."""
    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, _depth + 1) for v in value)
    return value


def contains_home_path(text: str) -> bool:
    """Whether *text* still carries THIS process's home path in any form it has leaked in —
    slashed, /private-prefixed, or flattened into a backend project id. For tests. (It previously
    said "and for `doctor`", which no longer calls it — a stale claim about who enforces a privacy
    check is worse than no claim.) For paths belonging to *another* account, or ones that merely
    share the username, see `looks_like_any_home_path`.

    Uses the SAME boundary rule as ``redact_text``, deliberately. When the two disagree the
    detector reports a leak the redactor has no way to remove, and the only ways out are to
    corrupt a path that was never a leak or to teach the tests to ignore a real one."""
    home = _home()
    if not home or not text:
        return False
    for prefix in (f"/private{home}", home):
        if _bounded(prefix, _SLASHED_TAIL).search(text):
            return True
    flat = _flattened_home()
    return bool(flat) and bool(_bounded(flat, _FLAT_TAIL).search(text))


# A username-shaped absolute path, for the assertion that no NEW leak channel has opened using a
# home directory other than this process's own (a server answering for another account's repo).
_ABS_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s\"'`]+/")


def looks_like_any_home_path(text: str) -> bool:
    """Whether *text* still carries an absolute, user-home-shaped path of ANY account.

    Broader than ``contains_home_path``, which only knows this process's own home. That narrowness
    is the point of having both: ``redact_text`` can only rewrite paths it can recognise as its
    own, so everything it cannot — another account's home, or a sibling directory that merely
    shares the username — is invisible to the narrow detector and would ship unnoticed.

    This had no callers at all, which made the "two independent defences" the module claims a
    defence and a decoration. The tests assert on it now (see ``test_redaction_boundary.py``), so a
    new leak channel of either shape turns the suite red instead of shipping quietly.
    """
    return bool(_ABS_HOME_RE.search(text or ""))
