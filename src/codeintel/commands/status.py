"""`codeintel status` — engine readiness and index age at a glance."""

import datetime
import os
from typing import Any

from codeintel.commands._common import never_raise, require_dir, resolve_root

# "available" alone was the misleading word: it meant "a binary is on PATH", which is not the same
# as runnable, and not the same as usable on THIS repo. Say which.
_READY = {"ok": "ready", "warn": "installed (not verified)", "fail": "unavailable"}


@never_raise("Status unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel import server

    project_root = resolve_root(args)
    problem = require_dir(project_root, "status")
    if problem:
        print(problem)
        return 1
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

    # Printed before anything else the user might act on. A skew means every line above describes
    # the code this process loaded, not the code installed — so a fix the user can read in the
    # CHANGELOG can be absent from every answer while the engines all report green.
    skew = status.get("version_skew")
    if isinstance(skew, dict) and skew.get("running") and skew.get("installed"):
        print(
            f"\n  ! serving {skew['running']}, but {skew['installed']} is installed"
            "\n    restart the codeintel server (or the agent hosting it) to pick it up"
        )

    from codeintel.config import load_config
    from codeintel.semantic_db import default_db_path

    db_path = default_db_path(str(load_config(project_root).get("model") or ""))
    if not os.path.exists(db_path):
        print(f"\nIndex: not found  ({db_path})")
        return 0

    # The age of THIS project, not of the database file. Every project shares one per-model file,
    # so its mtime advances whenever any OTHER repository is indexed — which made a months-stale
    # index report as freshly built, and did so most convincingly when someone was checking it
    # precisely because an answer looked wrong.
    from codeintel.semantic_db import SemanticDb

    indexed_at = None
    db = SemanticDb(db_path)
    try:
        indexed_at = db.indexed_at(os.path.realpath(project_root))
    finally:
        db.close()

    if indexed_at is None:
        # Either never indexed, or indexed before this was recorded. "Unknown" is the honest
        # answer; the previous number was worse than none because it looked authoritative.
        print(f"\nIndex age: unknown for this repo — run `codeintel index {project_root}`"
              f"  ({db_path})")
    else:
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(indexed_at)
        hours = int(age.total_seconds() // 3600)
        minutes = int((age.total_seconds() % 3600) // 60)
        print(f"\nIndex age: {hours}h {minutes}m  ({db_path})")
    return 0
