"""What the caller's `target` string denotes, and whether a row is the symbol they meant.

Phase 1 of `docs/refactor-graph-provider.md` lists `_SymbolTarget` under "pure text / model" with
the rest of the renderer's furniture. It gets its own module instead, because the brief that
prompted the rest of the split names target resolution as one of the five concerns
`providers/graph.py` had accumulated, and folding it into `graph_render.py` would have answered
"where does this live" with "with the other things that had nowhere else to go".

A repository with three functions called `handle` answers for all three at once unless the caller
can say which. The graph already tells them apart — every row carries `qualified_name` and
`file_path` — so the disambiguator is text the caller already holds, and this is where that text
becomes a predicate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from codeintel.graph_render import _FILE_EXTENSIONS, _strip_project_prefix


@dataclass(frozen=True)
class _SymbolTarget:
    """A ``target`` that may say WHICH symbol it means.

    The symbol-edge ops key on the unqualified name, so a repository with three functions called
    `handle` answers for all three at once and the caller has no way to ask for one. The graph
    already tells them apart — every row carries `qualified_name` and `file_path` — and both are
    printed on the result lines the caller has just read, so the disambiguator is text they already
    hold:

    * ``handle``                     every symbol named `handle`
    * ``api.routes.handle``          the one whose qualified name ends in those segments
    * ``handle@src/api/routes.py``   the one defined in that file

    Applied to rows rather than pushed into the Cypher ``WHERE``: a suffix match needs a string
    predicate, and which of those this backend supports is not something this project can pin — the
    0.9→0.10 wire-format break is the standing reminder that its dialect is not a stable interface.
    Narrowing rows already in hand costs one pass and cannot be broken by a backend release.
    """

    name: str
    qualified: str = ""
    file_hint: str = ""

    @property
    def narrowed(self) -> bool:
        """Whether the caller asked for one specific symbol rather than every symbol by that name."""
        return bool(self.qualified or self.file_hint)

    def describe(self) -> str:
        parts = []
        if self.qualified:
            parts.append(f"qualified name `{self.qualified}`")
        if self.file_hint:
            parts.append(f"file `{self.file_hint}`")
        return " in ".join(parts) or f"`{self.name}`"

    def matches(self, qualified_name: Any, file_path: Any) -> bool:
        """Whether the symbol at *qualified_name* / *file_path* is the one this target names."""
        if self.qualified and not _qualified_name_matches(qualified_name, self.qualified):
            return False
        return not (self.file_hint and not _file_path_matches(file_path, self.file_hint))


def _parse_symbol_target(target: Any) -> _SymbolTarget:
    """Split a ``target`` into a symbol name and whatever disambiguator it carries."""
    raw = str(target or "").strip()
    file_hint = ""
    if "@" in raw:
        head, _, tail = raw.rpartition("@")
        # A leading `@` is a decorator (`@app.route`), not a file hint, and `handle@` names no file.
        if head.strip() and tail.strip():
            raw, file_hint = head.strip(), tail.strip()
    qualified = ""
    if "." in raw:
        head, _, last = raw.rpartition(".")
        # `use-toast.ts` is a FILENAME, not a qualified name. Reading `ts` as the symbol name would
        # send a query for something nobody asked about — the same trap `_strip_project_prefix`
        # guards against, so the same derived extension set answers it.
        if head and last and last.lower() not in _FILE_EXTENSIONS:
            qualified, raw = raw, last
    return _SymbolTarget(name=raw, qualified=qualified, file_hint=file_hint)


def _qualified_name_matches(qualified_name: Any, wanted: str) -> bool:
    """Whether *qualified_name* ends with the dotted segments *wanted*.

    Segment-aligned, so `routes.handle` does not match `api.myroutes.handle`. Compared against both
    the raw name and its `_strip_project_prefix` form, because the caller will have copied the
    stripped one off a previous result line while the backend still stores the prefixed one."""
    raw = str(qualified_name or "")
    if not raw or not wanted:
        return False
    return any(have == wanted or have.endswith("." + wanted)
               for have in (raw, _strip_project_prefix(raw, may_be_filename=False)))


def _file_path_matches(file_path: Any, hint: str) -> bool:
    """Whether *file_path* is the file the caller named.

    A path-segment suffix match: `routes.py`, `api/routes.py` and the full repo-relative path all
    identify `src/api/routes.py` — all three are things a caller reasonably types, and the last is
    what the result lines print. A hint that stays ambiguous is not a problem to be solved here: two
    files can match, and the answer then says so rather than picking one silently."""
    have = str(file_path or "").replace("\\", "/").strip().lower()
    want = str(hint or "").replace("\\", "/").strip().strip("/").lower()
    if not have or not want:
        return False
    return have == want or have.endswith("/" + want)


def _same_path(a: str | None, b: str | None) -> bool:
    """Whether two paths denote the same directory, after realpath.

    Needed because a registry can hold `/tmp/x` while the caller asks about `/private/tmp/x` (macOS
    symlinks every `/tmp`), and a string comparison there reports a mismatch that does not exist."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except Exception:
        return str(a) == str(b)


def _has_own_git_dir(path: str) -> bool:
    """Whether *path* is the root of its own git repository.

    This is what separates the two cases an ancestor match conflates. A subdirectory of a monorepo
    has no `.git` of its own, and answering it from the monorepo's index is correct. A repository
    that merely happens to sit inside an indexed directory does have one, and answering it from the
    parent is the bug. A worktree or submodule records `.git` as a FILE rather than a directory, so
    test for existence, not `is_dir()`."""
    try:
        return os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False
