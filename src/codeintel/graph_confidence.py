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


def _confidence_badge(row: dict) -> str:
    """The per-row mark for an edge the backend did not resolve through an import.

    Two glyphs rather than one because the tiers mean different things and a reader scanning a list
    should be able to tell "unverified" from "probably junk" without consulting the note: `?` is a
    unique-name binding, `!` a suffix or string-similarity one."""
    if "_low_confidence" not in row:
        return ""
    conf = row.get("_low_confidence")
    if conf is None:
        # Condemned by its strategy, with no number attached. Say the strategy's verdict rather
        # than inventing a score for it.
        return " [?name-guess]"
    # ONE glyph. `!` and `?` used to split on the confidence float, which put `unique_name` at 0.75
    # and `unique_name` at 0.38 — the same strategy, the same kind of evidence — into two different
    # visual classes. The number still shows, as detail behind a single verdict.
    return f" [?{float(conf):.2f}]"
