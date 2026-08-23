"""Project resolution for the graph provider.

The third slice extracted from `GraphProvider` under docs/refactor-graph-provider.md: turning a
`project_root` (a filesystem path) into the backend project that answers for it, distinguishing
"not indexed" from "could not ask" from "indexed under a containing project", and caching the
result. `ProjectResolver` is constructed with the `BackendClient` it resolves through (it calls
`list_projects`) and owns `_lookup_project`/`_resolve_project`/`_match_project`/`_project_root_of`
plus the caches (`_project_cache`/`_negative_until`/the lock) and the `ProjectResolution`/
`ProjectLookup` value types.

`GraphProvider` composes a `ProjectResolver` instance and exposes `_project_cache`/
`_negative_until`/`_project_cache_lock` as properties over it, for the same reason
`graph_backend.py` exposes `available`/`_cmd`/etc. over `BackendClient` — tests build a provider
with `GraphProvider.__new__(GraphProvider)` and set these directly. `_match_project`/
`_project_root_of` are pure (no backend call) and stay genuinely thin delegators to the matching
`ProjectResolver` method.

`_lookup_project` takes an injected `run` callable so ONE body serves both callers without a second
copy — the seam that makes this a real extraction rather than a duplicated method. A directly driven
resolver uses its own `self._backend._run` (the default); `GraphProvider._lookup_project` is a thin
delegator that passes its OWN `self._run` instead, because a large population of tests stub the
transport at `gp._run` (`monkeypatch.setattr(p, "_run", ...)`) and expect resolution to honour that
stub, which the resolver's own `self._backend._run` would silently bypass. It stays overridable on
the provider because 6 tests stub `gp._lookup_project` directly.

`GraphProvider._resolve_project` is implemented as `self._lookup_project(project_root).resolution`
— calling `GraphProvider`'s OWN overridable `_lookup_project` (see above), not
`self._resolver._resolve_project(...)` — because 6 tests stub `gp._lookup_project` directly and
`grapher.py`/`mapper.py` call `_resolve_project`; routing it through the resolver's own
`_resolve_project` (which calls the resolver's OWN `_lookup_project`) would silently bypass that
stub. `probe` stays on `GraphProvider` as the doctor facade, calling into this resolver and
`BackendClient` directly; op orchestration stays there too.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from codeintel.graph_backend import BackendClient

# The `list_projects` fetch, injected into `_lookup_project` so ONE body serves both a directly
# driven resolver (its own `self._backend._run`) and a `GraphProvider` (which passes its own
# overridable `_run` delegator, the seam a large population of tests stub). Parameterising the fetch
# is what lets the provider keep a thin delegator instead of a second copy of the lookup body.
RunFn = Callable[[str, dict, int], Any]

# How long `list_projects` may take before resolution gives up. The old value was 3000ms, chosen
# against a mocked backend; the real one spawns a native binary that re-initialises its allocator
# per invocation and measured ~5.8s consistently on an ordinary machine. Every graph query resolves
# a project first, so a budget below the backend's real latency does not degrade the engine — it
# disables it. Generous on purpose: a successful lookup is cached for the process's lifetime, so
# this is paid approximately once, and being slow is enormously better than being silently wrong.
# Overridable for a machine slower still, or for a test that wants to force the timeout path.
try:
    _RESOLVE_TIMEOUT_MS = max(1, int(os.environ.get("CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS", "20000")))
except ValueError:
    _RESOLVE_TIMEOUT_MS = 20000


def _int_or_zero(value: Any) -> int:
    """A node count from an untrusted backend payload, or 0 when it is missing/not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _same_dir(a: str, b: str) -> bool:
    """Whether two paths name the same directory.

    `os.path.realpath` resolves symlinks but does not canonicalise CASE, and macOS APFS is
    case-insensitive: `/Users/x/Project/repo` and `/Users/x/project/repo` are one directory that
    compared as two, so a correctly-indexed repo was reported unindexed. `os.path.samefile` asks
    the filesystem, which is the only authority on this; fall back to a realpath compare when
    either path does not exist."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a).rstrip(os.sep) == os.path.realpath(b).rstrip(os.sep)


@dataclass(frozen=True)
class ProjectLookup:
    """A resolution attempt AND why it failed, which the caller must tell apart.

    "The backend did not answer" and "this repository is not indexed" are different facts with
    different remedies, and collapsing them is what turned a timeout into `project-not-indexed`
    plus an instruction to re-index a repository that was already indexed."""

    resolution: ProjectResolution | None
    reason: str  # "ok" | "not-indexed" | "backend-unreachable"


@dataclass(frozen=True)
class ProjectResolution:
    """How a project_root resolved to a backend project, INCLUDING whether the match was exact.

    `_resolve_project` used to return the project name alone, discarding the one fact that decides
    whether an answer is about the repository the caller asked about. `probe()` recovered it by
    re-deriving the matched root a second time; `build_result` did not, so `doctor` warned about an
    ancestor match while `code.query` answered from it silently — a human running the diagnostic
    was told, and the agent actually consuming the answers was not. Both now consume this record,
    so the two cannot drift apart again."""

    name: str
    matched_root: str | None
    scope: str  # "exact" | "ancestor"

    @property
    def is_ancestor(self) -> bool:
        return self.scope == "ancestor"


class ProjectResolver:
    """Resolves a `project_root` to a backend project, and caches the result. Never raises."""

    # Annotation only, no default: a `ProjectResolver.__new__(ProjectResolver)` instance built by a
    # test that stubs `GraphProvider._lookup_project`/`_resolve_project` directly never dereferences
    # this, and there is no safe stand-in backend to hand it if it did. Likewise `_project_cache`/
    # `_negative_until` — a `dict` default at class level would be a MUTABLE object shared across
    # every bare instance, which is the one thing a per-provider cache must never be; every real
    # `__new__` test site sets these two directly (never reads them first), so there is no bare
    # state to default. `_project_cache_lock` has no such hazard — a `threading.Lock` is inert until
    # acquired — so it gets the same class-level default `BackendClient` gives `_wire_format_lock`.
    _backend: BackendClient
    _project_cache: dict[str, ProjectResolution]
    _negative_until: dict[str, float]
    _project_cache_lock = threading.Lock()  # concurrent HTTP requests share one provider

    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend
        self._project_cache: dict[str, ProjectResolution] = {}   # resolved projects (stable, kept)
        self._negative_until: dict[str, float] = {}                 # failed lookups, short TTL only
        self._project_cache_lock = threading.Lock()  # concurrent HTTP requests share one provider

    @staticmethod
    def _match_project(raw: Any, project_root: str) -> ProjectResolution | None:
        """Resolve a list_projects response to the project for ``project_root``.

        The real codebase-memory-mcp returns ``{"projects": [...]}``; a bare list is the
        older/mocked shape — accept both. Prefer an exact ``root_path`` match; otherwise the
        LONGEST prefix match (so ``.../project/codeintel`` resolves to codeintel, not its
        parent ``.../project``). Static so ``_resolve_project`` and ``probe`` share it.

        The backend can hold MORE THAN ONE project for the same root — typically one registered
        under a short name and one under a path slug — and the two drift apart independently.
        Returning the first match meant a query could be answered from a months-stale index while
        a complete one sat beside it: observed on this repo as 1475 nodes vs 2631 for the same
        path, which is how `callers` reported a function's pre-refactor shape hours after the
        refactor. Among exact matches, prefer the most complete index."""
        # Normalize the input to an absolute realpath: the backend stores absolute root_paths, so a
        # relative ``project_root`` (e.g. `codeintel map .` passing ".") would otherwise never match
        # — the bug where the map/query silently reported "not indexed" from inside the repo.
        try:
            project_root = os.path.realpath(project_root)
        except Exception:
            pass
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return None
        exact: list[dict] = []
        best_prefix_len = -1
        best_prefix: dict | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rp = entry.get("root_path", "")
            if not rp:
                continue
            if rp == project_root:
                exact.append(entry)
                continue
            if project_root.startswith(rp.rstrip("/") + "/") and len(rp) > best_prefix_len:
                best_prefix_len = len(rp)
                best_prefix = entry

        if exact:
            # Node count is the available completeness signal — list_projects carries no indexed-at
            # timestamp, and head_sha is recorded per registration rather than per index pass, so
            # duplicates routinely report the same SHA with wildly different graphs. `max` keeps
            # the FIRST maximal entry, so with no completeness signal to go on (ties, or a backend
            # that omits `nodes`) this falls back to the original first-listed rule rather than
            # inventing an ordering.
            best = max(exact, key=lambda e: _int_or_zero(e.get("nodes")))
            name = best.get("name")
            if not name:
                return None
            return ProjectResolution(
                name=str(name), matched_root=str(best.get("root_path") or ""), scope="exact",
            )
        if best_prefix is not None:
            name = best_prefix.get("name")
            if name:
                # A prefix hit is only "ancestor" if it is genuinely a DIFFERENT directory. An
                # exact match that differed by case or a symlink lands here on the filesystems
                # where realpath cannot canonicalise it, and calling that an ancestor would refuse
                # scan ops on a correctly-indexed repo.
                root_path = str(best_prefix.get("root_path") or "")
                scope = "exact" if _same_dir(root_path, project_root) else "ancestor"
                return ProjectResolution(name=str(name), matched_root=root_path, scope=scope)
        return None

    def _lookup_project(self, project_root: str, run: RunFn | None = None) -> ProjectLookup:
        """Resolve a root to a backend project, distinguishing "not indexed" from "could not ask".

        `run` is the callable that fetches `list_projects` — defaulting to this resolver's own
        backend, but overridable so ONE body serves both callers without a second copy. A
        `GraphProvider` passes its OWN `self._run` delegator, because a large population of tests
        stub the transport at `gp._run` (`monkeypatch.setattr(p, "_run", ...)`) and expect
        resolution to honour that stub; the resolver's `self._backend._run` would bypass it. That
        seam is why this is parameterised rather than the provider keeping a duplicate body.

        The timeout here used to be a hardcoded 3000ms, and the real backend takes appreciably
        longer than that: `list_projects` spawns a native binary that initialises its own allocator
        on every invocation, measured at ~5.8s CONSISTENTLY — not just on a cold start. The effect
        was total. Every graph query timed out during resolution, `_match_project` was handed
        `None`, and the caller reported `project-not-indexed` with the advice to run
        `codeintel index` — which cannot help, because the repository was already indexed. The
        entire graph engine was dead on any machine where the backend is this slow, and it said so
        in the one way guaranteed to send the user somewhere useless.

        Nothing caught it because the backend is installed in no CI job, and the one live test that
        would have failed instead SKIPPED — with "project not indexed in this environment", a
        condition produced by this very bug.
        """
        if run is None:
            run = self._backend._run
        with self._project_cache_lock:
            if project_root in self._project_cache:
                return ProjectLookup(self._project_cache[project_root], "ok")
            neg_until = self._negative_until.get(project_root)
            if neg_until is not None and time.monotonic() < neg_until:
                return ProjectLookup(None, "not-indexed")  # recent genuine miss, within its TTL
        # list_projects shells out — resolve it OUTSIDE the lock so a slow backend can't serialize
        # every concurrent request. A rare duplicate lookup on first contact is harmless.
        raw = run("list_projects", {}, _RESOLVE_TIMEOUT_MS)
        if raw is None:
            # `_run` returns None on timeout/crash. That is NOT evidence the project is unindexed,
            # and it must not be cached as a miss: a backend having a slow moment would then be
            # remembered as "this repo has no index" for the next 30 seconds.
            return ProjectLookup(None, "backend-unreachable")
        resolution = self._match_project(raw, project_root)
        with self._project_cache_lock:
            if resolution is not None:
                self._project_cache[project_root] = resolution
                self._negative_until.pop(project_root, None)
            else:
                # Cache the MISS only briefly, so a repo indexed into the graph AFTER this failed
                # lookup is picked up within the TTL rather than staying stuck until a restart.
                self._negative_until[project_root] = time.monotonic() + 30.0
        return ProjectLookup(resolution, "ok" if resolution is not None else "not-indexed")

    def _resolve_project(self, project_root: str, run: RunFn | None = None) -> ProjectResolution | None:
        """The resolution alone, for callers that only branch on found/not-found."""
        return self._lookup_project(project_root, run).resolution

    @staticmethod
    def _project_root_of(raw: Any, name: str | None) -> str | None:
        """The ``root_path`` a list_projects response records for *name*."""
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list) or not name:
            return None
        # Among entries sharing a name, take the one `_match_project` would have chosen — the most
        # complete. Returning the first produced a false "not indexed on its own" warning on a
        # correctly-indexed repo whenever a stale duplicate registration existed.
        matches = [e for e in entries
                   if isinstance(e, dict) and e.get("name") == name and e.get("root_path")]
        if not matches:
            return None
        return str(max(matches, key=lambda e: _int_or_zero(e.get("nodes"))).get("root_path"))
