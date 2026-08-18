"""A typed result for internal seams, so a failure stays a failure all the way up.

Every provider-internal helper used to return ``X | None``, and ``None`` there meant five
different things: never asked · timed out · the backend errored · the payload did not parse ·
genuinely empty. Callers could not tell them apart, so they picked the cheapest reading — and the
cheapest reading of "no references came back" is "this symbol has no references", which is the
permissive answer and the wrong one.

That collapse is what produced the worst bug in the 2026-08-17 evaluation: a cold language server
timed out, ``_call_tool`` returned ``None``, and four conversions later the caller rendered
``## References\\n(none)`` — a confident false statement, with no ``reason`` and no ``hint``,
byte-identical to a true one.

``graph.py`` had already worked this out and re-invented a fix for it three separate times in one
module (``_FAIL``/``_UNPARSABLE``, ``ProjectLookup``, and ``_search_symbols`` returning ``None``
vs ``[]`` on purpose). ``lsp.py`` had none of them, which is exactly where the critical bug lived.
This is that abstraction, written once.

Rule: a helper returns ``Missing`` when it could not answer, and ``Ok`` when it did — including
``Ok`` of an empty collection, which is a real and useful answer meaning "asked, and there is
nothing". Never conflate the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeAlias, TypeVar

T = TypeVar("T")

# Why a call produced no value. These strings reach the caller inside a gap note, so they are
# written to be read by a person or an agent, not only matched by code.
MissingKind = Literal[
    "not-asked",      # a precondition was absent, so the call was never made
    "timeout",        # the backend did not answer inside the budget
    "backend-error",  # the backend answered, and the answer was an error
    "unparsable",     # the backend answered, and the payload could not be read
    "unsupported",    # this backend cannot answer this question at all
]

_DETAIL: dict[str, str] = {
    "not-asked": "a precondition for this lookup was missing, so it was never requested",
    "timeout": "the backend did not respond within the time budget",
    "backend-error": "the backend returned an error instead of an answer",
    "unparsable": "the backend's response could not be parsed",
    "unsupported": "this engine cannot answer that",
}


@dataclass(frozen=True)
class Ok(Generic[T]):
    """The call answered. ``value`` may legitimately be empty."""

    value: T


@dataclass(frozen=True)
class Missing:
    """The call did not answer, and this is why. Never rendered as a value."""

    kind: MissingKind
    detail: str = ""
    retry_after_s: float | None = None

    def describe(self) -> str:
        """One caller-facing sentence. Falls back to a default phrasing per kind so that every
        Missing carries something readable even when the call site passed no detail."""
        return self.detail or _DETAIL.get(self.kind, "this lookup did not complete")


Outcome: TypeAlias = "Ok[T] | Missing"


def is_missing(outcome: object) -> bool:
    """True when an outcome represents a failure to answer. Kept as a helper so call sites read
    as intent rather than as an isinstance check against an implementation detail."""
    return isinstance(outcome, Missing)
