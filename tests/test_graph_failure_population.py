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

from codeintel.graph_backend import BackendClient
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
    gp._backend = BackendClient.__new__(BackendClient)                 # type: ignore[attr-defined]
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

def _run_only_provider() -> BackendClient:
    # `_run`/`_run_stdin`/`_run_rawjson`/`_last_failure` now live on `BackendClient` (graph_backend.py)
    # — this builds one directly, since it's the true owner of the orchestration under test.
    backend = BackendClient.__new__(BackendClient)
    backend.available = True                                           # type: ignore[attr-defined]
    backend._cmd = "stub"                                               # type: ignore[attr-defined]
    backend._saw_unparsable = False                                     # type: ignore[attr-defined]
    backend._last_failure = None                                        # type: ignore[attr-defined]
    return backend


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
        backend = _run_only_provider()
        _S = {"_FAIL": BackendClient._FAIL, "_UNPARSABLE": BackendClient._UNPARSABLE}
        backend._run_stdin = lambda m, b, t, _s=stdin_out, _m=_S: _m[_s]     # type: ignore[method-assign]
        if raw_out is not None:
            backend._run_rawjson = lambda m, b, t, _s=raw_out, _m=_S: _m[_s]  # type: ignore[method-assign]
        out = backend._run("query_graph", {"project": "p"}, 5000)
        assert out is None, (label, out)
        assert isinstance(backend._last_failure, Missing), (
            f"{label}: the real _run returned None without recording WHY. Every op downstream will "
            f"now render this as an absence in the repository."
        )
        assert backend._last_failure.describe().strip(), label


def test_a_successful_run_records_no_failure():
    """The counterpart: a working backend must not leave a stale Missing behind, or every later
    answer in the same query is wrongly downgraded to partial."""
    backend = _run_only_provider()
    backend._run_stdin = lambda m, b, t: {"columns": [], "rows": []}        # type: ignore[method-assign]
    out = backend._run("query_graph", {"project": "p"}, 5000)
    assert out == {"columns": [], "rows": []}
    assert backend._last_failure is None


# --------------------------------------------------------------------------------------------- #
# T8 — a `callees` name-collision is resolved against ITS OWN caller row, not against the union of
# every caller family sharing the bare `target` name.
#
# `target` is a bare name, so the Cypher match can return edges from more than one distinct caller
# (a Python `target` and an unrelated TypeScript `target`). The collision filter used to build one
# set of families from ALL rows' `a.file_path` and check each callee against that set — so a `.ts`
# name-collision callee reached from the *Python* caller survived, because the set also contained
# `ts-js` from the unrelated TypeScript caller's own (legitimate) row. The row carries its own
# caller's file; the filter must use that, not the union.
# --------------------------------------------------------------------------------------------- #

def test_callees_cross_language_drop_is_resolved_per_row_not_by_the_caller_union():
    def _rows(cypher, project, timeout_ms):
        return [
            {  # real Python caller -> real Python callee: keep
                "b.name": "helper", "b.qualified_name": "pkg.helper",
                "b.file_path": "src/bar.py", "type(c)": "CALLS",
                "a.file_path": "src/foo.py",
            },
            {  # SAME Python caller -> a `.ts` name collision: must drop
                "b.name": "util", "b.qualified_name": "ui.util",
                "b.file_path": "ui/util.ts", "type(c)": "USAGE",
                "a.file_path": "src/foo.py",
            },
            {  # unrelated TypeScript caller sharing the bare name -> real TS callee: keep
                "b.name": "sibling", "b.qualified_name": "ui.sibling",
                "b.file_path": "ui/helper.ts", "type(c)": "CALLS",
                "a.file_path": "ui/main.ts",
            },
        ]

    gp = _gp(query_rows=_rows)
    env = gp.build_result("callees", "target", [], 30000, "/tmp/x")

    assert env["ok"] is True
    result = env["result"]
    assert result is not None
    assert "ui/util.ts" not in result, (
        "the .ts row reached from the Python caller must be dropped as a name collision, "
        "not kept because an UNRELATED TypeScript caller happens to share the bare name"
    )
    assert "src/bar.py" in result
    assert "ui/helper.ts" in result
    assert "(2)" in result
    assert env.get("confidence") == "partial"
    gaps = env.get("gaps") or []
    assert any(g.get("kind") == "name-collisions-dropped" for g in gaps), gaps


# --------------------------------------------------------------------------------------------- #
# T9 — a `callees` answer for a name several symbols share is a QUESTION, not a subtraction.
#
# `target` is a bare name, so one query can return edges out of several distinct symbols. The rows
# used to be flattened into one list and the ambiguous ones subtracted, reported as a "N dropped"
# gap: honest, but the caller still could not get the whole answer. `callees` feeds "is this symbol
# safe to change?", and for that decision "unknown" and "none" are opposite answers.
#
# Resolution comes first: a target may name the symbol it means, by qualified name or by file, using
# text the previous answer already printed. What genuine ambiguity remains is grouped and stated
# rather than dropped.
# --------------------------------------------------------------------------------------------- #

_THREE_HANDLERS = [
    {  # symbol 1 — the HTTP route
        "b.name": "parse_body", "b.qualified_name": "api.parse_body",
        "b.file_path": "src/api/body.py", "type(c)": "CALLS",
        "a.name": "handle", "a.qualified_name": "proj.api.routes.handle",
        "a.file_path": "src/api/routes.py",
    },
    {
        "b.name": "audit", "b.qualified_name": "api.audit",
        "b.file_path": "src/api/audit.py", "type(c)": "CALLS",
        "a.name": "handle", "a.qualified_name": "proj.api.routes.handle",
        "a.file_path": "src/api/routes.py",
    },
    {  # symbol 2 — the queue worker, a different function that happens to share the name
        "b.name": "dequeue", "b.qualified_name": "worker.dequeue",
        "b.file_path": "src/worker/queue.py", "type(c)": "CALLS",
        "a.name": "handle", "a.qualified_name": "proj.worker.main.handle",
        "a.file_path": "src/worker/main.py",
    },
    {  # symbol 3 — the front-end click handler, in another language entirely
        "b.name": "render", "b.qualified_name": "ui.render",
        "b.file_path": "ui/render.ts", "type(c)": "CALLS",
        "a.name": "handle", "a.qualified_name": "proj.ui.button.handle",
        "a.file_path": "ui/button.ts",
    },
]


def _callees(target: str, rows: list[dict]) -> dict:
    gp = _gp(query_rows=lambda cypher, project, timeout_ms: list(rows))
    return gp.build_result("callees", target, [], 30000, "/tmp/x")


def test_callees_of_a_shared_name_groups_the_symbols_instead_of_merging_them():
    env = _callees("handle", _THREE_HANDLERS)
    body = env["result"]

    assert body is not None
    # Nothing is dropped for being ambiguous: every callee of every `handle` is still here.
    for callee in ("api.parse_body", "api.audit", "worker.dequeue", "ui.render"):
        assert callee in body, f"{callee} was lost: {body}"
    assert "(4)" in body
    # And each one is attributed to the symbol it actually came from.
    for owner in ("api.routes.handle", "worker.main.handle", "ui.button.handle"):
        assert owner in body, f"{owner} is not named as a distinct symbol: {body}"
    assert "3 distinct symbols" in body
    # The ambiguity is machine-readable, not only prose.
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "target-ambiguous" for g in env["gaps"]), env["gaps"]
    # And the body says how to ask about one of them.
    assert "handle@src/api/routes.py" in body or "api.routes.handle" in body


def test_a_qualified_target_answers_for_exactly_one_symbol():
    env = _callees("worker.main.handle", _THREE_HANDLERS)
    body = env["result"]

    assert body is not None
    assert "worker.dequeue" in body                      # the one asked for
    assert "(1)" in body
    for other in ("api.parse_body", "api.audit", "ui.render"):
        assert other not in body, f"{other} belongs to a different `handle`: {body}"
    assert "Narrowed" in body
    assert not any(g["kind"] == "target-ambiguous" for g in env.get("gaps") or []), env.get("gaps")
    assert env["confidence"] == "complete"


def test_a_file_hint_answers_for_exactly_one_symbol():
    env = _callees("handle@src/api/routes.py", _THREE_HANDLERS)
    body = env["result"]

    assert body is not None
    assert "api.parse_body" in body and "api.audit" in body
    assert "(2)" in body
    for other in ("worker.dequeue", "ui.render"):
        assert other not in body, f"{other} belongs to a different `handle`: {body}"
    assert env["confidence"] == "complete"


def test_a_bare_filename_hint_is_enough():
    """The hint is text a human types, so a basename has to work — and if a basename is itself
    ambiguous, that is reported by the same grouping rather than resolved by guessing."""
    env = _callees("handle@routes.py", _THREE_HANDLERS)
    assert "api.parse_body" in env["result"]
    assert "worker.dequeue" not in env["result"]


def test_a_hint_that_matches_nothing_is_not_reported_as_having_no_callees():
    """The dangerous reading. "I could not find the symbol you named" and "that symbol calls
    nothing" are opposite answers, and the second is the one that gets code deleted."""
    env = _callees("handle@src/does/not/exist.py", _THREE_HANDLERS)
    body = env["result"]

    assert body is not None
    assert "(0)" not in body, f"a lookup that matched no symbol must not claim zero callees: {body}"
    assert "No symbol matching" in body and "has callees in this index" in body
    # It must not claim the symbol is absent from the INDEX — a symbol can be indexed and still have
    # no edge of this kind (`Group.invoke` has callees but no callers on a real repository).
    assert "not in this index" not in body
    # It names the symbols that DO carry the name, which is what makes a second attempt possible.
    for owner in ("api.routes.handle", "worker.main.handle", "ui.button.handle"):
        assert owner in body
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "target-hint-unmatched" for g in env["gaps"]), env["gaps"]


def test_a_truncated_callee_list_says_it_was_truncated():
    """The query caps rows. A list cut off at the cap reads exactly like a complete one, which is
    the same defect as a filtered list reading as complete."""
    from codeintel.providers.graph import _EDGE_ROW_LIMIT

    rows = [{
        "b.name": f"callee_{i}", "b.qualified_name": f"pkg.callee_{i}",
        "b.file_path": f"src/mod_{i}.py", "type(c)": "CALLS",
        "a.name": "wide", "a.qualified_name": "pkg.wide", "a.file_path": "src/wide.py",
    } for i in range(_EDGE_ROW_LIMIT)]

    env = _callees("wide", rows)
    body = env["result"]

    assert body is not None
    assert "pkg.callee_0" in body and f"pkg.callee_{_EDGE_ROW_LIMIT - 1}" in body   # non-vacuity
    assert "Truncated" in body
    assert str(_EDGE_ROW_LIMIT) in body
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "row-cap-reached" for g in env["gaps"]), env["gaps"]
    # A list one row short of the cap is a complete answer and must not be caveated.
    short = _callees("wide", rows[:-1])
    assert "Truncated" not in short["result"]
    assert short["confidence"] == "complete"


def test_callers_narrows_by_the_same_hint_so_impact_stays_coherent():
    """`impact` is callers + callees on one target. If only one half honoured a hint, a
    blast-radius answer would pair one symbol's callees with a different symbol's callers — and
    read as precise while being about two different functions."""
    callers_rows = [
        {"a.name": "serve", "a.qualified_name": "proj.api.app.serve",
         "a.file_path": "src/api/app.py", "type(c)": "CALLS",
         "b.qualified_name": "proj.api.routes.handle", "b.file_path": "src/api/routes.py"},
        {"a.name": "loop", "a.qualified_name": "proj.worker.run.loop",
         "a.file_path": "src/worker/run.py", "type(c)": "CALLS",
         "b.qualified_name": "proj.worker.main.handle", "b.file_path": "src/worker/main.py"},
    ]
    seq = iter([callers_rows, _THREE_HANDLERS])
    gp = _gp(query_rows=lambda cypher, project, timeout_ms: list(next(seq)))

    env = gp.build_result("impact", "handle@src/api/routes.py", [], 30000, "/tmp/x")
    body = env["result"]

    assert body is not None
    assert "api.app.serve" in body, "the caller of the symbol asked about must be there"
    assert "worker.run.loop" not in body, f"that calls a DIFFERENT `handle`: {body}"
    assert "api.parse_body" in body
    assert "worker.dequeue" not in body


def test_every_gap_callees_can_report_states_its_numbers_in_the_body_too():
    """`attach_confidence` says the engine "is expected to have said the same thing in the body
    text, because that is the field an agent actually reads" — a `gaps` entry alone is a promise
    kept to the JSON and broken to the reader.

    The gap-kind population is read out of `graph.py`'s AST rather than typed here, because a
    hand-typed list would keep passing on the day a new gap kind ships with no body text behind it —
    the defect class this project keeps re-learning (CHANGELOG 0.15.4/0.15.5).
    """
    import ast
    import pathlib
    import re

    source = pathlib.Path("src/codeintel/providers/graph.py").read_text(encoding="utf-8")
    declared: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_add_gap" and len(node.args) >= 2):
            continue
        section, kind = node.args[0], node.args[1]
        if not isinstance(kind, ast.Constant):
            continue
        # A call whose SECTION is a variable can land on `callees`, and the AST cannot tell whether
        # it does. Counted in, because requiring body text for a gap that might not be a callees gap
        # errs toward more disclosure — the safe direction here.
        if not isinstance(section, ast.Constant) or section.value == "callees":
            declared.add(str(kind.value))
    assert declared, "found no gap kinds in the source — the AST walk is broken, not the code"

    collision = [dict(_THREE_HANDLERS[0], **{"b.file_path": "src/api/notes.json"})]
    wide = [{"b.name": f"c{i}", "b.qualified_name": f"pkg.c{i}", "b.file_path": f"src/m{i}.py",
             "type(c)": "CALLS", "a.name": "wide", "a.qualified_name": "pkg.wide",
             "a.file_path": "src/wide.py"} for i in range(60)]
    scenarios = {
        "several symbols share the name": ("handle", _THREE_HANDLERS),
        "a row dropped as a collision": ("handle", _THREE_HANDLERS + collision),
        "every row dropped": ("handle", collision),
        "the hint matched nothing": ("handle@nowhere.py", _THREE_HANDLERS),
        "the row cap was reached": ("wide", wide),
    }

    seen: set[str] = set()
    for label, (target, rows) in scenarios.items():
        env = _callees(target, rows)
        body = str(env.get("result") or "")
        assert body, f"{label}: no body to check"
        for gap in env.get("gaps") or []:
            seen.add(str(gap["kind"]))
            assert env["confidence"] == "partial", f"{label}: gap without partial — {env}"
            for number in re.findall(r"\d+", str(gap["detail"])):
                assert number in body, (
                    f"{label}: gap {gap['kind']} reports {number} in `gaps` but not in the body a "
                    f"reader sees:\n{body}")

    assert declared <= seen, (
        f"these gap kinds are reachable in graph.py but no scenario here produces one, so nothing "
        f"checks that they reach the body: {sorted(declared - seen)}")


def test_a_filename_is_never_read_as_a_qualified_target():
    """`use-toast.ts` is a filename. Reading its last segment as the symbol name would send a query
    for `ts` — a symbol nobody asked about, on a repository where 985 names took exactly this shape
    (CHANGELOG 0.15.x, `_strip_project_prefix`). The extension population is derived from the
    source's own set, so a language added there is covered here without anyone remembering."""
    from codeintel.providers.graph import _FILE_EXTENSIONS, _parse_symbol_target

    for ext in sorted(_FILE_EXTENSIONS):
        name = f"use-toast.{ext}"
        parsed = _parse_symbol_target(name)
        assert parsed.name == name, f"{name} was split into a symbol and a qualifier"
        assert not parsed.qualified


def test_target_parsing_keeps_its_hands_off_what_is_not_a_hint():
    from codeintel.providers.graph import _parse_symbol_target

    plain = _parse_symbol_target("handle")
    assert (plain.name, plain.qualified, plain.file_hint) == ("handle", "", "")
    assert plain.narrowed is False

    # A leading `@` is a decorator, not a file hint, and a trailing one names no file.
    for odd in ("@app.route", "handle@", "@", ""):
        assert _parse_symbol_target(odd).file_hint == "", odd

    both = _parse_symbol_target("api.routes.handle@src/api/routes.py")
    assert (both.name, both.qualified, both.file_hint) == (
        "handle", "api.routes.handle", "src/api/routes.py")
    assert both.narrowed is True


def test_a_qualified_hint_must_match_whole_segments():
    """`routes.handle` naming `api.myroutes.handle` would silently answer about the wrong symbol,
    which is the failure this whole mechanism exists to prevent."""
    from codeintel.providers.graph import _qualified_name_matches

    assert _qualified_name_matches("proj.api.routes.handle", "routes.handle")
    assert _qualified_name_matches("proj.api.routes.handle", "api.routes.handle")
    assert not _qualified_name_matches("proj.api.myroutes.handle", "routes.handle")
    assert not _qualified_name_matches("", "routes.handle")
    # The prefix the backend adds is stripped for display, so the stripped form must match too:
    # that is the text the caller copied off the previous answer.
    assert _qualified_name_matches("my-repo.src.pkg.fn", "src.pkg.fn")


def test_a_file_hint_must_match_whole_path_segments():
    from codeintel.providers.graph import _file_path_matches

    assert _file_path_matches("src/api/routes.py", "routes.py")
    assert _file_path_matches("src/api/routes.py", "api/routes.py")
    assert _file_path_matches("src/api/routes.py", "src/api/routes.py")
    assert not _file_path_matches("src/api/myroutes.py", "routes.py")
    assert not _file_path_matches("", "routes.py")
    assert not _file_path_matches("src/api/routes.py", "")


# --------------------------------------------------------------------------------------------- #
# T10 — `callers` groups by the symbol being CALLED, for the same reason `callees` groups by the
# symbol doing the calling.
#
# `callers` was given the disambiguator but not the grouping, on the argument that a merged caller
# list over-reports and over-reporting is the safe direction for "is this safe to change?". That
# argument is about the SET, not about the answer: a reader told "3 callers of `invoke`" cannot tell
# that one of the three calls a different `invoke`, and believing a caller exists that does not is a
# wrong fact whichever direction it errs in. It also left `impact` internally inconsistent — one half
# grouped, the other flat, from one target.
# --------------------------------------------------------------------------------------------- #

_TWO_HANDLERS_CALLED = [
    {  # -> the HTTP route's `handle`
        "a.name": "serve", "a.qualified_name": "proj.api.app.serve",
        "a.file_path": "src/api/app.py", "type(c)": "CALLS",
        "b.name": "handle", "b.qualified_name": "proj.api.routes.handle",
        "b.file_path": "src/api/routes.py",
    },
    {  # -> the same `handle`, from somewhere else
        "a.name": "reload", "a.qualified_name": "proj.api.app.reload",
        "a.file_path": "src/api/app.py", "type(c)": "CALLS",
        "b.name": "handle", "b.qualified_name": "proj.api.routes.handle",
        "b.file_path": "src/api/routes.py",
    },
    {  # -> a DIFFERENT `handle` entirely
        "a.name": "loop", "a.qualified_name": "proj.worker.run.loop",
        "a.file_path": "src/worker/run.py", "type(c)": "CALLS",
        "b.name": "handle", "b.qualified_name": "proj.worker.main.handle",
        "b.file_path": "src/worker/main.py",
    },
]


def _callers(target: str, rows: list[dict]) -> dict:
    gp = _gp(query_rows=lambda cypher, project, timeout_ms: list(rows))
    return gp.build_result("callers", target, [], 30000, "/tmp/x")


def test_callers_of_a_shared_name_says_which_symbol_each_caller_calls():
    env = _callers("handle", _TWO_HANDLERS_CALLED)
    body = env["result"]

    assert body is not None
    for caller in ("api.app.serve", "api.app.reload", "worker.run.loop"):
        assert caller in body, f"{caller} was lost: {body}"      # nothing dropped
    assert "(3)" in body
    assert "2 distinct symbols" in body
    # Each caller is attributed to the symbol it actually calls.
    assert "api.routes.handle" in body and "worker.main.handle" in body
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "target-ambiguous" for g in env["gaps"]), env["gaps"]


def test_callers_of_an_unambiguous_name_is_left_flat():
    """The un-grouped rendering has to survive for the ordinary case, or every single-symbol answer
    pays for the ambiguous one with a heading it does not need."""
    env = _callers("handle", _TWO_HANDLERS_CALLED[:2])
    body = env["result"]

    assert body is not None
    assert "(2)" in body
    assert "###" not in body, f"one matched symbol needs no per-symbol sections: {body}"
    assert "distinct symbols" not in body
    assert env["confidence"] == "complete"
    assert not any(g["kind"] == "target-ambiguous" for g in env.get("gaps") or [])


def test_impact_groups_both_of_its_halves_or_neither():
    """`impact` is callers + callees on one target. Grouping one half and flattening the other is
    the inconsistency this closes: a reader would see the callees attributed per symbol and read the
    callers list as belonging to whichever one they were looking at."""
    seq = iter([_TWO_HANDLERS_CALLED, _THREE_HANDLERS])
    gp = _gp(query_rows=lambda cypher, project, timeout_ms: list(next(seq)))
    env = gp.build_result("impact", "handle", [], 30000, "/tmp/x")
    body = env["result"]

    assert body is not None
    callers_half, _, callees_half = body.partition("## Callees of")
    assert "distinct symbols" in callers_half, f"callers half not grouped:\n{callers_half}"
    assert "distinct symbols" in callees_half, f"callees half not grouped:\n{callees_half}"
    assert env["confidence"] == "partial"


def test_both_edge_ops_render_through_one_renderer():
    """The two ops must not drift apart on the drift-prone parts — the count in the heading, the
    ambiguity disclosure, the truncation note. Enumerated from the source rather than trusted: this
    is the same reasoning that made the repo-scan ops share `_render_scan`."""
    import ast
    import pathlib

    source = pathlib.Path("src/codeintel/providers/graph.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    for op in ("_op_callers", "_op_callees"):
        assert op in functions, op
        calls = [n for n in ast.walk(functions[op])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        rendered = {n.func.attr for n in calls}
        assert "_render_edge_answer" in rendered, (
            f"{op} builds its own answer instead of going through the shared renderer, so the "
            f"ambiguity and truncation disclosures can drift between the two ops")


# --------------------------------------------------------------------------------------------- #
# T11 — a module-scope pseudo-node is rendered as the LOCATION it is, never as a caller/callee that
# does not exist.
#
# The backend has no node for code that runs at module or class-body scope, so it hangs those edges
# off a whole-file container: a `File` node whose qualified name it synthesises as `<module>.__file__`,
# and/or a `Module` node named for the file. Rendered verbatim, `## Callers of invoke (4)` listed
# `src.click.core.__file__` and `src.click.decorators.__file__` as two of its four callers — half the
# count a reader trusts was a symbol nobody can open. The edge is real (module-scope code genuinely
# references the symbol), so it is relabelled to the file location rather than dropped: dropping would
# report a live, referenced function as uncalled, and on the pinned corpus 147 symbols are referenced
# ONLY this way.
# --------------------------------------------------------------------------------------------- #

def test_module_scope_node_classifier_reads_both_wire_shapes():
    """The label arrives JSON-encoded (`'["File"]'`) over the real wire and as a plain list from the
    mocked backend; both must classify, and a callable label or a missing one never must."""
    from codeintel.graph_render import _is_module_scope_node, _node_labels

    for container in ('["File"]', '["Module"]', ["File"], ["Module"], "File"):
        assert _is_module_scope_node(container), container
    for callable_label in ('["Function"]', '["Method"]', '["Class"]', '["Variable"]', ["Function"]):
        assert not _is_module_scope_node(callable_label), callable_label
    for absent in (None, "", "[]", []):
        assert not _is_module_scope_node(absent), absent
    assert _node_labels('["File","Module"]') == frozenset({"File", "Module"})


_INVOKE_WITH_MODULE_SCOPE = [
    {  # a real method caller — must survive untouched
        "a.name": "forward", "a.qualified_name": "proj.click.core.Context.forward",
        "a.file_path": "src/click/core.py", "labels(a)": '["Method"]', "type(c)": "CALLS",
        "b.name": "invoke", "b.qualified_name": "proj.click.core.Context.invoke",
        "b.file_path": "src/click/core.py",
    },
    {  # the File `__file__` pseudo-node for core.py's module scope
        "a.name": "core.py", "a.qualified_name": "proj.click.core.__file__",
        "a.file_path": "src/click/core.py", "labels(a)": '["File"]', "type(c)": "CALLS",
        "b.name": "invoke", "b.qualified_name": "proj.click.core.Context.invoke",
        "b.file_path": "src/click/core.py",
    },
    {  # the Module pseudo-node for the SAME file — the backend's second representation of one scope,
        # which must collapse into the File row rather than double-count it
        "a.name": "src/click/core.py", "a.qualified_name": "proj.click.core",
        "a.file_path": "src/click/core.py", "labels(a)": '["Module"]', "type(c)": "USAGE",
        "b.name": "invoke", "b.qualified_name": "proj.click.core.Context.invoke",
        "b.file_path": "src/click/core.py",
    },
    {  # module scope of a DIFFERENT file — a distinct row
        "a.name": "decorators.py", "a.qualified_name": "proj.click.decorators.__file__",
        "a.file_path": "src/click/decorators.py", "labels(a)": '["File"]', "type(c)": "CALLS",
        "b.name": "invoke", "b.qualified_name": "proj.click.core.Context.invoke",
        "b.file_path": "src/click/core.py",
    },
]


def test_callers_render_module_scope_nodes_as_locations_not_file_dunders():
    env = _callers("invoke", _INVOKE_WITH_MODULE_SCOPE)
    body = env["result"]

    assert body is not None
    # The fiction is gone: no synthetic `__file__` caller, no bare module rendered as a symbol.
    assert "__file__" not in body, body
    assert "proj.click.core [" not in body, f"a Module node rendered as a caller symbol: {body}"
    # The edges are kept, each rendered as the location it is.
    assert "module scope of src/click/core.py" in body
    assert "module scope of src/click/decorators.py" in body
    # The File and Module representations of core.py's ONE module scope collapse to ONE row.
    assert body.count("module scope of src/click/core.py") == 1, body
    # So the count is 1 (core.py) + 1 (decorators.py) + 1 (the real method) = 3, not the raw 4.
    assert "(3)" in body
    assert "Context.forward" in body, "the real method caller was lost"
    # A pure relabel drops nothing, so the answer stays complete — not a partial with a gap.
    assert env["confidence"] == "complete"
    assert not (env.get("gaps") or [])


def test_a_symbol_referenced_only_from_module_scope_is_not_reported_as_uncalled():
    """Why DROP was rejected. A symbol whose only reference is at module scope is real and reachable;
    dropping its one caller row would render `## Callers (0)`, the "safe to delete" answer. 147 symbols
    on the pinned corpus (`_check_iter`, `make_metavar`, `_param_memo`, …) are referenced only this
    way."""
    rows = [{
        "a.name": "core.py", "a.qualified_name": "proj.click.core.__file__",
        "a.file_path": "src/click/core.py", "labels(a)": '["File"]', "type(c)": "CALLS",
        "b.name": "_check_iter", "b.qualified_name": "proj.click.core._check_iter",
        "b.file_path": "src/click/core.py",
    }]
    env = _callers("_check_iter", rows)
    body = env["result"]

    assert body is not None
    assert env.get("reason") is None, "a referenced symbol must be answered, not reported as a miss"
    assert "(1)" in body and "(0)" not in body
    assert "module scope of src/click/core.py" in body
    assert "__file__" not in body
    assert env["confidence"] == "complete"


def test_callees_are_untouched_by_the_module_scope_pass():
    """The backend never emits a File/Module node as a callee — the callee side of every edge is a
    Function/Method/Class/Variable/Decorator — so the shared pass must be a no-op for `callees` and
    leave a real callee list exactly as it was."""
    rows = [{
        "b.name": "helper", "b.qualified_name": "pkg.helper", "b.file_path": "src/bar.py",
        "labels(b)": '["Function"]', "type(c)": "CALLS",
        "a.name": "wide", "a.qualified_name": "pkg.wide", "a.file_path": "src/wide.py",
    }]
    env = _callees("wide", rows)
    body = env["result"]

    assert body is not None
    assert "pkg.helper" in body
    assert "module scope of" not in body
    assert "(1)" in body
    assert env["confidence"] == "complete"


# --------------------------------------------------------------------------------------------- #
# T12 — the cross-language / non-code collision filter is now shared, so `callers` drops the same
# name-collision pollution `callees` always did.
#
# `callers X` matches by the bare name `X`, and the extractor emits an edge for a bare local name, so
# a `.ts` function or a `.json` node three files over that merely shares the name becomes a "caller".
# A call edge cannot cross a language family (`_LANG_FAMILIES`) and a data file defines no caller, so
# these are collisions, resolved per group against the CALLED symbol's language — the exact filter
# `callees` had, lifted to one shared helper both ops use.
# --------------------------------------------------------------------------------------------- #

_HANDLE_WITH_CALLER_COLLISIONS = [
    {  # a real Python caller of a Python target — keep
        "a.name": "serve", "a.qualified_name": "proj.api.app.serve",
        "a.file_path": "src/api/app.py", "labels(a)": '["Function"]', "type(c)": "CALLS",
        "b.name": "handle", "b.qualified_name": "proj.api.routes.handle",
        "b.file_path": "src/api/routes.py",
    },
    {  # a `.ts` function sharing the bare name — cross-language collision, drop
        "a.name": "handle", "a.qualified_name": "ui.button.handle",
        "a.file_path": "ui/button.ts", "labels(a)": '["Function"]', "type(c)": "USAGE",
        "b.name": "handle", "b.qualified_name": "proj.api.routes.handle",
        "b.file_path": "src/api/routes.py",
    },
    {  # a node in a data file — a JSON defines no caller, drop
        "a.name": "handle", "a.qualified_name": "config.handle",
        "a.file_path": "config/handlers.json", "labels(a)": '["Variable"]', "type(c)": "USAGE",
        "b.name": "handle", "b.qualified_name": "proj.api.routes.handle",
        "b.file_path": "src/api/routes.py",
    },
]


def test_callers_drops_cross_language_and_non_code_collisions():
    env = _callers("handle", _HANDLE_WITH_CALLER_COLLISIONS)
    body = env["result"]

    assert body is not None
    assert "api.app.serve" in body                       # the real caller survives
    assert "ui/button.ts" not in body, "a cross-language collision was reported as a caller"
    assert "handlers.json" not in body, "a data file was reported as a caller"
    assert "(1)" in body
    # Rule 5: the drop count and reason live in the body a reader sees, not only in `gaps`.
    assert "2 row(s) dropped" in body
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "name-collisions-dropped" for g in (env.get("gaps") or [])), env


def test_callers_emptied_only_by_collisions_is_not_reported_as_uncalled():
    """The dangerous reading again, on the caller side: a symbol whose only rows are collisions must
    say "found N, all filtered", never "0 callers" — the latter reads as safe to delete."""
    env = _callers("handle", _HANDLE_WITH_CALLER_COLLISIONS[1:])   # only the two collisions

    body = env["result"]
    assert body is not None
    assert env.get("reason") is None, "an answer WE emptied must not be reported as a repo absence"
    assert "(0)" in body
    assert "name-collision" in body.lower()
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "name-collisions-dropped" for g in (env.get("gaps") or [])), env


def test_a_genuine_cross_language_caller_is_kept_when_the_target_language_is_unknown():
    """The filter only drops when BOTH ends have a KNOWN, differing family — a builtin or synthetic
    target (no file, unknown family) must not cause every caller to be dropped as a collision."""
    rows = [{
        "a.name": "use", "a.qualified_name": "proj.ui.app.use", "a.file_path": "ui/app.ts",
        "labels(a)": '["Function"]', "type(c)": "CALLS",
        "b.name": "len", "b.qualified_name": "builtins.len", "b.file_path": "",
    }]
    env = _callers("len", rows)
    body = env["result"]
    assert body is not None
    assert "ui.app.use" in body, "a caller was dropped though the target's family is unknown"
    assert "(1)" in body
    assert env["confidence"] == "complete"
