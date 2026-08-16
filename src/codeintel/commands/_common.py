"""Shared plumbing for the CLI command handlers.

Every command is `run(args) -> int` — it *returns* its exit code rather than calling `sys.exit`,
so a test can call it directly and assert on the code instead of driving argv and catching
SystemExit. `__main__` is the only place that turns the code into an exit.
"""

import functools
import json
import os
import sys
from collections.abc import Callable
from typing import Any


def resolve_root(args: Any) -> str:
    """The project root a command operates on: the positional argument, else the cwd."""
    return getattr(args, "project_root", None) or os.getcwd()


def require_dir(project_root: str, command: str) -> str | None:
    """An error line when *project_root* is not a directory, else None.

    A mistyped path used to produce confident, well-formed output about a directory that does not
    exist — `setup /typo` rendered a full three-engine health table for it — which is the worst
    possible response to a typo in a script, because it is indistinguishable from success."""
    if os.path.isdir(project_root):
        return None
    return f"{command} failed: not a directory: {project_root}"


def emit(report: dict, *, as_json: bool, render: Callable[[dict], str]) -> None:
    """Print a report as structured JSON (--json) or as its human-facing text rendering."""
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))


def never_raise(template: str, *, code: int = 0, stderr: bool = False):
    """Degrade with a message instead of a traceback — the CLI's parity with the never-raise MCP
    handlers. `template` is formatted with `{exc}`.

    Only `Exception` is caught. `SystemExit` and `KeyboardInterrupt` are BaseException and still
    pass through, which is what lets serve-http's non-loopback guard exit on its own terms and
    lets Ctrl-C stay a Ctrl-C.
    """
    def decorate(fn: Callable[[Any], int]) -> Callable[[Any], int]:
        @functools.wraps(fn)
        def wrapper(args: Any) -> int:
            try:
                return fn(args)
            except Exception as exc:
                print(template.format(exc=exc), file=sys.stderr if stderr else sys.stdout)
                return code
        return wrapper
    return decorate
