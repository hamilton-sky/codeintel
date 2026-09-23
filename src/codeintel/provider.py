from __future__ import annotations

import logging
import os
import traceback
from typing import Any, NotRequired, Protocol, runtime_checkable

from typing_extensions import TypedDict

_logger = logging.getLogger("codeintel")
_DEBUG = os.environ.get("CODEINTEL_DEBUG", "").strip().lower() in ("1", "true", "on", "yes")


def log_swallowed(where: str, exc: BaseException) -> None:
    """Record an exception the never-raise contract is about to swallow. Quiet by default so the
    contract stays silent in normal use; set ``CODEINTEL_DEBUG=1`` to surface a full traceback when
    diagnosing why a query came back as a safe-null. Never raises — logging failures are ignored."""
    try:
        if _DEBUG:
            _logger.warning("codeintel swallowed error in %s: %s\n%s", where, exc, traceback.format_exc())
        else:
            _logger.debug("codeintel swallowed error in %s: %s", where, exc)
    except Exception:
        pass


class Result(TypedDict):
    ok: bool
    op: str
    target: str
    result: Any | None
    engine: str
    cached: bool
    # Transport success (``ok``) is deliberately separate from whether the question was answered.
    # This field lets integrations branch without reverse-engineering ``reason`` and ``gaps``.
    outcome: NotRequired[str]
    reason: NotRequired[str]
    hint: NotRequired[str]
    retry_after_s: NotRequired[float]
    # Set when the answer was served while a reindex for its project was still running, i.e. it
    # reflects the last COMPLETED index rather than the current source. Optional, and the MCP
    # tools deliberately return plain `dict` so this never becomes a required schema field.
    reindexing: NotRequired[bool]
    # How much of the answer the engine vouches for. A non-null `result` used to be an implicit
    # promise of completeness, which is how a timed-out reference lookup shipped as "(none)".
    #   "complete" — ran; the engine stands behind the whole body
    #   "partial"  — ran; produced a body; a NAMED part of it is known to be missing (see `gaps`)
    # Absent on null results, where `reason` already carries the whole story.
    confidence: NotRequired[str]
    # The answer's rows as FIELDS, for an integration that must branch rather than read. Every
    # value is already stated in `result`'s prose; this is the same facts in a shape that does not
    # require parsing markdown this project reserves the right to reword. Present only on ops that
    # produce rows, and only when they produced some.
    #   rows[]   {relation, name, qualified_name, file, module_scope, edge, verified, evidence,
    #             strategy, confidence, why}
    #   evidence {verified, possible, unstated, returned, total, truncated,
    #             safe_for_destructive}
    # `rows` are exactly the rows the body printed, in the order it printed them — verified first.
    # `relation` is `caller` or `callee`, which `impact` needs because it answers both in one body.
    # `evidence.total` is None when the query hit the BACKEND's row cap: we know it had at least
    # `returned`, and reporting that as the total is how a capped list comes to read as a whole one.
    # It is a number when rows were withheld by our own candidate cap — those are in hand, so
    # `total > returned` and `truncated` is still true.
    rows: NotRequired[list[dict[str, Any]]]
    evidence: NotRequired[dict[str, Any]]
    # What this answer can be USED for, in one word — `evidence`, `discovery` or `advisory`. The
    # readiness doc's "distinguish discovery from proof", as a field rather than as a convention a
    # reader has to have absorbed. Stamped on every answered envelope by `attach_confidence`.
    evidence_class: NotRequired[str]
    # One entry per part of the answer that could not be retrieved:
    # {"section": str, "kind": str, "detail": str, "retry_after_s": float?}. Emitted only when
    # non-empty. The same fact is always rendered into `result` too, because that is the field an
    # agent actually reads.
    gaps: NotRequired[list[dict[str, Any]]]


@runtime_checkable
class CodeProvider(Protocol):
    """Implementors MUST never raise."""

    def build_result(
        self,
        op: str,
        target: str,
        files: list[str],
        budget: int,
        project_root: str,
    ) -> Result | None: ...


def safe_null_result(
    op: Any,
    target: Any,
    engine: str = "none",
    reason: str = "no-engine",
    hint: str | None = None,
) -> Result:
    failed_reasons = {
        "backend-error", "backend-incompatible", "boot-failed", "error", "gateway-error",
        "handler-error", "index-failed", "provider-error", "query-failed", "timeout",
        "unparsable",
    }
    unavailable_reasons = {
        "backend-unreachable", "engine-unavailable", "engines-unavailable", "indexing-in-progress",
        "index-stale", "no-engine", "no-project-root", "no-target",
        "op-not-allowed-for-role", "op-not-supported", "op-withdrawn", "project-not-indexed",
        "project-not-indexed-standalone", "root-not-allowed-for-role", "source-unreadable",
        "unknown-engine", "unsupported-op", "warming",
    }
    not_found_reasons = {
        "below-floor", "no-edges", "no-index", "no-result", "not-found", "not-in-graph",
    }
    if reason in failed_reasons:
        outcome = "failed"
    elif reason in unavailable_reasons:
        outcome = "unavailable"
    elif reason in not_found_reasons:
        outcome = "not_found"
    else:
        # New failure reasons must fail closed.  Defaulting every unknown string to ``not_found``
        # turned a newly introduced ``source-unreadable`` reason into an assertion that the symbol
        # was absent — exactly the ambiguity the explicit outcome field was added to remove.
        outcome = "failed"
    r: Result = {
        "ok": True,
        "op": str(op or ""),
        "target": str(target or ""),
        "result": None,
        "engine": engine,
        "cached": False,
        "outcome": outcome,
        "reason": reason,
    }
    # Optional actionable breadcrumb (e.g. "not indexed → run codeintel index"); emit the key
    # only when set, exactly like `reason`, so envelope-shape tests stay unaffected.
    if hint is not None:
        r["hint"] = hint
    return r


# ── What an answer can be used for ────────────────────────────────────────────────────────────
#
# Three words, and the distinction they draw is the one an agent gets wrong for free: a ranked list
# of plausible matches and a resolved binding are both "results", both render as rows, and only one
# of them settles anything. `code.query`'s own tool description carries the same three words for
# each op, because choosing a tool happens before any envelope exists.
#
# ADVISORY is the default for an unknown op on purpose. A new op that nobody classified is not
# thereby proof of anything, and the failure mode of guessing high here ends in a deletion.
EVIDENCE = "evidence"
DISCOVERY = "discovery"
ADVISORY = "advisory"

# The BEST an op's answer can be, before its own contents are read. Two ops share a ceiling and
# reach different classes on the same repository, which is the whole reason this is not a constant
# printed per op: `callers` over an import-resolved symbol is evidence, and `callers` over a bare
# name the index does not own is a list of leads wearing the same heading.
_OP_CEILING: dict[str, str] = {
    # Ranked or matched, never resolved. Not a degraded form of evidence — a different question.
    "search": DISCOVERY, "pattern": DISCOVERY, "overview": DISCOVERY, "hotspots": DISCOVERY,
    "changed": DISCOVERY, "changes": DISCOVERY,
    # A language server resolved a real binding; a graph edge op when every row followed one.
    "symbol": EVIDENCE, "callers": EVIDENCE, "callees": EVIDENCE,
    # Advisory whatever their rows say. `impact` and `context` are blast-radius JUDGEMENTS assembled
    # from two traversals plus non-call reference edges, and `chain`'s hops are heuristic. Their
    # rows still carry per-row `verified`, so an agent that wants the evidence-grade subset filters
    # for it — which is a narrower and more honest claim than the whole answer being evidence.
    "impact": ADVISORY, "context": ADVISORY, "chain": ADVISORY,
}


def _evidence_class(result: Result, partial: bool) -> str:
    """Which of the three this answer is, decided by the answer and not only by the op it came from.

    An op-keyed constant would be the same word on every `callers` result, including the 48-row one
    in which two rows were callers — a label true of the op and false of the answer, which is the
    substitution this repository keeps finding. So the ceiling comes from the op and the verdict
    comes from the rows: `safe_for_destructive` is already "every row followed a real binding and
    nothing is disclosed missing", and that is exactly what makes a row list proof.
    """
    ceiling = _OP_CEILING.get(str(result.get("op") or ""), ADVISORY)
    if ceiling is not EVIDENCE:
        return ceiling
    # Proof and "a named part of this answer is missing" cannot be the same envelope, whatever the
    # rows say. Checked FIRST rather than left to `safe_for_destructive`, which happens to include
    # an empty gap list today: relying on that would make this rule a property of another field's
    # current definition instead of a rule. The readiness doc's "evidence is incomplete" case, and
    # incomplete evidence is advice.
    if partial:
        return ADVISORY
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        return EVIDENCE if evidence.get("safe_for_destructive") else ADVISORY
    # No row summary to consult. Exactly one answer earns proof without one: a language server's
    # `symbol`, which resolved a real definition and its references by construction. Everything
    # else with no summary is advice — and that used to be the default, not the exception. A
    # `--engine both` fan-out of `callers` carries no `evidence` by design (see
    # `Gateway._fan_out`), so its concatenated bodies, name-matched graph rows included, were
    # stamped `evidence`; so was a graph `callers` answer whose summary was withheld, and so would
    # be any future engine that answers `callers` in prose. Failing closed means a new shape has to
    # be argued up to proof rather than argued down from it.
    if result.get("op") == "symbol" and result.get("engine") == "lsp":
        return EVIDENCE
    return ADVISORY


def attach_confidence(result: Result, gaps: Any = ()) -> Result:
    """Stamp an answered envelope with how much of it the engine vouches for.

    Every engine must call this, and that is the point. `confidence` was introduced on the LSP
    provider alone, which reproduced in miniature the defect it was added to fix: the instructions
    told callers to check a field that two of three engines never set, and an ABSENT field is
    ambiguous — "complete" and "this engine does not report" look identical. A contract that only
    one implementation honours is not a contract.

    `gaps` is a sequence of {"section", "kind", "detail", "retry_after_s"?} dicts. Empty ⇒
    `complete`; non-empty ⇒ `partial`, and the caller is expected to have said the same thing in
    the body text, because that is the field an agent actually reads.

    It also stamps `evidence_class`, for the same reason and in the same place: a second contract
    that only one engine honoured would reproduce this one's original defect exactly. Every engine
    already funnels its answered envelope through here, so there is no second site to forget.

    Null results are left alone: `reason` already carries the whole story there, and stamping them
    would imply a body exists to be partial about.
    """
    try:
        if result.get("result") is None:
            return result
        items = [g for g in (gaps or ()) if isinstance(g, dict)]
        out: Result = {
            **result,
            "confidence": "partial" if items else "complete",
            "outcome": "partial" if items else "answered",
            "evidence_class": _evidence_class(result, bool(items)),
        }
        if items:
            out["gaps"] = items
        return out
    except Exception as exc:
        log_swallowed("attach_confidence", exc)
        return result
