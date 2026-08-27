"""`codeintel c4` — a LikeC4 architecture model (`.c4`) built from this repo's import graph."""

import json
import os
from typing import Any

from codeintel.commands._common import never_raise, require_dir, resolve_root

_REASON_FIX = {
    "engine-unavailable": "run `codeintel doctor` to check the graph backend",
    # Reached only under --no-index; the default path indexes the repo instead of printing this.
    "project-not-indexed": "drop --no-index to let c4 index this repo, or run `codeintel index`",
    "project-not-indexed-standalone": ("this repo is nested inside an already-indexed project — "
                                       "drop --no-index, or run `codeintel index` on it directly"),
    "no-source-files": "run `codeintel doctor` to check what got indexed",
    "scope-not-found": "drop --scope, or point it at a directory that actually exists",
    "error": "run `codeintel doctor` to check engine health",
}


def _other_c4_files_elsewhere(project_root: str, out_dir: str) -> list[str]:
    """`.c4` files under *project_root* but outside *out_dir* — LikeC4 merges every `.c4` it finds
    under wherever it is started, not just the one directory this command writes to. Best-effort:
    never lets a filesystem walk fail the command that just successfully wrote a model."""
    try:
        out_abs = os.path.realpath(out_dir)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
            if os.path.realpath(dirpath) == out_abs:
                dirnames[:] = []
                continue
            for name in filenames:
                if name.endswith(".c4"):
                    full = os.path.join(dirpath, name)
                    found.append(os.path.relpath(full, project_root))
            if len(found) >= 20:
                break
        return found
    except Exception:
        return []


# code=1: this command's job is to WRITE A FILE. Exiting 0 after failing to write it reports
# success to any CI step gating on $? while nothing was produced — same reasoning as graph.py.
@never_raise("c4 failed: {exc}", code=1)
def run(args: Any) -> int:
    from codeintel import c4

    project_root = resolve_root(args)
    problem = require_dir(project_root, "c4")
    if problem:
        print(problem)
        return 1

    def build() -> dict:
        return c4.build_c4_payload(
            project_root,
            depth=getattr(args, "depth", None),
            scope=tuple(getattr(args, "scope", None) or ()),
            include_tests=bool(getattr(args, "include_tests", False)),
        )

    payload = build()

    # One command, one artifact: an un-indexed repo is a prerequisite this command can satisfy
    # itself, so it does — rather than printing "run `codeintel index`" and exiting 1, which makes
    # the user run two commands to get one file. Announced BEFORE it starts (it is the slow part,
    # and silence here reads as a hang) and retried exactly once, so a repo the backend cannot
    # index reports that instead of looping. `--no-index` restores the strict behaviour for CI,
    # where "the index was missing" should fail the step rather than be quietly repaired.
    reason = payload.get("reason") or ""
    if reason in c4.INDEXABLE_REASONS and not getattr(args, "no_index", False):
        print(f"{project_root} has no graph index yet — indexing it now "
              f"(graph only; a minute or two on a large repo)")
        outcome = c4.index_repo(project_root)
        if not outcome.get("ok"):
            print(f"c4 failed: could not index this repo — {outcome.get('problem')}")
            print("  run `codeintel doctor` to check the graph backend")
            return 1
        print("  ✓ indexed")
        payload = build()

    # After the auto-index, so `--json` reports the model this repo actually has rather than the
    # not-indexed envelope it had a moment ago.
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    reason = payload.get("reason") or ""
    if reason:
        fix = _REASON_FIX.get(reason, "run `codeintel doctor`")
        print(f"c4 failed: {reason} — {fix}")
        return 1

    dsl = c4.render_c4_dsl(payload)
    out_dir = getattr(args, "out", None) or "codeintel-c4"
    result = c4.write_model(payload, dsl, out_dir)
    if not result.get("ok"):
        print(f"c4 failed: {result.get('problem')}")
        return 1

    fit = payload.get("fit") or {}
    elements = payload.get("elements") or []
    relations = payload.get("relations") or []
    table = fit.get("table") or {}
    depth, how, cap = fit.get("depth"), fit.get("how"), fit.get("cap")
    over_cap = bool(fit.get("over_cap"))

    table_str = ", ".join(f"d{d}={table[d]}" for d in sorted(table))
    if how == "requested":
        how_desc = f"requested depth {depth}: {table_str}"
    else:
        how_desc = f"auto-fit: {table_str}"
        if over_cap:
            how_desc += f" > {cap} cap"

    out_path = result.get("path", out_dir)
    print(f"Wrote {out_path}/{c4.MODEL_FILENAME} — {len(elements)} elements at depth {depth} "
          f"({how_desc}), {len(relations)} relations")
    print(f"npx likec4 start {out_path}")

    if over_cap:
        print(f"WARNING: {len(elements)} elements exceeds the {cap}-element view cap; "
              f"the index view will be slow")

    other = _other_c4_files_elsewhere(project_root, out_path)
    if other:
        print(f"NOTE: found {len(other)} other .c4 file(s) elsewhere in this repo "
              f"({', '.join(other[:5])}{', …' if len(other) > 5 else ''}). LikeC4 merges every "
              f"`.c4` it discovers, so root it at this model: npx likec4 start {out_path}")
    return 0
