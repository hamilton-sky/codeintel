"""`codeintel doctor` — per-engine health + index status, with the fix for each gap."""

from typing import Any

from codeintel.commands._common import emit, never_raise, resolve_root


@never_raise("doctor unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel import doctor as _doctor

    report = _doctor.run_doctor(resolve_root(args), deep=args.deep)
    emit(report, as_json=args.json, render=_doctor.render_doctor_text)
    # Exit non-zero when a repo-critical engine is unhealthy, so scripts/CI can gate on it.
    return 0 if report.get("summary", {}).get("healthy") else 1
