"""Where the codebase-memory-mcp binary is, when it is not on PATH.

PATH was the only place codeintel looked, and PATH is the least reliable thing a process inherits.
`pip install codebase-memory-mcp` into the same environment as codeintel puts the backend next to
`codeintel` in that environment's `bin/`, and an agent host, a subagent shell or a GUI-launched MCP
server routinely runs `.venv/bin/codeintel` with a PATH that does not contain that folder. Every
graph answer then said "engine unavailable" with the binary one directory listing away — measured
in every subagent shell of the 0.30.0 build (`.venv/bin/codeintel` and `c4 --check` both).

The search, in order, first hit wins:

1. ``CODEINTEL_GRAPH_BACKEND`` — an explicit path, for an install nothing else can find. A value
   that is not an executable file is ignored rather than trusted, so a stale setting falls back to
   the normal search instead of disabling the engine.
2. PATH (`shutil.which`), as before — what the user launches by hand is what codeintel uses.
3. The folder of the running interpreter (`sys.executable`) and this environment's scripts folder
   (`sysconfig`), which differ under some venv layouts — i.e. codeintel's own `bin/`.

Only executable regular files count. Nothing here launches anything.
"""
from __future__ import annotations

import os
import shutil
import sys
import sysconfig

BACKEND_NAME = "codebase-memory-mcp"
BACKEND_ENV = "CODEINTEL_GRAPH_BACKEND"


def _executable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _candidate_names() -> tuple[str, ...]:
    if os.name == "nt":
        return (f"{BACKEND_NAME}.exe", f"{BACKEND_NAME}.cmd", BACKEND_NAME)
    return (BACKEND_NAME,)


def own_bin_dirs() -> list[str]:
    """codeintel's own `bin/` folders: the interpreter's directory and the scripts directory.

    A function of its own so the test suite can switch it off: a developer venv that has the
    backend installed would otherwise make every "backend absent" test (they stub PATH lookup)
    find it anyway. See `tests/conftest.py::_no_backend_beside_interpreter`."""
    dirs: list[str] = []
    for d in (os.path.dirname(sys.executable or ""), sysconfig.get_path("scripts") or ""):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def find_backend() -> str | None:
    """Absolute path of the backend binary, or None. Never raises."""
    try:
        explicit = os.environ.get(BACKEND_ENV, "").strip()
        if explicit:
            explicit = os.path.abspath(os.path.expanduser(explicit))
            if _executable_file(explicit):
                return explicit
        found = shutil.which(BACKEND_NAME)
        if found:
            return found
        for d in own_bin_dirs():
            for name in _candidate_names():
                path = os.path.join(d, name)
                if _executable_file(path):
                    return path
    except Exception:
        return None
    return None
