from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable
from typing_extensions import NotRequired, TypedDict


class Result(TypedDict):
    ok: bool
    op: str
    target: str
    result: Optional[Any]
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
    hint: Optional[str] = None,
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
