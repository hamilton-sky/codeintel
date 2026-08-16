"""`codeintel query` — one question against the gateway: search, callers, callees, impact, chain."""

import json
import sys
import time
from typing import Any

from codeintel.commands._common import never_raise, resolve_root
from codeintel.provider import Result, safe_null_result

# How long a one-shot CLI process will wait for the LSP session to finish booting before giving up
# and reporting whatever the gateway last said.
_WARMING_TIMEOUT_S = 45.0


def run(args: Any) -> int:
    """`--json` promises parseable stdout, so its failures must be JSON too.

    `never_raise` prints a plain-text line on stdout, which under `--json` hands a `| jq` consumer
    a parse error alongside exit 0 — the worst combination, since the pipeline reports success.
    Route the two output modes to their own never-raise handlers instead of sharing one."""
    if getattr(args, "json", False):
        return _run_json(args)
    return _run_text(args)


def _run_json(args: Any) -> int:
    try:
        result = _query(args)
    except Exception as exc:
        # A safe-null envelope, the same shape every other failure produces — stdout stays valid
        # JSON whatever went wrong.
        result = safe_null_result(str(getattr(args, "op", "") or ""),
                                  str(getattr(args, "target", "") or ""), reason=str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


@never_raise("No result (reason: {exc})")
def _run_text(args: Any) -> int:
    result = _query(args)
    value = result.get("result")
    if value is not None:
        print(value)
    else:
        print(f"No result (reason: {result.get('reason', 'unknown')})")
        hint = result.get("hint")
        if hint:
            print(f"  hint: {hint}", file=sys.stderr)
    return 0


def _query(args: Any) -> Result:
    from codeintel import server

    project_root = resolve_root(args)
    engine = args.engine if args.engine != "auto" else None
    gw = server._build_gateway()

    def _run_query() -> Result:
        return gw.query(
            op=args.op,
            target=args.target,
            engine=engine,
            role="",
            project_root=project_root,
        )

    result = _run_query()

    # The LSP engine warms a serena session in a background thread and returns reason:warming on
    # the first call. A one-shot CLI process would otherwise always exit on 'warming' (the
    # subprocess dies with it). Wait — bounded, never-raise — for the session to boot, re-querying
    # the same gateway (session is cached per root).
    if result.get("result") is None and result.get("reason") == "warming":
        print("(lsp warming up — waiting for the language server...)", file=sys.stderr)
        deadline = time.monotonic() + _WARMING_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(0.5)
            result = _run_query()
            if result.get("result") is not None or result.get("reason") != "warming":
                break

    return result
