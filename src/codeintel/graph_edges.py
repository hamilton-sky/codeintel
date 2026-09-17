"""The relationship kinds a graph answer is built from, and how rows group by symbol.

`_EdgeGroup` is the unit that keeps one symbol's rows apart from another symbol's when the two
share a bare name. That separation is structural rather than remembered: a row can only ever be
compared against its own group, so the caller-family union bug that `0.15.5` fixed by hand cannot
be written again on top of it.

`_EDGE_KINDS` is here rather than in the renderer because the kinds are a claim about the CODE —
called, referenced, passed as a value — and conflating them is what reported a module-scope
mention as a caller and never asked for `CALL_REFERENCE` at all.
"""
from __future__ import annotations

from dataclasses import dataclass

from codeintel.graph_render import _strip_project_prefix

# The row cap on the two symbol-edge queries. Named rather than inlined because the RENDERER needs
# to know it: an answer that came back exactly AT the cap was truncated by us, and a truncated
# callee list that reads as complete is the same defect class as a filtered one that reads as
# complete. `callees` feeds "is this safe to change?", where "unknown" and "none" are opposite
# answers.
_EDGE_ROW_LIMIT = 50


# What each relationship kind ASSERTS, in the words a reader needs.
#
# These are different facts, not different confidences in one fact, and conflating them is a
# category error this engine used to commit twice over. `callers` matched `[:CALLS|USAGE]` and
# printed every row under one "Callers" heading — so a module-scope mention was reported as a
# caller — while `CALL_REFERENCE`, the kind that records a function being PASSED somewhere rather
# than invoked, was never queried at all. The visible cost: `forward_released_item` is registered
# via `set_forward_fn(app.forward_released_item)` at two sites, the backend had both of them
# correctly stored as CALL_REFERENCE, and codeintel answered "no callers" — the exact reading that
# gets a live method deleted. No confidence threshold could have recovered that; only asking for
# the right relationship can.
_EDGE_KINDS: dict[str, str] = {
    "CALLS": "called directly",
    "USAGE": "referenced, not called (module scope, or a mention that is not a call site)",
    "CALL_REFERENCE": "passed as a value or registered as a callback — never invoked here",
}


_DIRECT_KIND = "CALLS"


# How many same-named candidates to name when the answer has to ask "which one?". A list long enough
# to be unreadable is not a choice offered, and the count always states the full total.
_CANDIDATE_CAP = 12


@dataclass
class _EdgeGroup:
    """The rows belonging to ONE symbol, held apart from every other symbol sharing its bare name.

    Grouping is what stops a `callees` answer from being the union of several questions with no way
    to tell which row came from where. It also makes the per-row language check structural rather
    than remembered: a row can only ever be compared against its own group's caller, so the
    caller-family UNION bug that `0.15.5` fixed by hand cannot be written again here."""

    label: str
    qn_raw: str
    file: str
    rows: list[dict]

    def describe(self) -> str:
        if self.label and self.file:
            return f"`{self.label}` ({self.file})"
        if self.label:
            return f"`{self.label}`"
        return self.file or "(a symbol the index does not name)"


def _group_edges(rows: list[dict], name_key: str, qn_key: str, file_key: str) -> list[_EdgeGroup]:
    """Partition *rows* by the distinct symbol they belong to, preserving the backend's row order.

    Keyed on the qualified name AND the file: one file legitimately holds two symbols with the same
    bare name (a method on two classes), and a row carrying neither still has to land somewhere
    rather than being dropped."""
    groups: dict[tuple[str, str], _EdgeGroup] = {}
    for r in rows:
        file = str(r.get(file_key) or "")
        qn_raw = str(r.get(qn_key) or "")
        label = _strip_project_prefix(qn_raw, may_be_filename=False) or str(r.get(name_key) or "")
        key = (label, file)
        if key not in groups:
            groups[key] = _EdgeGroup(label=label, qn_raw=qn_raw, file=file, rows=[])
        groups[key].rows.append(r)
    return list(groups.values())
