"""`codeintel c4` — a LikeC4 architecture model (`.c4`) built from this repo's import graph."""

import json
import os
import time
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


# How long to keep re-asking after a successful index before giving up, and how long to wait
# between attempts. 30s is chosen against the one measurement available: on pathly-adapters (246
# source files) the project was queryable by the next command invocation, seconds later. Kept
# deliberately short — this is a foreground CLI, and a wait long enough to cover an arbitrarily
# large repo would read as a hang. Exceeding it is reported, never silently treated as failure to
# index.
SETTLE_SECONDS = 30.0
SETTLE_INTERVAL_SECONDS = 2.0


def _settle(build: Any, payload: dict) -> dict:
    """Re-run *build* until it stops reporting a not-indexed reason, or the budget runs out.

    Returns the last payload either way; the caller decides what an un-settled result means. Never
    raises — a clock or provider failure degrades to the payload already in hand.
    """
    try:
        from codeintel import c4

        deadline = time.monotonic() + SETTLE_SECONDS
        while (payload.get("reason") or "") in c4.INDEXABLE_REASONS:
            if time.monotonic() >= deadline:
                break
            time.sleep(SETTLE_INTERVAL_SECONDS)
            payload = build()
        return payload
    except Exception:
        return payload


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


def _common_prefix(ids: list[str]) -> str:
    """The longest shared dotted-segment prefix, so a report of 54 module ids stays readable.

    Segment-wise, never character-wise: trimming `src.codeintel.c` off `c4` and `cache` would be
    shorter and meaningless. Returns "" when there is nothing shared, which is the common case for a
    payload spanning `src/`, `bench/` and `scripts/`.
    """
    if len(ids) < 2:
        return ""
    split = [i.split(".") for i in ids]
    shared: list[str] = []
    for parts in zip(*split, strict=False):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    # Never consume the whole id — an element must keep at least one segment to be nameable.
    while shared and any(len(s) <= len(shared) for s in split):
        shared.pop()
    return ".".join(shared) + "." if shared else ""


def _print_layers(payload: dict) -> None:
    """Print the inferred layer bands, top to bottom, plus cycles and what was left out.

    Elements are named by their ELEMENT ID, not their title. Titles collide — this repo alone has two
    elements titled `c4` (`src/codeintel/c4.py` and `src/codeintel/commands/c4.py`) and two titled
    `graph`, sitting in different bands. A first draft of this function printed titles, and the output
    showed `c4` twice in one band and `graph` in two, which reads as a bug in the ranking rather than
    an ambiguity in the label. A shared prefix is trimmed once, and named, to keep ids readable.

    Three things are stated explicitly rather than left for the reader to infer, because each is a way
    this output could be misread:

    * **Bands are IMPORTS-only, and IMPORTS is module-level only.** A reader who assumes it covers
      calls, or covers imports inside functions, would over-trust it.
    * **Zero violations here means nothing.** Inferred ranks make every edge descend by construction
      (§2.3), so an inferred layering can never report a violation. Printing a reassuring "no
      problems" would be the absence of an opinion dressed as a clean bill of health.
    * **An excluded element is not known to be unrelated.** This is the correction that matters most:
      `unassigned` means "no MODULE-LEVEL import edge either way", and that is not the same fact.
      `src/codeintel/doctor.py` has seven `codeintel` imports, every one of them inside a function,
      so `IMPORTS` sees none of them — it is heavily related and completely invisible here. An earlier
      draft of this line called such elements "unrelated rather than foundational", which was a claim
      about the world the edge source cannot support.
    """
    layers = payload.get("layers") or {}
    stats = layers.get("stats") or {}
    bands = layers.get("bands") or []
    unassigned = list(layers.get("unassigned") or [])

    every_id = [eid for band in bands for eid in (band.get("elements") or [])] + unassigned
    prefix = _common_prefix(every_id)

    def label(eid: str) -> str:
        return eid[len(prefix):] if prefix and eid.startswith(prefix) else eid

    ranked = int(stats.get("elements_ranked") or 0)
    total = int(stats.get("elements_total") or 0)
    print(f"Inferred layers for {payload.get('project') or '?'} — "
          f"{len(bands)} band(s) over {ranked} of {total} element(s), "
          f"{stats.get('edges_used') or 0} IMPORTS edge(s)")
    print("  source: module-level IMPORTS only. The CALLS|USAGE union is not layerable (measured: "
          "38.8%-70.4% of elements collapse into one cycle), and an import inside a function is "
          "invisible to IMPORTS entirely.")
    if prefix:
        print(f"  names below are relative to `{prefix}`")

    if layers.get("degraded"):
        largest = stats.get("largest_cycle") or 0
        print(f"  ! DEGRADED: one import cycle covers {largest} of {ranked} ranked element(s). "
              f"The band order below is close to meaningless; fix the cycle first.")

    if not bands:
        print("  no element has a module-level IMPORTS edge, so there is no layering to show")
    for position, band in enumerate(bands):
        names = sorted(label(eid) for eid in (band.get("elements") or []))
        print(f"  [{position}] rank {band.get('rank')}: {', '.join(names)}")

    cycles = layers.get("cycles") or []
    if cycles:
        print(f"  {len(cycles)} import cycle(s) — each occupies ONE rank, because its members have "
              f"no relative order:")
        for cycle in cycles:
            shown = ", ".join(label(m) for m in (cycle.get("members") or []))
            omitted = int(cycle.get("members_omitted") or 0)
            more = f" (+{omitted} more)" if omitted else ""
            print(f"    rank {cycle.get('rank')}, {cycle.get('size')} members: {shown}{more}")

    if unassigned:
        print(f"  {len(unassigned)} element(s) not placed — no MODULE-LEVEL import edge either way. "
              f"That is a limit of the edge source, NOT evidence they are unrelated: a module whose "
              f"imports all sit inside functions lands here while being heavily coupled.")
        shown = ", ".join(label(u) for u in unassigned[:12])
        extra = f" (+{len(unassigned) - 12} more)" if len(unassigned) > 12 else ""
        print(f"    {shown}{extra}")

    print("  note: inferred ranks make every edge descend by construction, so this view can never "
          "report a layer violation. Only a declared [layers] config can — see --suggest-config.")


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
    # and silence here reads as a hang). `--no-index` restores the strict behaviour for CI, where
    # "the index was missing" should fail the step rather than be quietly repaired.
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

        # The backend returns from `index_repository` BEFORE the freshly indexed project is
        # queryable, so a single immediate rebuild loses a race it cannot see. Measured on
        # pathly-adapters: the index reported success after 82s, the immediate retry still resolved
        # to nothing, and the very next invocation of this command produced a 62-element model from
        # that same index. A lone retry therefore printed "✓ indexed" and "project-not-indexed —
        # run `codeintel index`" one line apart, telling the user to redo what had just succeeded.
        #
        # So poll instead, bounded. Each `build()` constructs its own provider and therefore its own
        # resolution cache, which is what lets a later attempt see a registration an earlier one
        # missed — no cache-invalidation reach-through required.
        payload = _settle(build, payload)
        reason = payload.get("reason") or ""
        if reason in c4.INDEXABLE_REASONS:
            # Never re-suggest indexing here: it just succeeded. This is the backend not having
            # published the project, which is a different problem with a different fix.
            print(f"c4 failed: indexed this repo, but the graph backend still reports no project "
                  f"for it after {SETTLE_SECONDS:.0f}s ({reason})")
            print("  the index may still be building — re-run `codeintel c4` in a moment, "
                  "or check `codeintel doctor`")
            return 1

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

    # Both of these are BARE outputs like `--json`: they print and write nothing. `--suggest-config`
    # emits TOML meant to be redirected into a file, so it must never share stdout with a progress
    # line — hence it returns before the writing path rather than decorating it.
    if getattr(args, "suggest_config", False):
        from codeintel.c4_layers import suggest_config

        layers = payload.get("layers") or {}
        print(suggest_config(layers.get("ranks") or {}, payload.get("elements") or []),
              end="")
        return 0

    if getattr(args, "layers", False):
        _print_layers(payload)
        return 0

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
