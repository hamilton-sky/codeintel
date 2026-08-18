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
"""

from __future__ import annotations

import os
import re

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
    return home.strip("/").replace("/", "-")


def redact_text(text: str) -> str:
    """Replace occurrences of the home directory with `~`, including its /private-prefixed form.

    macOS resolves `/tmp` to `/private/tmp` and reports realpaths accordingly, so a leak can arrive
    with a `/private` prefix that a plain home-prefix comparison would miss."""
    home = _home()
    if not home or not text:
        return text
    out = text
    for prefix in (f"/private{home}", home):
        if prefix in out:
            out = out.replace(prefix, "~")
    flat = _flattened_home()
    if flat and flat in out:
        # No `~` here: this appears mid-identifier, where a tilde would read as part of the name.
        out = out.replace(flat, "<home>")
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
    """Whether *text* still carries the home path in ANY form we have seen it leak in — slashed,
    /private-prefixed, or flattened into a backend project id. For tests and for `doctor`."""
    home = _home()
    if not home or not text:
        return False
    if home in text or f"/private{home}" in text:
        return True
    flat = _flattened_home()
    return bool(flat) and flat in text


# A username-shaped absolute path, for the assertion that no NEW leak channel has opened using a
# home directory other than this process's own (a server answering for another account's repo).
_ABS_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s\"'`]+/")


def looks_like_any_home_path(text: str) -> bool:
    return bool(_ABS_HOME_RE.search(text or ""))
