"""Where codeintel keeps its per-machine state — one definition, used by every consumer.

``CODEINTEL_HOME`` exists because ``Path.home()`` raises when a process has no resolvable home
directory: a UID with no passwd entry and no ``$HOME``. That is not exotic for this tool. It is an
MCP server whose whole purpose is to be launched by coding agents, which are routinely run in
containers shaped exactly that way.

The override used to be honoured by the semantic cache alone. ``config.py`` and ``auth.py`` each
called ``Path.home()`` directly, so on such a host ``load_config()`` raised ``RuntimeError`` — and
kept raising with ``CODEINTEL_HOME`` set, because the override never reached them. The escape hatch
did not work in the one environment it was written for, and the failure surfaced several layers
away as a generic ``provider-error``. Anything resolving this directory now asks here.
"""
from __future__ import annotations

import os
import pathlib
import re


def codeintel_home() -> pathlib.Path:
    """The per-machine state directory (cache, global config, auth).

    Raises ``RuntimeError`` (from ``Path.home()``) when there is no override and no resolvable home
    — deliberately, so a caller that must NAME the problem can. ``SemanticProvider.probe`` reports
    it with the remediation that actually fixes it; callers for whom a missing file is merely
    "no config" should catch it instead (see ``config.global_config_path``).
    """
    override = os.environ.get("CODEINTEL_HOME", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".codeintel"


def graph_cache_dir() -> str:
    """Where codebase-memory-mcp keeps one sqlite file per indexed project.

    codeintel does not own this directory, which is exactly why `reset --all` never touched it —
    and why "start from a clean index" was not true for the graph engine. Honouring the backend's
    own override so a relocated cache is still cleared."""
    override = os.environ.get("CODEBASE_MEMORY_HOME") or os.environ.get("CODEBASE_MEMORY_CACHE_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".cache", "codebase-memory-mcp")


def graph_slug(canonical_root: str) -> str:
    """The stem the graph backend gives a project's cache file: every run of non-alphanumeric
    characters collapses to a single ``-``, with the ends trimmed. So ``/Users/me/aws-resource-tools``
    → ``Users-me-aws-resource-tools`` and ``/tmp/-Weird/repo`` → ``tmp-Weird-repo`` (NOT
    ``tmp--Weird-repo`` — a naive ``/``→``-`` replace leaves a double dash and misses the file,
    silently leaking the graph index a scoped reset was meant to remove). Verified against real
    on-disk caches. The collapse also flattens away every path separator, so the target can never
    escape the cache directory."""
    return re.sub(r"[^A-Za-z0-9]+", "-", canonical_root).strip("-")


def _db_stamps_ns(db_path: str) -> list[int]:
    """The ``st_mtime_ns`` of a graph db and its ``-wal`` (a write lands in the WAL first)."""
    stamps = []
    for path in (db_path, db_path + "-wal"):
        try:
            stamps.append(os.stat(path).st_mtime_ns)
        except FileNotFoundError:
            continue
    return stamps


def graph_db_stamp(project_root: str) -> int:
    """When the graph index for *project_root* was last written, as nanoseconds; 0 when unknown.

    Reads ``<graph_cache_dir()>/<graph_slug(realpath root)>.db`` and its ``-wal``. A root that has
    no db of its own may be answered from a project that contains it, so the newest ``*.db`` or
    ``*.db-wal`` in the directory stands in: a write to any index then still moves the stamp. 0
    when the directory is missing."""
    directory = graph_cache_dir()
    if not os.path.isdir(directory):
        return 0
    own = _db_stamps_ns(os.path.join(directory, graph_slug(os.path.realpath(str(project_root))) + ".db"))
    if own:
        return max(own)
    newest = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith((".db", ".db-wal")):
                continue
            try:
                newest = max(newest, entry.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
    return newest


def graph_db_mtime(project: str) -> float | None:
    """The mtime of the graph db for a RESOLVED project id (its WAL included), or ``None`` when
    there is no such file."""
    stamps = _db_stamps_ns(os.path.join(graph_cache_dir(), f"{project}.db"))
    return max(stamps) / 1e9 if stamps else None
