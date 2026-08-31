"""Run the call-edge benchmark. `python bench/run.py [repo-key]`

The target list is STRATIFIED, not sampled. A random draw from a real repository is dominated by
easy cases — a uniquely-named function imported directly and called directly — and every engine
scores well on those. The disagreements in this project were all about the other cases, so those are
what the list is built from: names shared with a framework global, functions reached through a
re-export, functions only ever passed as a value, handlers dispatched by a framework and never called
at all.

That biases the absolute numbers DOWNWARD, deliberately. A benchmark whose population matches the
questions actually in dispute is more useful than one whose average looks reassuring, and the point
is to choose between engines rather than to produce a flattering headline.
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from score import run

PATHLY = "/Users/shammaihamilton/Documents/project/pathly-adapters"
SNITCH = "/Users/shammaihamilton/Documents/snitch-simulator"

# (file where it is DEFINED, symbol) — never a dotted string, because `src.pkg.mod.f` and
# `pkg.mod.f` name the same function and only one of them appears in any import statement. The
# oracle derives the importable name from the definition site.
REPOS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "pathly-adapters": (PATHLY, [
        # Plainly resolvable: direct import, direct call. The control group — an engine that gets
        # these wrong is broken, and one that only gets these right has not been tested.
        ("src/pathly_orchestrator/db/queries/flow_graph_ops.py", "replace_flow_graph"),
        ("src/pathly_orchestrator/db/queries/flow_graph_ops.py", "read_flow_nodes"),
        ("src/pathly_orchestrator/db/queries/flow_graph_ops.py", "read_flow_edges"),

        # Reached through a re-export chain (`flow_defs` re-exports from `flow_graph_ops`), which is
        # ordinary Python and defeated the oracle's own first version.
        ("src/pathly_orchestrator/db/queries/flow_graph_ops.py", "ensure_adapter_map_default"),
        ("src/pathly_orchestrator/db/queries/flow_graph_ops.py", "_assemble_from_parts"),
        ("src/pathly_orchestrator/db/queries/flow_graph_ops.py", "_decompose_flow_dict"),

        # Framework-dispatched: Flask view functions, never called by any Python code. Truth is
        # legitimately zero callers, so these test the opposite failure — an engine inventing one.
        ("src/pathly_orchestrator/http_server/blueprints/flows/defs.py", "list_flows"),
        ("src/pathly_orchestrator/http_server/blueprints/comms/messages_crud.py", "comms_delete"),

        # Short/common names, the population where `unique_name` binding does its damage.
        ("src/pathly_orchestrator/http_server/sse.py", "_broadcast"),
        ("src/pathly_orchestrator/runner/output.py", "_claude_tokens"),
    ]),
    "snitch-simulator": (SNITCH, [
        # Only ever PASSED, never invoked — `set_forward_fn(app.forward_released_item)`. Truth is
        # zero calls and two references, so this is the case that separates an engine which reports
        # the relationship it found from one which reports "no callers" and invites a deletion.
        ("services/simulator/src/snitch_simulator/proxy.py", "forward_released_item"),
        # Ordinary methods on the same class, as the control.
        ("services/simulator/src/snitch_simulator/proxy.py", "_strip_hop_by_hop"),
        ("services/simulator/src/snitch_simulator/proxy.py", "_content_length"),
        ("services/simulator/src/snitch_simulator/proxy.py", "_set_content_length"),
        # Module-level functions reached across packages.
        ("services/simulator/src/snitch_simulator/state.py", "FaultStore"),
        ("services/simulator/src/snitch_simulator/config.py", "load_config"),
    ]),
}


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "pathly-adapters"
    if key not in REPOS:
        print(f"unknown repo '{key}'; known: {', '.join(REPOS)}")
        return 2
    root, targets = REPOS[key]
    run(root, targets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
