"""`codeintel status` — engine readiness and index age at a glance."""

import datetime
import os
from typing import Any

from codeintel.commands._common import never_raise, resolve_root

# "available" alone was the misleading word: it meant "a binary is on PATH", which is not the same
# as runnable, and not the same as usable on THIS repo. Say which.
_READY = {"ok": "ready", "warn": "installed (not verified)", "fail": "unavailable"}


@never_raise("Status unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel import server

    project_root = resolve_root(args)
    status = server.code_status_handler({"project_root": project_root})

    readiness = status.get("readiness") or {}
    print("Engine status:")
    for engine in ["graph", "lsp", "semantic"]:
        entry = readiness.get(engine) or {}
        state = _READY.get(str(entry.get("status") or ""), "unavailable")
        detail = entry.get("detail") or ""
        print(f"  {engine:<10} {state:<26} {detail}")
    if status.get("healthy") is False:
        print("\n  run `codeintel doctor` for the fix for each gap")

    from codeintel.config import load_config
    from codeintel.semantic_db import default_db_path

    db_path = default_db_path(str(load_config(project_root).get("model") or ""))
    if os.path.exists(db_path):
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(os.path.getmtime(db_path))
        hours = int(age.total_seconds() // 3600)
        minutes = int((age.total_seconds() % 3600) // 60)
        print(f"\nIndex age: {hours}h {minutes}m  ({db_path})")
    else:
        print(f"\nIndex: not found  ({db_path})")
    return 0
