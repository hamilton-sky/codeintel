"""`codeintel status` — engine readiness and index age at a glance."""

import datetime
import json
import os
from typing import Any

from codeintel.cli._common import never_raise, require_dir, resolve_root

# "available" alone was the misleading word: it meant "a binary is on PATH", which is not the same
# as runnable, and not the same as usable on THIS repo. Say which.
_READY = {"ok": "ready", "warn": "installed (not verified)", "fail": "unavailable"}


@never_raise("Status unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel.transports import mcp as server

    project_root = resolve_root(args)
    problem = require_dir(project_root, "status")
    if problem:
        print(problem)
        return 1
    status = server.code_status_handler({
        "project_root": project_root,
        "deep": bool(getattr(args, "deep", False)),
    })

    from codeintel.core.config import load_config
    from codeintel.engines.semantic.db import SemanticDb, default_db_path

    db_path = default_db_path(str(load_config(project_root).get("model") or ""))
    indexed_at = None
    semantic_readiness = (status.get("readiness") or {}).get("semantic") or {}
    # Metadata can outlive every chunk after a failed/partial cleanup. An age attached to zero
    # searchable rows makes an unindexed repo look recently indexed, so only surface it when the
    # engine independently confirmed this repository has chunks.
    if os.path.exists(db_path) and semantic_readiness.get("repo_indexed") is True:
        db = SemanticDb(db_path)
        try:
            indexed_at = db.indexed_at(os.path.realpath(project_root))
        finally:
            db.close()

    if getattr(args, "json", False):
        payload = {
            **status,
            "project_root": project_root,
            "semantic_index": {
                "path": db_path,
                "exists": os.path.exists(db_path),
                "indexed_at": indexed_at,
                "age_seconds": (
                    max(0, int(datetime.datetime.now().timestamp() - indexed_at))
                    if indexed_at is not None else None
                ),
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    readiness = status.get("readiness") or {}
    print("Engine status:")
    for engine in ["graph", "lsp", "semantic"]:
        entry = readiness.get(engine) or {}
        state = _READY.get(str(entry.get("status") or ""), "unavailable")
        detail = entry.get("detail") or ""
        print(f"  {engine:<10} {state:<26} {detail}")
    # `healthy` ignores the optional graph engine, so a repo whose graph backend is installed and
    # refusing to run prints a green verdict above a row that says otherwise. Said in words here,
    # because the table is read as a verdict and this is the line that stops "healthy" reading as
    # "everything works".
    degraded = [str(name) for name in (status.get("degraded") or [])]
    if degraded:
        lead = "healthy, but " if status.get("healthy") else ""
        print(f"\n  {lead}{', '.join(degraded)} degraded (installed, but not working for this repo)")
    if status.get("healthy") is False:
        print("\n  run `codeintel doctor` for the fix for each gap")
    elif degraded:
        print("\n  run `codeintel doctor` for the reason and the fix")

    # Printed before anything else the user might act on. A skew means every line above describes
    # the code this process loaded, not the code installed — so a fix the user can read in the
    # CHANGELOG can be absent from every answer while the engines all report green.
    skew = status.get("version_skew")
    if isinstance(skew, dict) and skew.get("running") and skew.get("installed"):
        print(
            f"\n  ! serving {skew['running']}, but {skew['installed']} is installed"
            "\n    restart the codeintel server (or the agent hosting it) to pick it up"
        )

    _print_graph_index(status.get("graph_index"), project_root)

    if not os.path.exists(db_path):
        print(f"\nSemantic index: not found  ({db_path})")
        return 0

    # The age of THIS project, not of the database file. Every project shares one per-model file,
    # so its mtime advances whenever any OTHER repository is indexed — which made a months-stale
    # index report as freshly built, and did so most convincingly when someone was checking it
    # precisely because an answer looked wrong.
    if indexed_at is None:
        # Either never indexed, or indexed before this was recorded. "Unknown" is the honest
        # answer; the previous number was worse than none because it looked authoritative.
        # When the engine itself cannot work (no model, a download refused), re-indexing does not
        # help, and its row above already names the fix.
        fix = ("see the semantic row above, or `codeintel doctor`"
               if semantic_readiness.get("remediation")
               else f"run `codeintel index {project_root}`")
        print(f"\nSemantic index age: unknown for this repo — {fix}  ({db_path})")
    else:
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(indexed_at)
        print(f"\nSemantic index age: {_hm(age.total_seconds())}  ({db_path})")
    return 0


def _hm(seconds: float) -> str:
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def _print_graph_index(block: Any, project_root: str) -> None:
    """The graph index's age and whether the working tree moved past it. The graph is what
    `callers`, `impact` and `changed` answer from, so its age is the one that decides whether
    those answers are current; the semantic line below is only about `search`."""
    if not isinstance(block, dict):
        return
    seconds = block.get("age_seconds")
    age = f"{_hm(float(seconds))} old" if isinstance(seconds, (int, float)) else "age unknown"
    state = block.get("state")
    detail = str(block.get("detail") or "")
    if state == "fresh":
        print(f"\nGraph index: {age}, current with the working tree")
    elif state == "stale":
        print(f"\nGraph index: {age}, STALE — {detail}"
              f"\n  callers/impact/changed answers miss what changed since; "
              f"run `codeintel index {project_root}`")
    else:
        print(f"\nGraph index: {age}, freshness unknown — {detail or 'could not be checked'}")
