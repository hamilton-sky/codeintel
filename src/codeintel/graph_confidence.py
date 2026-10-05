"""How much the backend vouches for an edge, and how that reaches a reader.

Separated from rendering because it is a POLICY, and the policy is the part that has been wrong
here more than once: a numeric floor split one resolution strategy across two tiers, and a badge
driven by the float rather than by the strategy reported the same kind of evidence two different
ways. Provenance (`c.strategy`) decides; the number is detail.

The rule this module exists to keep is one line long and has cost two bugs: **an unstamped edge is
not a confident one.** A backend generation that reports no confidence at all has to stay
distinguishable from one that reports a low number — the same distinction the envelope's own
`confidence` field preserves one level up, and the same one `outcome.py` draws between a failed
call and an empty answer.
"""
from __future__ import annotations

from codeintel.graph_render import _lang_family

# HOW an edge was resolved, which the backend records per edge as `c.strategy`. This is provenance,
# not a score, and it is the field that should drive policy.
#
# The confidence float alone is not enough, and shipping a threshold over it was a mistake this
# constant exists to correct: on one real repository `unique_name` — a single strategy, one kind of
# guess — appears at BOTH 0.75 and 0.38, so a numeric floor splits one strategy across two tiers and
# reports the same kind of evidence two different ways. Meanwhile `same_module` at 0.90 and
# `lsp_constructor` at 0.85 are genuinely different KINDS of claim that a float renders as neighbours.
# Read the strategy; keep the number as detail.
#
# Classified by prefix because the vocabulary is open — `lsp_direct`, `lsp_ts_method`,
# `lsp_callable_alias`, `lsp_builtin_constructor` and a dozen more are all the same class of
# evidence, and enumerating them would go stale on the backend's next release.
# The three buckets a caller row can fall into, which is what the heading counts. Deliberately
# COARSER than `_evidence_class` above: a reader deciding whether to trust a count needs to know
# whether a binding was followed, not which of nine lsp strategies followed it. The fine grain stays
# on the row badge and in the note beneath.
_RESOLVED = "resolved"


_NAME_MATCHED = "name-matched"


_UNSTATED = "unstated"


_TRUSTED_EVIDENCE: tuple[str, ...] = ("lsp", "import_map", "same_module")


_GUESS_EVIDENCE: tuple[str, ...] = ("unique_name", "suffix_match", "fuzzy", "qualified_suffix")


def _evidence_class(strategy: str) -> str:
    """`c.strategy` reduced to the class that decides how an edge should be presented.

    ``"lsp"`` / ``"import"`` / ``"same-module"`` are resolutions: something followed a real binding.
    ``"name-guess"`` is the cascade falling back on a bare name matching. ``""`` means the backend
    did not say, which is its own state and never silently promoted to either side."""
    st = (strategy or "").strip().lower()
    if not st:
        return ""
    # Two vocabularies reach this function and both have to be understood. `query_graph` returns the
    # SPECIFIC strategy on the edge (`lsp_callable_alias`, `unique_name`, `suffix_match`);
    # `trace_path --include-evidence` returns the backend's own COARSE class
    # (`lsp | language_rule | heuristic | unresolved`). Mapping only the specific names left every
    # traced hop labelled "other" — including the `heuristic` ones, which are exactly the guesses
    # that most need saying.
    if st.startswith("lsp"):
        return "lsp"
    if st.startswith("import"):
        return "import"
    if st.startswith("same_module"):
        return "same-module"
    # Not a strategy the backend reports: the label this project gives an edge it RE-RESOLVED from the
    # class hierarchy (`self.m()` bound through the caller's own ancestry — `graph_dispatch.py`). It
    # is classified here, with the others, so every reader of `strategy` agrees it is a resolution.
    if st.startswith("self_mro"):
        return "self-mro"
    if st.startswith("language_rule"):
        return "language-rule"
    if st.startswith("unresolved"):
        return "unresolved"
    # `heuristic` is the coarse class the backend uses for the weak end of its cascade — the same
    # thing `unique_name`/`suffix_match` are, named one level up.
    if st.startswith("heuristic"):
        return "name-guess"
    for guess in _GUESS_EVIDENCE:
        if st.startswith(guess):
            return "name-guess"
    return "other"


# How much the backend trusts an edge's target resolution — and the line below which it is a GUESS.
#
# The graph backend resolves each call target through a prioritised cascade and stamps the edge with
# a confidence: 0.95 when it followed the file's import map, 0.90 same-module, 0.85 import-suffix —
# and then 0.75 for "the only symbol in the whole repository carrying this bare name", 0.55 for a
# suffix match among several candidates, and 0.30-0.40 for raw string similarity. Only the first
# three consult the imports of the file the call is written in. Everything below them is name
# matching, and name matching is exactly how a call to a framework global (`describe` from vitest,
# `dict.get`) or to a local callback (`onClose`, `setScope`) acquires an edge to whichever project
# symbol happens to share its name.
#
# These are not rare. Measured over the CALLS edges of three real repositories: 24%, 33% and 43% of
# every edge sat below this floor. codeintel selected these rows and then dropped the confidence
# column on the floor, so a fabricated caller rendered identically to a real one and the envelope
# still said `confidence: "complete"` — the one combination the safe-null contract exists to make
# impossible.
#
# The rows are KEPT, not filtered: dropping a 0.75 row would trade a false positive for a false
# negative, and "no callers" is the more dangerous of the two when the next action is a delete.
# They are labelled in the body, counted in a note, and raised as a gap so the envelope goes
# `partial`.
_EDGE_CONFIDENCE_FLOOR = 0.85


# Below the floor the cascade stops consulting imports, but it does not become uniformly wrong, and
# a check that treats it as such is its own precision bug. Two tiers, because they fail differently:
#
#   0.55 < c < 0.85  — `unique_name`. The call resolved here because this is the ONLY symbol in the
#                      index carrying that bare name. That is right whenever the call really does
#                      target a project symbol (measured by hand: `runAlerts -> evaluate` and
#                      `buildSnapshot -> usageDayFor` are both genuine, and both stamped 0.75), and
#                      wrong whenever it targets a same-named symbol the index never saw. Suspicion,
#                      not a verdict.
#   c <= 0.55        — suffix match among several candidates, or raw string similarity. These are
#                      the rows that put an archived UI component in one tree "calling" a hook in
#                      another because both mention `setScope`.
_EDGE_CONFIDENCE_WEAK = 0.55


def _edge_confidence(row: dict) -> float | None:
    """How much the backend vouches for THIS edge's target resolution, or None if it never said.

    An unstamped edge is not a confident one: older index generations wrote no confidence at all,
    and on one evaluated repository 409 of 8,969 CALLS edges came back blank. Returning None rather
    than a default keeps "the backend did not say" distinguishable from "the backend said 0.95",
    which is the same distinction the envelope's `confidence` field exists to preserve one level up.
    """
    raw = row.get("c.confidence")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# `same_module` is the strategy this module used to take on trust, and the one that was wrong.
#
# What the backend does for it: the call's bare leaf name is looked up among the symbols defined in
# the CALLER'S OWN FILE, and a hit is stamped 0.90. That is a sound resolution for a call written as
# a bare name (`run()` inside the module that defines `run` binds by scope, no import needed) and an
# unsound one for a call written through a receiver — `subprocess.run(...)` in a module that also
# defines a `run` is a call to the standard library, and the backend bound it to the local `run`
# anyway because the leaf matched. Measured on this repository: `callers run@bench/score.py` listed
# `_run_codeintel` and `_provenance` as `resolved` callers of `bench.score.run`. Neither calls it —
# both call `subprocess.run` — and the line attached to them said the edge "followed an import or a
# language-server binding", which is false for `same_module` in every case.
#
# The edge carries what is needed to tell the two apart, which is why this reads no source: the
# backend records the callee EXACTLY AS WRITTEN at the call site (`c.callee` — `subprocess.run`,
# `run`, `self.helper`), next to the strategy and the score. Reading it back costs nothing and cannot
# be stale, where re-reading the caller's file would be both slower and a second opinion about text
# the extractor had already parsed.
#
# Three verdicts, because "I checked and it is not" and "I could not check" license opposite things:
_SAME_MODULE_OWN = "own"
_SAME_MODULE_FOREIGN = "foreign"
_SAME_MODULE_UNCHECKED = "unchecked"

# A receiver that denotes the enclosing object itself, so `self.helper()` resolving to a symbol in
# the same module is the rule working as intended and not a guess. The check is defined for exactly
# the languages listed here: the ones whose member-call semantics are the same — a bare name binds by
# scope, a name reached through a receiver binds to whatever the receiver is — and only the SPELLING
# of "the enclosing object" differs (`self`/`cls` in Python, `this`/`super` in JavaScript and
# TypeScript). Keyed by `_lang_family`, and a language not in it is `unchecked`: Go's package
# qualifiers and Rust's paths each need a rule of their own, and one borrowed from the wrong
# language would downgrade real edges.
_OWN_RECEIVERS: dict[str, frozenset[str]] = {
    "python": frozenset({"self", "cls"}),
    "ts-js": frozenset({"this", "super"}),
}


def _same_module_call(row: dict) -> str:
    """Whether a `same_module` edge's call site is one that rule can bind: `own`, `foreign`, or
    `unchecked` when the question cannot be answered from what the edge carries.

    `foreign` is claimed only on clear evidence: the callee text is a dotted expression whose
    receiver is neither the enclosing object (`self`, `cls`, `super(...)`) nor a name that appears in
    the called symbol's own qualified name (the class or module that owns it, so `Config.load` and
    `score.run` inside their own module are left alone). Everything else is `unchecked`, and an
    unchecked row stays exactly as resolved as it was — a check that cannot run must not move a row,
    which is the rule `source-unreadable` already follows elsewhere in this project.

    Python, JavaScript and TypeScript only (`_OWN_RECEIVERS`). The rule is a statement about the
    scoping those languages share (a bare name binds to the module's own definition; an attribute
    access never does). Go's package qualifiers and Rust's paths each need their own, and a rule
    borrowed from the wrong language would downgrade real edges — the over-filtering failure this
    project retired `deadcode` for.
    """
    callee = str(row.get("callee") or "").strip()
    if not callee:
        return _SAME_MODULE_UNCHECKED           # an older index, or an edge the extractor left blank
    own_receivers = _OWN_RECEIVERS.get(_lang_family(str(row.get("a.file_path") or "")))
    if own_receivers is None:
        return _SAME_MODULE_UNCHECKED
    target = str(row.get("b.name") or "")
    receiver, dot, leaf = callee.rpartition(".")
    # The text has to be ABOUT the symbol the edge points at. A different leaf means this is not the
    # call the backend bound (an alias, a wrapper), and then nothing here applies.
    if target and leaf != target:
        return _SAME_MODULE_UNCHECKED
    if not dot:
        return _SAME_MODULE_OWN
    # `this?.log()` is optional chaining on the enclosing object, which is still the enclosing object.
    receiver = receiver.rstrip("?")
    if receiver in own_receivers or receiver.startswith("super("):
        return _SAME_MODULE_OWN
    owners = {seg for seg in str(row.get("b.qualified_name") or "").split(".")[:-1] if seg}
    if receiver.rsplit(".", 1)[-1] in owners:
        return _SAME_MODULE_OWN
    return _SAME_MODULE_FOREIGN


def _edge_strength(row: dict) -> tuple[int, float]:
    """How much an edge is worth, as a sort key where SMALLER is stronger.

    The one place the question "which of two edges between the same pair do we keep, and which of
    two callers do we drop first" is answered, so the answers cannot disagree with each other or with
    the bucket the row is eventually counted under. Mirrors the classification `_confidence_note`
    applies — a bound edge, then an unscored one, then a guess — without needing the row to have
    been classified yet, because ranking happens before the note is written.
    """
    evidence = _evidence_class(str(row.get("strategy") or ""))
    conf = _edge_confidence(row)
    if evidence in ("lsp", "import", "self-mro"):
        rank = 0
    elif evidence == "same-module":
        rank = 2 if _same_module_call(row) == _SAME_MODULE_FOREIGN else 0
    elif evidence == "name-guess":
        rank = 2
    elif conf is None:
        rank = 1
    else:
        rank = 0 if conf >= _EDGE_CONFIDENCE_FLOOR else 2
    return rank, -(conf if conf is not None else -1.0)


def _callee_for_display(callee: str) -> str:
    """The callee text, made safe to quote inside a sentence that is parsed back by line."""
    flat = " ".join(callee.split())
    return flat if len(flat) <= 60 else flat[:57] + "..."


def _via_kind(row: dict) -> str:
    """`protocol`, `protocol-undecided` or `base`: what kind of base method a `via` row called."""
    kind = str(row.get("_via_kind") or "")
    return kind if kind in ("protocol", "protocol-undecided") else "base"


def _why(row: dict, bucket: str) -> str:
    """One sentence saying what ACTUALLY happened to produce this row's bucket.

    There used to be one sentence for the whole `resolved` bucket — "followed an import or a
    language-server binding" — which is true of `lsp_*` and `import_map` and false of the third
    thing that bucket holds. A reader (or an agent) deciding whether to act on a row reads this, so
    it is stated per strategy: the claim is exactly as strong as the mechanism behind it and no
    stronger.
    """
    strategy = str(row.get("strategy") or "").strip()
    via = str(row.get("_via") or "")
    if via:
        # A caller of a BASE method listed under a symbol that overrides it. Whatever the edge's own
        # strategy was, it bound the call to `via` and not to this symbol, so the row is never
        # `resolved` for the symbol asked about; the strategy is kept here as the account of how it
        # reached `via`, because that is the part of the claim the backend did make.
        kind = "base class" if _via_kind(row) == "base" else "Protocol"
        how = (f"the receiver's declared type (`{strategy}`)" if strategy == "field_type_hint"
               else f"`{strategy}`" if strategy else "an edge the backend left unlabelled")
        text = (f"resolved to the {kind} method `{via}` by {how}, not to this symbol; it reaches this "
                "override only by dispatch, when the object is an instance of this symbol's class, "
                "so no binding the index records ties it to this symbol")
        # Each of these is a way the claim is weaker than the line above says, stated on the row so
        # it is not only in a heading three screens up.
        if _via_kind(row) == "protocol-undecided":
            text += ("; and whether that class satisfies the Protocol is undecided — an attribute or "
                     "property could supply a member it lacks as a method")
        if row.get("_via_how") == "base_classes":
            text += ("; and the link from this symbol's class to that base was found by resolving the "
                     "base-class NAME its statement wrote, not from an INHERITS edge")
        if row.get("_via_super"):
            text += (f"; it is a `super()` call inside `{row['_via_super']}`, a class that descends from "
                     "this symbol's class, so it can reach this symbol")
        elif row.get("_via_super_unplaced"):
            text += ("; it is a `super()` call from a class the index could not place in this symbol's "
                     "hierarchy, so whether it can reach this symbol was not decided")
        return text
    if bucket == _RESOLVED:
        evidence = str(row.get("_evidence") or "") or _evidence_class(strategy)
        if evidence == "self-mro":
            return str(row.get("_self_mro") or "resolved through the caller's own class hierarchy")
        if evidence == "lsp":
            return f"a language server resolved the call to this symbol ({strategy})"
        if evidence == "import":
            return ("the caller's file imports this symbol and the call was followed through "
                    f"that import ({strategy})")
        if evidence == "same-module":
            if str(row.get("_same_module") or "") == _SAME_MODULE_OWN:
                return ("this module defines the symbol and the call names it bare, so scope "
                        "binds it — no import or language server was involved")
            # Reached only when the call-site check COULD NOT decide, and the row is counted as
            # `resolved` (so `verified` is true) all the same — which is why this sentence may not
            # say "no binding was followed" and stop there. It says what scope did, what scope
            # needs, and that the second half was not confirmed: the residual risk, stated beside the
            # verdict it qualifies instead of contradicting it.
            callee = str(row.get("callee") or "").strip()
            if not callee:
                why_unchecked = "the backend recorded no call text for this edge"
            elif _lang_family(str(row.get("a.file_path") or "")) not in _OWN_RECEIVERS:
                why_unchecked = ("the call-site check is only defined for Python and "
                                 "JavaScript/TypeScript")
            else:
                why_unchecked = (f"the recorded call text `{_callee_for_display(callee)}` is not a "
                                 "call of this symbol's name")
            return ("scope resolved this inside the caller's own module (`same_module`). Scope only "
                    "binds a call written bare, and whether this call is written bare could not be "
                    f"checked ({why_unchecked}) — so it is counted as resolved, as every "
                    "`same_module` edge was before that check existed; if it is written through a "
                    "receiver, the binding is a guess")
        return ("the backend scored this edge at or above "
                f"{_EDGE_CONFIDENCE_FLOOR} without naming a strategy, a tier it only reaches "
                "through the caller's imports")
    if bucket == _NAME_MATCHED:
        if str(row.get("_same_module") or "") == _SAME_MODULE_FOREIGN:
            callee = _callee_for_display(str(row.get("callee") or ""))
            return (f"matched by name inside the caller's own module, but the call is written "
                    f"`{callee}` — through a receiver, not as this module's own symbol — so the "
                    f"match is a guess about what the receiver is ({strategy})")
        text = (f"matched by name ({strategy})" if strategy
                else "matched by bare symbol name, not by following a binding")
        # A `self.m()` the class hierarchy would bind if one of its links were an edge: said here so
        # a reader who sees a `self.` call still badged knows the rule looked and why it declined.
        note = str(row.get("_hierarchy_note") or "")
        return f"{text}; {note}" if note else text
    if bucket == _UNSTATED:
        return "the backend reported no provenance for this edge"
    return "unclassified"


def _confidence_badge(row: dict) -> str:
    """The per-row mark for an edge the backend did not resolve through an import.

    Two glyphs rather than one because the tiers mean different things and a reader scanning a list
    should be able to tell "unverified" from "probably junk" without consulting the note: `?` is a
    unique-name binding, `!` a suffix or string-similarity one."""
    if "_low_confidence" not in row:
        return ""
    conf = row.get("_low_confidence")
    if row.get("_via"):
        # The backend's score is for the edge to the BASE method — kept on show as detail, as every
        # other badge does — but the verdict is about this symbol, and the glyph says which kind of
        # base the call went through, since that is what a reader scanning the list needs to know.
        kind = _via_kind(row)
        return f" [?via {kind}]" if conf is None else f" [?via {kind} {float(conf):.2f}]"
    # A `same_module` edge this module refused to trust keeps the backend's own score on show — the
    # number is the backend's and is still detail — but the verdict is OURS, and a bare `[?0.90]`
    # reads as high confidence behind a question mark. Say what the doubt is about.
    via = " qualified call" if row.get("_same_module") == _SAME_MODULE_FOREIGN else ""
    if conf is None:
        # Condemned by its strategy, with no number attached. Say the strategy's verdict rather
        # than inventing a score for it.
        return f" [?{via.strip()}]" if via else " [?name-guess]"
    # ONE glyph. `!` and `?` used to split on the confidence float, which put `unique_name` at 0.75
    # and `unique_name` at 0.38 — the same strategy, the same kind of evidence — into two different
    # visual classes. The number still shows, as detail behind a single verdict.
    return f" [?{float(conf):.2f}{via}]"
