"""Line-number conversion for the backends whose numbering differs from ours.

**This module does not apply to every backend, and the line base is data, not a rule.** An earlier
version of this docstring asserted "the only place in codeintel permitted to format a path:line" and
"every backend we speak to counts lines from zero". The second claim is false, and an investigation
found that following the first would have introduced a NEW off-by-one: the graph backend
(codebase-memory-mcp) reports 1-based line numbers, so ``graph.py`` formats its own ``path:line``
directly and must keep doing so. Centralising a guarantee only removes a defect class when the
implementations agree on the semantics being centralised — here they do not.

    LINE BASES, verified by probing each backend — see LINE_BASES below:
      serena / LSP          -> use loc()/span()
      semantic chunk_start  -> use loc()      (chunk_start = max(0, start - 1), indexer.py)
      codebase-memory-mcp   -> see LINE_BASES below; do NOT use loc(); emit as-is

Use ``loc()``/``span()`` for the 0-based backends. Every human, editor and terminal that consumes
our output counts from one, and agent hosts turn ``path:line`` into a clickable link. Each
renderer used to do its own conversion, which meant each renderer got to forget it independently
— and two of them did:

- ``lsp.py`` emitted serena's ``body_location`` and reference markers verbatim;
- ``semantic.py`` emitted ``chunk_start``, which is 0-based by construction in ``indexer.py``.

Both shipped answers pointing one line above the truth, and both produced ``path:0`` for anything
at the top of a file — a line number that does not exist, which is the tell that survived into the
evaluation transcript before anyone noticed it.

Centralising the conversion is what stops the next renderer from re-deciding this. If you need a
``path:line`` string, call ``loc()``; if you need the number alone, call ``line1()``.
"""

from __future__ import annotations

# The measured line base of each backend. The prose table above is the explanation; THIS is the
# enforced form (tests/test_loc_census.py drives every provider and checks the number it emits
# against this map). A new provider file with no entry here fails the census.
LINE_BASES: dict[str, int] = {"lsp": 0, "semantic": 0, "graph": 1}


def line1(line0: object) -> int | None:
    """Convert a backend's 0-based line number to a 1-based one.

    Returns None for anything that is not a usable line number, so callers render a bare path
    rather than inventing a position. Accepts str because some backends report line numbers as
    text inside a larger payload.
    """
    if line0 is None or isinstance(line0, bool):
        return None
    try:
        n = int(line0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    # A negative line is meaningless; clamp rather than emit `path:-1` or `path:0`.
    return n + 1 if n >= 0 else None


def loc(path: object, line0: object = None) -> str:
    """Render ``path:line`` with the line converted to 1-based, or a bare path when there is no
    usable line. ``line0`` is the backend's own 0-based number — never pre-increment it."""
    p = str(path or "").strip() or "?"
    n = line1(line0)
    return f"{p}:{n}" if n is not None else p


def span(path: object, start0: object, end0: object = None) -> str:
    """Render ``path:start-end`` (1-based) for a symbol body, or ``path:start``/``path`` when the
    end is absent or unusable."""
    p = str(path or "").strip() or "?"
    s = line1(start0)
    if s is None:
        return p
    e = line1(end0)
    return f"{p}:{s}-{e}" if e is not None else f"{p}:{s}"
