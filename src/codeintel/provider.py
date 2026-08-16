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
