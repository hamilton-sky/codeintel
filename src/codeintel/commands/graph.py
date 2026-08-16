"""`codeintel graph` — an interactive call-graph viewer (--html), or the graph as JSON."""

import json
from typing import Any

from codeintel.commands._common import never_raise, require_dir, resolve_root


# code=1: this command's job is to WRITE A FILE. Exiting 0 after failing to write it
# reports success to any `make`/CI step gating on $? while nothing was produced.
@never_raise("graph failed: {exc}", code=1)
def run(args: Any) -> int:
    from codeintel import grapher

    project_root = resolve_root(args)
    problem = require_dir(project_root, "graph")
    if problem:
        print(problem)
        return 1
    payload = grapher.build_graph_payload(project_root, limit=args.limit)
    nodes, edges = len(payload.get("nodes", [])), len(payload.get("edges", []))

    if not args.html:
        print(json.dumps(payload, indent=2))
        return 0

    out = args.out or "codeintel-graph.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(grapher.render_html(payload))
    print(f"Wrote {out}  ({nodes} nodes, {edges} edges) — open it in any browser")
    if not nodes:
        reason = payload.get("reason")
        if reason:
            print(f"  (graph empty: {reason} — run `codeintel doctor` to check the "
                  f"graph backend + index)")
        else:
            print("  (no internal call edges found for this repo)")
    return 0
