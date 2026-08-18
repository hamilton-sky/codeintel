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
    reason: NotRequired[str]
    hint: NotRequired[str]
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
    r: Result = {
        "ok": True,
        "op": str(op or ""),
        "target": str(target or ""),
        "result": None,
        "engine": engine,
        "cached": False,
        "reason": reason,
    }
    # Optional actionable breadcrumb (e.g. "not indexed → run codeintel index"); emit the key
    # only when set, exactly like `reason`, so envelope-shape tests stay unaffected.
    if hint is not None:
        r["hint"] = hint
    return r


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

    Null results are left alone: `reason` already carries the whole story there, and stamping them
    would imply a body exists to be partial about.
    """
    try:
        if result.get("result") is None:
            return result
        items = [g for g in (gaps or ()) if isinstance(g, dict)]
        out: Result = {**result, "confidence": "partial" if items else "complete"}
        if items:
            out["gaps"] = items
        return out
    except Exception as exc:
        log_swallowed("attach_confidence", exc)
        return result
