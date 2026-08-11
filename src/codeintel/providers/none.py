from __future__ import annotations

from codeintel.provider import Result, safe_null_result


class NoneProvider:
    """Always returns a safe-null Result. Never raises."""

    def build_result(
        self,
        op,
        target,
        files,
        budget,
        project_root,
    ) -> Result:
        try:
            op = str(op or "")
            target = str(target or "")
            return safe_null_result(op, target, engine="none", reason="no-engine")
        except Exception:
            return {
                "ok": True,
                "op": str(op or ""),
                "target": str(target or ""),
                "result": None,
                "engine": "none",
                "cached": False,
                "reason": "no-engine",
            }
