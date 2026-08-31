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

import os
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from score import run

# Repository locations are read from the environment, with the author's own paths as the default.
# They were hardcoded, which made the one artifact that turns this project's accuracy arguments into
# arithmetic runnable on exactly one machine — so nobody else could reproduce a number, and nobody
# could re-run the table after a backend release to see whether it had moved.
#
#     CODEINTEL_BENCH_PATHLY=~/src/pathly-adapters python bench/run.py pathly-adapters
#
# `bench/fixtures/corpus` needs none of this: it is checked in, and `tests/test_bench_oracle.py`
# pins the oracle against it on every CI run.
PATHLY = os.path.expanduser(
    os.environ.get("CODEINTEL_BENCH_PATHLY",
                   "/Users/shammaihamilton/Documents/project/pathly-adapters"))
SNITCH = os.path.expanduser(
    os.environ.get("CODEINTEL_BENCH_SNITCH",
                   "/Users/shammaihamilton/Documents/snitch-simulator"))

# (file where it is DEFINED, symbol) — never a dotted string, because `src.pkg.mod.f` and
# `pkg.mod.f` name the same function and only one of them appears in any import statement. The
# oracle derives the importable name from the definition site.
# A TypeScript repository to measure. There is no default, because inventing target symbols for a
# codebase nobody here has read is exactly the kind of unfounded claim this benchmark exists to
# replace. Point it at a real one and list its disputed symbols below.
TS_REPO = os.path.expanduser(os.environ.get("CODEINTEL_BENCH_TS", ""))

CORPUS_TS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "corpus_ts")

REPOS: dict[str, tuple[str, list[tuple[str, str]], str]] = {
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
    ], "python"),
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
    ], "python"),

    # The checked-in TypeScript corpus. Small, and it is a SMOKE TEST of the arm end to end rather
    # than a measurement — 19 files written to have a known answer cannot say anything about a real
    # codebase. Its value is that the whole path runs without a private clone: oracle, scorer, and
    # both engines through codeintel's own envelope.
    "corpus-ts": (CORPUS_TS, [
        # Only ever PASSED, never invoked — the `forward_released_item` shape, in TypeScript.
        ("src/proxy.ts", "forwardReleasedItem"),
        # The framework global. THE case: 32 fabricated callers, and the reason this arm exists.
        ("src/proxy.ts", "describe"),
        # A name the tree installs on `globalThis`, where the module argument stops holding.
        ("src/proxy.ts", "legacyHelper"),
    ], "typescript"),

    # A real TypeScript repository, named by the environment. Fill in the disputed symbols.
    "typescript": (TS_REPO, [], "typescript"),
}


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "pathly-adapters"
    if key not in REPOS:
        print(f"unknown repo '{key}'; known: {', '.join(REPOS)}")
        return 2
    root, targets, language = REPOS[key]
    if key == "typescript" and not targets:
        print("The `typescript` slot has no targets yet.\n"
              "Set CODEINTEL_BENCH_TS to a real TypeScript repository and list its disputed\n"
              "symbols in REPOS — the stratification note above says which kinds to pick.\n"
              "To exercise the arm itself without a clone: python bench/run.py corpus-ts")
        return 2
    if not os.path.isdir(root):
        # Say so, rather than scoring every arm against an empty repository. A silent run of zeros
        # is the failure mode this benchmark keeps finding in the tools it measures.
        env = {"pathly-adapters": "CODEINTEL_BENCH_PATHLY",
               "snitch-simulator": "CODEINTEL_BENCH_SNITCH"}.get(key, "CODEINTEL_BENCH_TS")
        print(f"'{key}' is not checked out at {root}.\n"
              f"Point {env} at your clone, or run the checked-in corpus instead:\n"
              f"    pytest tests/test_bench_oracle.py")
        return 2
    run(root, targets, language=language)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
