"""The layer check: turn a C4 payload plus a declared ``[layers]`` config into FINDING RECORDS.

Phase 2 of [docs/layers-design.md](../../docs/layers-design.md), §5. Never raises.

**Records before serializers, and the order is deliberate** (§5.5). `check_layers` produces a list of
records; `render_report` and `--json` are two consumers that share no formatting logic. Writing the
text report first and extracting a record shape from it afterwards is precisely the refactor this
split exists to avoid — and it is why adding SARIF later is a new serializer rather than surgery on
the check.

**One finding is one record, and every class uses the same shape.** Irrelevant fields are ``None``
rather than absent, so a consumer never branches on key existence.

**`severity` is resolved here, not by a serializer.** `strict_adjacent` and `require_all` change
whether a given `kind` gates, so the record carries the answer *after* config is applied. Otherwise
two serializers could disagree about the exit code, which is the kind of bug nobody finds until CI
passes something it should have failed.

**No line numbers, and that is not an oversight** (§5.5). Measured against the live index: on an
``IMPORTS`` edge the source file's `line` is empty and `start_line` is 0, while the *target's*
`start_line` is populated. So the graph can point at the imported definition in the target file but
not at the import statement in the offending file — backwards from what a code annotation wants.
Carrying an invented or misleading line field would be worse than carrying none.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from codeintel.c4_layers import assign_declared_layers, compute_layers, glob_match

# `rule` is a stable machine id and the set is an API: it is what a future allowlist keys on, what a
# suppression file references, and what SARIF's `ruleId` maps to. Renaming one after release breaks
# other people's config, so these strings do not change.
RULE_ORDER = "layer-order"
RULE_CYCLE = "import-cycle"
RULE_UNASSIGNED = "layer-unassigned"
RULE_ALLOW_NO_REASON = "allow-no-reason"
RULE_ADVISORY = "layer-order-advisory"
RULE_SPLIT = "layer-split"
RULE_SPREAD = "layer-spread"
RULE_STALE_ALLOW = "stale-allow"
RULE_AMBIGUOUS = "layer-ambiguous"

# A declared layer whose members span at least this many inferred ranks is probably two layers wearing
# one name (§3.7). Informational, and explicitly a heuristic: the design calls the number a guess that
# "needs one real config on one real repo before it means anything", so it is named here rather than
# buried, and it never gates.
SPREAD_RANKS = 3

# Show some, count the rest — the same treatment `c4.py` already gives `dropped`.
PATHS_REPORT_CAP = 5


def _record(*, rule: str, kind: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    """One finding, with every field of the §5.5 shape present.

    Fields default to ``None`` rather than being omitted so a consumer never has to branch on key
    existence — the property that lets `--json` emit records verbatim and a future SARIF serializer
    read them without knowing which kind produced them.
    """
    base: dict[str, Any] = {
        "rule": rule, "kind": kind, "severity": severity, "message": message,
        "from_element": None, "to_element": None,
        "from_paths": None, "to_paths": None,
        "witness": None, "witnesses_total": None, "weight": None,
        "from_layer": None, "from_layer_index": None,
        "to_layer": None, "to_layer_index": None,
        "direction": None, "layers_skipped": None,
        "edge_source": None, "confirmed_by": None, "cycle_members": None,
        "allowlisted": False, "allow_reason": None, "allow_index": None,
        "depth": None,
    }
    base.update(extra)
    return base


def _paths_of(elements: Iterable[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for element in elements:
        eid = str(element.get("id") or "")
        if eid:
            out[eid] = sorted(p.strip() for p in str(element.get("path") or "").split(",")
                              if p.strip())
    return out


def _witness(from_paths: list[str], to_paths: list[str], weight: int) -> dict[str, Any]:
    """One concrete file pair behind an element-level finding.

    Mandatory (§5.5): an element-level violation is an aggregate over possibly many file pairs, and a
    finding a human cannot go and look at is not actionable. The first pair in sorted order is chosen
    so the same edge always names the same witness across runs.
    """
    return {"from": from_paths[0] if from_paths else None,
            "to": to_paths[0] if to_paths else None,
            "n": weight}


def _match_allow(entry: dict[str, Any], from_paths: list[str], to_paths: list[str],
                 from_element: str, to_element: str) -> bool:
    """Does an allowlist entry cover this edge?

    Matched against file paths first (the stable identity, per §3.3) and element ids second, so an
    entry written either way works. Globs are honoured through the same segment matcher membership
    uses, so `from = "src/codeintel/providers/**"` behaves as an author would expect.
    """
    src, dst = entry.get("from") or "", entry.get("to") or ""
    if not src or not dst:
        return False

    def hits(pattern: str, paths: list[str], element: str) -> bool:
        return (pattern == element
                or any(pattern == p or glob_match(pattern, p) for p in paths))

    return hits(src, from_paths, from_element) and hits(dst, to_paths, to_element)


def _cycle_record(cycle: dict[str, Any], depth: int) -> dict[str, Any]:
    """A `cycle` finding. Gating, because unlike an advisory up-edge an import cycle is a
    source-confirmed structural fact — and no declared order can be satisfied until it is broken.

    `from_element`/`to_element` name the first two members purely so a serializer that expects an
    edge-shaped record has something to show; `cycle_members` is the authoritative field.
    """
    members = list(cycle.get("members") or [])
    return _record(
        rule=RULE_CYCLE, kind="cycle", severity="gating",
        message=(f"import cycle of {cycle.get('size')} elements — no layering can order these "
                 f"until it is broken"),
        from_element=members[0] if members else None,
        to_element=members[1] if len(members) > 1 else None,
        cycle_members=members, weight=cycle.get("size"),
        witnesses_total=cycle.get("size"),
        edge_source="cycle", confirmed_by=["imports"], depth=depth)


def check_layers(payload: dict, parsed: dict[str, Any]) -> dict[str, Any]:
    """Compute every §5.2 finding class for one payload against one declared config. Never raises.

    Returns ``{mode, order, coverage, findings, gating, problem, shorthand, switches}``.

    Three things this deliberately does NOT do:

    * **It does not gate on inferred ranks.** By §2.3 every inferred edge descends, so an inferred
      layering yields zero violations by construction. Reporting that as "no problems" would dress the
      absence of an opinion as a clean bill of health.
    * **It does not gate on shorthand config.** An `order` with no `[layers.members]` has membership
      *guessed* from layer names (§3.6); failing someone's build on that guess is the worst outcome
      this feature could produce, so it is refused explicitly rather than quietly allowed.
    * **It does not filter allowlisted findings out.** They stay in the list with `allowlisted: true`
      and `severity` demoted to `info`. "Visible, not gating" is a property of the record, not of one
      serializer's formatting.
    """
    elements = list(payload.get("elements") or [])
    relations = list(payload.get("relations") or [])
    depth = int(((payload.get("fit") or {}).get("depth")) or 0)
    switches: dict[str, bool] = dict(parsed.get("switches") or {})
    order: list[str] = list(parsed.get("order") or [])

    result: dict[str, Any] = {
        "mode": "declared", "order": order, "shorthand": bool(parsed.get("shorthand")),
        "switches": switches, "problem": str(parsed.get("problem") or ""),
        "coverage": {"assigned": 0, "unassigned": 0, "total": len(elements)},
        "findings": [], "gating": 0,
    }
    if result["problem"] or not order:
        return result

    assignment = assign_declared_layers(elements, parsed)
    layer_of: dict[str, str] = assignment["layer_of"]
    index_of: dict[str, int] = assignment["index_of"]
    paths_of = _paths_of(elements)
    inferred = compute_layers(elements, relations)
    inferred_ranks: dict[str, int] = inferred.get("ranks") or {}

    result["coverage"] = {"assigned": len(layer_of),
                          "unassigned": len(assignment["unassigned"]),
                          "total": len(elements)}

    findings: list[dict[str, Any]] = []
    allow_entries: list[dict[str, Any]] = list(parsed.get("allow") or [])
    allow_used: set[int] = set()

    # ── allowlist hygiene: an entry with no reason is a GATING finding ─────────────────────────────
    # The single rule that separates an allowlist from a mute button. Reported before any violation so
    # a reader sees that the allowlist itself is untrustworthy before reading what it excused.
    findings += [
        _record(
            rule=RULE_ALLOW_NO_REASON, kind="allow-no-reason", severity="gating",
            message=(f"allowlist entry {entry['index']} "
                     f"({entry.get('from') or '?'} -> {entry.get('to') or '?'}) has no reason; "
                     f"an exception without a reason is a mute button"),
            from_element=entry.get("from") or None, to_element=entry.get("to") or None,
            allow_index=entry["index"], depth=depth)
        for entry in allow_entries if not entry.get("reason")
    ]

    # ── violations, from IMPORTS edges where BOTH ends are declared (§5.1) ─────────────────────────
    for rel in relations:
        a, b = str(rel.get("from") or ""), str(rel.get("to") or "")
        weight = int(rel.get("n") or 0)
        kind_of_edge = str(rel.get("kind") or "")
        if a == b or a not in layer_of or b not in layer_of:
            # Both ends must be declared. You cannot violate an order you did not declare (§3.5).
            continue

        from_index, to_index = index_of[a], index_of[b]
        if to_index < from_index:
            direction, skipped = "up", 0
        elif to_index == from_index:
            direction, skipped = "same", 0
        else:
            direction, skipped = "skip", to_index - from_index - 1

        if kind_of_edge != "imports":
            # CALLS/USAGE-only edge. NEVER gating: the union fabricates edges by bare symbol name and
            # is not evidence of a dependency (§1). Recorded so the information is not lost, and
            # collapsed to a count by the text serializer so it cannot drown the real findings.
            if direction == "up":
                findings.append(_record(
                    rule=RULE_ADVISORY, kind="advisory", severity="advisory",
                    message=f"{a} -> {b} points up, but only via CALLS/USAGE — not a dependency",
                    from_element=a, to_element=b,
                    from_paths=paths_of.get(a, [])[:PATHS_REPORT_CAP],
                    to_paths=paths_of.get(b, [])[:PATHS_REPORT_CAP],
                    witness=_witness(paths_of.get(a, []), paths_of.get(b, []), weight),
                    witnesses_total=1, weight=weight,
                    from_layer=layer_of[a], from_layer_index=from_index,
                    to_layer=layer_of[b], to_layer_index=to_index,
                    direction=direction, layers_skipped=skipped,
                    edge_source="calls_usage", confirmed_by=["calls_usage"], depth=depth))
            continue

        offends = (direction == "up"
                   or (direction == "same" and not switches.get("allow_same_layer", True))
                   or (direction == "skip" and switches.get("strict_adjacent", False)
                       and skipped > 0))
        if not offends:
            continue

        if direction == "up":
            message = f"{layer_of[a]} imports {layer_of[b]}, which is above it"
        elif direction == "same":
            message = f"{a} imports {b} within layer {layer_of[a]}, and same-layer imports are off"
        else:
            message = (f"{layer_of[a]} imports {layer_of[b]}, skipping {skipped} layer(s) "
                       f"while strict_adjacent is on")

        from_paths = paths_of.get(a, [])
        to_paths = paths_of.get(b, [])
        allowed_by = next((e for e in allow_entries
                           if e.get("reason")
                           and _match_allow(e, from_paths, to_paths, a, b)), None)
        if allowed_by is not None:
            allow_used.add(allowed_by["index"])

        findings.append(_record(
            rule=RULE_ORDER, kind="violation",
            severity="info" if allowed_by is not None else "gating",
            message=message,
            from_element=a, to_element=b,
            from_paths=from_paths[:PATHS_REPORT_CAP], to_paths=to_paths[:PATHS_REPORT_CAP],
            witness=_witness(from_paths, to_paths, weight),
            witnesses_total=max(1, len(from_paths) * len(to_paths)), weight=weight,
            from_layer=layer_of[a], from_layer_index=from_index,
            to_layer=layer_of[b], to_layer_index=to_index,
            direction=direction, layers_skipped=skipped,
            edge_source="imports", confirmed_by=["imports"],
            allowlisted=allowed_by is not None,
            allow_reason=(allowed_by or {}).get("reason"),
            allow_index=(allowed_by or {}).get("index"), depth=depth))

    # ── cycles: source-confirmed structural facts, so they gate (§5.2) ─────────────────────────────
    findings += [_cycle_record(cycle, depth) for cycle in (inferred.get("cycles") or [])]

    # ── unassigned: gating only under require_all (§3.5) ──────────────────────────────────────────
    require_all = switches.get("require_all", False)
    findings += [
        _record(
            rule=RULE_UNASSIGNED, kind="unassigned",
            severity="gating" if require_all else "info",
            message=f"{eid} matches no declared layer",
            from_element=eid, from_paths=paths_of.get(eid, [])[:PATHS_REPORT_CAP], depth=depth)
        for eid in assignment["unassigned"]
    ]

    # ── split: an element whose files span layers, resolved upward (§3.4) ──────────────────────────
    findings += [
        _record(
            rule=RULE_SPLIT, kind="split", severity="info",
            message=(f"{split['element']}'s files span layers {list(split['layer_split'])} — taking "
                     f"the highest ({split['chosen']}); a deeper --depth resolves this"),
            from_element=split["element"], from_layer=split["chosen"],
            from_paths=paths_of.get(split["element"], [])[:PATHS_REPORT_CAP], depth=depth)
        for split in assignment["splits"]
    ]

    # ── ambiguous: the tie was broken by declaration order alone (§3.3) ───────────────────────────
    findings += [
        _record(
            rule=RULE_AMBIGUOUS, kind="layer-ambiguous", severity="info",
            message=(f"{amb['path']} matches '{amb['pattern_a']}' ({amb['layer_a']}) and "
                     f"'{amb['pattern_b']}' ({amb['layer_b']}) equally specifically; resolved by "
                     f"order alone"),
            from_paths=[amb["path"]], from_layer=amb["layer_a"], to_layer=amb["layer_b"],
            depth=depth)
        for amb in assignment["ambiguous"]
    ]

    # ── spread: a declared layer covering a wide inferred rank range (§3.7) ───────────────────────
    for name in order:
        ranks = sorted(inferred_ranks[eid] for eid, layer in layer_of.items()
                       if layer == name and eid in inferred_ranks)
        if len(ranks) >= 2 and (ranks[-1] - ranks[0] + 1) >= SPREAD_RANKS:
            findings.append(_record(
                rule=RULE_SPREAD, kind="spread", severity="info",
                message=(f"layer '{name}' spans inferred ranks {ranks[0]}..{ranks[-1]} — possibly "
                         f"two layers wearing one name"),
                from_layer=name, from_layer_index=order.index(name), depth=depth))

    # ── stale allowlist entries: the violation was fixed, delete the exception ─────────────────────
    # Without this an allowlist only ever grows, and in three years nobody knows which entries are
    # load-bearing. Never gating: a stale entry is untidy, not broken.
    findings += [
        _record(
            rule=RULE_STALE_ALLOW, kind="stale-allow", severity="info",
            message=(f"allowlist entry {entry['index']} ({entry['from']} -> {entry['to']}) "
                     f"matches no current violation — the exception can be deleted"),
            from_element=entry["from"], to_element=entry["to"],
            allow_index=entry["index"], allow_reason=entry.get("reason"), depth=depth)
        for entry in allow_entries
        if entry.get("reason") and entry["index"] not in allow_used
    ]

    # Deterministic order (§5.5), so both the text output and the JSON diff cleanly between runs — the
    # same requirement the emitted `.c4` has.
    findings.sort(key=lambda f: (str(f["kind"]), str(f["from_element"] or ""),
                                 str(f["to_element"] or ""),
                                 str((f.get("witness") or {}).get("from") or "")))

    result["findings"] = findings
    result["gating"] = sum(1 for f in findings if f["severity"] == "gating")
    if result["shorthand"]:
        # §3.6 — supported for looking, refused for gating. The findings stay (they are worth reading)
        # but the gating count is zeroed, and `--check` reports the refusal rather than an exit 2 that
        # would rest on a guess about which layer each file belongs to.
        result["gating"] = 0
    return result


def render_report(payload: dict, check: dict[str, Any]) -> str:
    """The plain-text serializer — one of two consumers of the records, sharing no logic with `--json`.

    Three formatting rules that matter more than they look (§5.6):

    * **Two lines per finding, maximum** — the rule and the layer pair, then the witness. A CI log is
      skimmed, not read.
    * **Advisory is collapsed to a count with a pointer, never enumerated.** It is the largest and
      least trustworthy class, and a report that leads with fourteen advisories teaches people to
      ignore the whole thing. Collapsing it is a correctness property of the report, not a space
      saving.
    * **The gating count and the exit code share the last line**, because whoever is reading a failed
      CI step is looking at the bottom.
    """
    fit = payload.get("fit") or {}
    findings: list[dict[str, Any]] = list(check.get("findings") or [])
    coverage = check.get("coverage") or {}
    lines: list[str] = ["codeintel c4 --check — layer report"]
    lines.append(f"  project: {payload.get('project') or '?'}      "
                 f"depth {fit.get('depth')} ({fit.get('how')})      "
                 f"mode: {check.get('mode')}")
    if check.get("order"):
        lines.append("  layers:   " + " > ".join(check["order"]))
    lines.append(f"  coverage: {coverage.get('assigned', 0)} of {coverage.get('total', 0)} elements "
                 f"assigned; {coverage.get('unassigned', 0)} unassigned")
    if check.get("shorthand"):
        lines.append("  ! shorthand config: `order` without [layers.members], so membership is "
                     "GUESSED from layer names. Reported, never gating — add [layers.members] to gate.")

    def group(kind: str) -> list[dict[str, Any]]:
        return [f for f in findings if f["kind"] == kind]

    violations = group("violation")
    gating_v = sum(1 for f in violations if f["severity"] == "gating")
    allowed_v = sum(1 for f in violations if f["allowlisted"])
    lines.append("")
    lines.append(f"VIOLATIONS ({gating_v} gating, {allowed_v} allowlisted)")
    for finding in violations:
        tail = (f"ALLOWED: {finding['allow_reason']}" if finding["allowlisted"]
                else finding["message"])
        witness = finding.get("witness") or {}
        # Two lines per finding, maximum (§5.6): the rule and the layer pair, then the witness.
        lines += [
            f"  {finding['rule']}  {finding['from_element']} -> {finding['to_element']}    {tail}",
            f"      {witness.get('from')} -> {witness.get('to')}  (x{witness.get('n')})",
        ]
    if not violations:
        lines.append("  none")

    cycles = group("cycle")
    lines.append("")
    lines.append(f"CYCLES ({len(cycles)})")
    lines += [f"  {finding['rule']}  {finding['weight']} elements: "
              f"{', '.join(finding['cycle_members'] or [])}"
              for finding in cycles]

    for kind, heading in (("unassigned", "UNASSIGNED"), ("split", "SPLIT"),
                          ("spread", "SPREAD"), ("layer-ambiguous", "AMBIGUOUS"),
                          ("stale-allow", "STALE ALLOWLIST"), ("allow-no-reason", "ALLOWLIST")):
        rows = group(kind)
        if not rows:
            continue
        gating_n = sum(1 for f in rows if f["severity"] == "gating")
        suffix = f" — {gating_n} gating" if gating_n else " — never gating"
        lines.append("")
        lines.append(f"{heading} ({len(rows)}){suffix}")
        lines += [f"  {finding['rule']}  {finding['message']}"
                  for finding in rows[:PATHS_REPORT_CAP * 4]]
        if len(rows) > PATHS_REPORT_CAP * 4:
            lines.append(f"  ... and {len(rows) - PATHS_REPORT_CAP * 4} more — use --json")

    advisory = group("advisory")
    if advisory:
        lines.append("")
        lines.append(f"ADVISORY ({len(advisory)} — never gating)")
        lines.append(f"  {len(advisory)} CALLS/USAGE-only edge(s) point up the stack. These are NOT "
                     f"evidence of a")
        lines.append("  dependency (see docs/layers-design.md §1). Use --json for the list.")

    gating = int(check.get("gating") or 0)
    lines.append("")
    if check.get("shorthand") and any(f["severity"] == "gating" for f in findings):
        lines.append("0 gating finding(s) — exit 0 "
                     "(shorthand config cannot gate; add [layers.members])")
    else:
        lines.append(f"{gating} gating finding(s) — exit {2 if gating else 0}")
    return "\n".join(lines)
