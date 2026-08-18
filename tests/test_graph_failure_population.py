"""D1 — a graph query that did not return must never be reported as "nothing calls this".

`lsp.py:333-342` already made this check for the LSP engine: a backend call that failed is not the
same fact as "asked, and there is nothing" — the first says nothing about the code, the second is
an answer about it. The graph engine never carried that check, so `callers` on a real function with
zero callers, `callers` on a symbol that does not exist, and `callers` on a symbol whose backend
call outright failed all rendered byte-identical envelopes: `result: null`, `reason: "not-in-
graph"`, the same "refresh with: codeintel index" hint. That is the permissive direction — an agent
reads it as "safe to delete".

Enumerated by REFLECTION over `GraphProvider`, never a hand-typed op list — the same reasoning
`test_every_lsp_op_survives_a_backend_that_answers_nothing` already uses for the LSP engine, so an
op added next year is covered without anyone remembering to add a case.
"""
from __future__ import annotations

import threading

from codeintel.outcome import Missing
from codeintel.providers.graph import GraphProvider, ProjectLookup, ProjectResolution

# Ops `_dispatch` does not route as an ordinary user-facing op, recorded as DATA rather than
# silently skipped — a hand-typed exclusion list without a reason is exactly the domain-shrinking
# this project keeps re-learning not to do.
_NOT_A_USER_OP = {
    "deadcode": "withdrawn pending a precision measurement (graph.py _WITHDRAWN_OPS)",
}


def _gp(*, run=None, query_rows=None) -> GraphProvider:
    """A GraphProvider whose resolution always succeeds, with the backend seam(s) stubbed."""
    gp = GraphProvider.__new__(GraphProvider)
    gp.available = True                                               # type: ignore[attr-defined]
    gp._cmd = "stub"                                                   # type: ignore[attr-defined]
    gp._saw_unparsable = False                                         # type: ignore[attr-defined]
    gp._pending_gaps = ()                                              # type: ignore[attr-defined]
    gp._project_cache = {}                                             # type: ignore[attr-defined]
    gp._negative_until = {}                                            # type: ignore[attr-defined]
    gp._project_cache_lock = threading.Lock()                          # type: ignore[attr-defined]
    gp._resolve_project = lambda root: ProjectResolution(               # type: ignore[method-assign]
        name="proj", matched_root=root, scope="exact")
    gp._lookup_project = lambda root: ProjectLookup(                    # type: ignore[method-assign]
        ProjectResolution(name="proj", matched_root=root, scope="exact"), "ok")
    if run is not None:
        gp._run = run                                                  # type: ignore[method-assign]
    if query_rows is not None:
        gp._query_rows = query_rows                                    # type: ignore[method-assign]
    return gp


def _fail_backend(gp: GraphProvider, missing: Missing):
    """Emulates a total backend failure exactly as the real `_run` does (graph.py:576-578)."""

    def _run(method, payload, timeout_ms):
        gp._last_failure = missing
        return None

    return _run


# --------------------------------------------------------------------------------------------- #
# T4 — no graph op reports a backend failure as an absence.
# --------------------------------------------------------------------------------------------- #

def test_no_graph_op_reports_a_backend_failure_as_an_absence():
    ops = sorted(n[len("_op_"):] for n in vars(GraphProvider) if n.startswith("_op_"))
    assert len(ops) >= 8, f"op domain looks broken, only found: {ops}"

    for missing in (
        Missing("backend-error", "the graph backend did not answer"),
        Missing("timeout", "the graph backend did not respond within the time budget"),
    ):
        for op in ops:
            gp = _gp()
            gp._run = _fail_backend(gp, missing)                       # type: ignore[method-assign]

            env = gp.build_result(op, "make_widget", [], 30000, "/tmp/x")
            assert env["ok"] is True, (op, env)

            if op in _NOT_A_USER_OP:
                assert env.get("reason") == "op-withdrawn", (op, env)
                continue

            assert env.get("reason") != "unsupported-op", (
                f"{op}: an _op_ method with no dispatch route is itself a finding — {env}"
            )

            hint = env.get("hint") or ""
            assert "is not in the graph index" not in hint, (op, env)

            result = env.get("result")
            if result is None:
                reason = env.get("reason")
                assert reason in {
                    "backend-error", "timeout", "unparsable", "backend-incompatible",
                }, (op, missing.kind, env)
                assert reason not in {"not-in-graph", "no-result"}, (op, missing.kind, env)
            else:
                assert env.get("confidence") == "partial", (op, env)
                gaps = env.get("gaps") or []
                assert any(g.get("section") == "backend" for g in gaps), (op, env)
                assert "Incomplete:" in result, (op, env)


# --------------------------------------------------------------------------------------------- #
# T5 — an answer emptied by our own filter is not reported as "not in graph".
# --------------------------------------------------------------------------------------------- #

def test_an_answer_emptied_by_our_own_filter_is_not_reported_as_not_in_graph():
    def _rows(cypher, project, timeout_ms):
        return [{
            "b.name": "write",
            "b.qualified_name": "preload.write",
            "b.file_path": "studio/src/main/preload/index.ts",
            "type(c)": "CALLS",
            "a.file_path": "src/board_mirror.py",
        }]

    gp = _gp(query_rows=_rows)
    env = gp.build_result("callees", "write_board_mirror", [], 30000, "/tmp/x")

    assert env["ok"] is True
    assert env["result"] is not None
    assert "(0)" in env["result"]
    assert env.get("confidence") == "partial"
    gaps = env.get("gaps") or []
    assert any(g.get("kind") == "name-collisions-dropped" for g in gaps), gaps
    assert "reason" not in env


# --------------------------------------------------------------------------------------------- #
# T6 — a true negative and a failure are distinguishable.
# --------------------------------------------------------------------------------------------- #

def test_a_true_negative_and_a_failure_are_distinguishable():
    def _run_valid_empty(method, payload, timeout_ms):
        return {"columns": ["a.name", "a.qualified_name", "a.file_path", "type(c)"], "rows": []}

    gp_true_negative = _gp(run=_run_valid_empty)
    env_true_negative = gp_true_negative.build_result("callers", "nope", [], 30000, "/tmp/x")

    gp_failure = _gp()
    gp_failure._run = _fail_backend(                                   # type: ignore[method-assign]
        gp_failure, Missing("backend-error", "the graph backend did not answer"))
    env_failure = gp_failure.build_result("callers", "nope", [], 30000, "/tmp/x")

    assert env_true_negative.get("reason") == "not-in-graph"
    assert env_failure.get("reason") == "backend-error"
    assert env_true_negative != env_failure


# --------------------------------------------------------------------------------------------- #
# T7 — the PRODUCER half. The tests above stub `_run` itself and assert the ops consume a recorded
# failure correctly. That leaves the other half of the invariant unguarded: nothing checked that the
# REAL `_run` records one. Deleting `self._last_failure = ...` from graph.py left every test in this
# file green while every op silently reverted to answering "not-in-graph" for a dead backend — the
# exact half-population blind spot this file was written to prevent, reproduced inside it.
#
# `_fail_backend` above even carries the comment "emulates a total backend failure exactly as the
# real `_run` does (graph.py:576-578)". A comment asserting fidelity to an implementation is not a
# check on it; that is the prose-as-enforcement habit the project has already been burned by twice.
# So stub one level LOWER — at the transports `_run` drives — and let the real `_run` run.
# --------------------------------------------------------------------------------------------- #

def _run_only_provider() -> GraphProvider:
    gp = GraphProvider.__new__(GraphProvider)
    gp.available = True                                                # type: ignore[attr-defined]
    gp._cmd = "stub"                                                   # type: ignore[attr-defined]
    gp._saw_unparsable = False                                         # type: ignore[attr-defined]
    gp._last_failure = None                                            # type: ignore[attr-defined]
    return gp


def test_the_real_run_records_why_every_transport_failure_happened():
    """Each distinguishable failure of the real `_run` must leave a `Missing` behind.

    Without this, the consumer-side guards in this file certify a contract whose producer can be
    deleted underneath them."""
    cases = [
        ("both transports fail", "_FAIL", "_FAIL"),
        ("stdin unparsable", "_UNPARSABLE", None),
        ("fallback unparsable", "_FAIL", "_UNPARSABLE"),
    ]
    for label, stdin_out, raw_out in cases:
        gp = _run_only_provider()
        _S = {"_FAIL": GraphProvider._FAIL, "_UNPARSABLE": GraphProvider._UNPARSABLE}
        gp._run_stdin = lambda m, b, t, _s=stdin_out, _m=_S: _m[_s]     # type: ignore[method-assign]
        if raw_out is not None:
            gp._run_rawjson = lambda m, b, t, _s=raw_out, _m=_S: _m[_s]  # type: ignore[method-assign]
        out = gp._run("query_graph", {"project": "p"}, 5000)
        assert out is None, (label, out)
        assert isinstance(gp._last_failure, Missing), (
            f"{label}: the real _run returned None without recording WHY. Every op downstream will "
            f"now render this as an absence in the repository."
        )
        assert gp._last_failure.describe().strip(), label


def test_a_successful_run_records_no_failure():
    """The counterpart: a working backend must not leave a stale Missing behind, or every later
    answer in the same query is wrongly downgraded to partial."""
    gp = _run_only_provider()
    gp._run_stdin = lambda m, b, t: {"columns": [], "rows": []}        # type: ignore[method-assign]
    out = gp._run("query_graph", {"project": "p"}, 5000)
    assert out == {"columns": [], "rows": []}
    assert gp._last_failure is None
