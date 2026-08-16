"""`codeintel reset` — clear the semantic index (recover from a corrupt/stale DB)."""

import sys
from typing import Any

from codeintel.commands._common import emit, never_raise, resolve_root


@never_raise("reset unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel import reset as _reset

    project_root = resolve_root(args)
    preview = _reset.run_reset(project_root, all_projects=args.all, apply=False)
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to reset without --yes in a non-interactive shell")
            return 1
        target = "ALL projects" if args.all else project_root
        count = preview.get("count", 0)
        ans = input(f"Reset semantic index for {target} ({count} entries)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted")
            return 0
    report = _reset.run_reset(project_root, all_projects=args.all, apply=True)
    emit(report, as_json=args.json, render=lambda r: r.get("detail", "reset complete"))
    return 0
