"""The `graph_qualified` arm: verified rows plus the name-matched rows nobody refuted.

`graph` keeps every guess and `graph_verified` keeps only proven bindings; the argument for keeping
heuristic rows is an argument about rows nobody CHECKED, and the qualifier scan is the first thing
that checks some of them. This arm prices the middle of that trade, so what it keeps and what it
drops is the whole of its meaning — and it is read off the envelope's `rows[]` the way `graph_verified`
is, never off a badge in the prose.

The engine is stubbed at `_run_codeintel`, the one seam the scorer shells out through, so these test
the arm's arithmetic and not any engine.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

BENCH = pathlib.Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(BENCH))

import score

FILE = "src/exec.ts"


def _row(name: str, *, evidence: str, seen: bool | None, file: str = FILE, verified: bool = False,
         edge: str = "CALLS", module_scope: bool = False) -> dict:
    return {"relation": "caller", "name": name, "qualified_name": f"src.exec.{name}", "file": file,
            "module_scope": module_scope, "edge": edge, "verified": verified,
            "qualifier": "Chain" if seen is not None else None, "qualifier_seen": seen,
            "evidence": evidence, "strategy": "unique_name", "confidence": 0.75, "why": "x"}


def _body(rows: list[dict]) -> str:
    out = ["## Callers of Chain.resolve"]
    for r in rows:
        badge = "" if r["verified"] else " [?0.75]"
        out.append(f"- {r['qualified_name']} [{r['edge']}]{badge} ({r['file']})")
    return "\n".join(out)


def _arms(monkeypatch, rows: list[dict], **env_extra):
    env = {"result": _body(rows), "rows": rows, **env_extra}
    monkeypatch.setattr(score, "_run_codeintel", lambda *a, **k: env)
    return score.graph_answer("/root", "Chain.resolve", "codeintel")


def _names(answer) -> set[str]:
    return {name for _file, name in answer.callers}


def test_it_keeps_verified_rows_and_every_guess_nobody_refuted_and_drops_only_the_refuted(monkeypatch):
    rows = [
        _row("bound", evidence="resolved", seen=None, verified=True),
        _row("names_it", evidence="name-matched", seen=True),
        _row("unjudged", evidence="name-matched", seen=None),
        _row("never_names_it", evidence="name-matched", seen=False),
    ]
    graph, verified, qualified = _arms(monkeypatch, rows)

    assert _names(graph) == {"bound", "names_it", "unjudged", "never_names_it"}
    assert _names(verified) == {"bound"}
    assert _names(qualified) == {"bound", "names_it", "unjudged"}


def test_it_is_strictly_between_the_other_two_arms_and_so_cannot_silence_what_verified_answers(
        monkeypatch):
    """A superset of `graph_verified` and a subset of `graph`, by construction: the filter discards
    what a check refuted, not everything a binding did not confirm. That is the property that makes
    its `wrongly silent` column a price and not a coincidence."""
    rows = [_row("bound", evidence="resolved", seen=None, verified=True),
            *[_row(f"g{i}", evidence="name-matched", seen=bool(i % 2) if i < 4 else None)
              for i in range(6)]]
    graph, verified, qualified = _arms(monkeypatch, rows)

    assert verified.callers <= qualified.callers <= graph.callers
    assert qualified.callers and verified.callers


def test_a_refuted_row_that_is_the_only_row_empties_the_arm_and_the_column_would_say_so(monkeypatch):
    """The honest limit of the arm, kept as a test so nobody reads it as strictly better: when the
    scan refutes every row of an answer — including a true caller it could not have known — the arm
    claims nothing for a symbol `graph` answered. `Scores.add` is what turns that into `wrongly
    silent`, and it must see an empty, answered arm."""
    rows = [_row("only", evidence="name-matched", seen=False)]
    graph, _, qualified = _arms(monkeypatch, rows)

    assert graph.callers and not qualified.callers and not qualified.unavailable
    scores = score.Scores()
    scores.add(qualified.callers, {(FILE, "only")}, {(FILE, "only")}, qualified.unavailable)
    assert scores.said_nothing_wrongly == 1


def test_module_scope_and_reference_rows_are_keyed_as_the_other_arms_key_them(monkeypatch):
    """The engine never scans module-scope code (`_text_can_judge`), so a module-scope row is always
    `null` — the only row shape it can have. `seen=False` with `module_scope=True` cannot occur, and a
    fixture that built it would test a path no answer takes."""
    rows = [_row("ref", evidence="name-matched", seen=None, edge="CALL_REFERENCE"),
            _row("scope", evidence="name-matched", seen=None, module_scope=True, edge="USAGE")]
    rows[1]["qualified_name"] = ""
    body = ["## Callers of Chain.resolve", f"- src.exec.ref [CALL_REFERENCE] [?0.75] ({FILE})",
            f"- module scope of {FILE} [?0.75]"]
    env = {"result": "\n".join(body), "rows": rows}
    monkeypatch.setattr(score, "_run_codeintel", lambda *a, **k: env)

    graph, _, qualified = score.graph_answer("/root", "Chain.resolve", "codeintel")

    assert qualified.others == {(FILE, "ref")}, qualified
    assert qualified.callers == {(FILE, "<module>")}, "an unjudged module-scope row is kept, as `null` always is"
    assert graph.callers == {(FILE, "<module>")}, graph


def test_an_engine_that_publishes_no_rows_leaves_both_row_arms_unanswered_not_empty(monkeypatch):
    """An engine built before `rows[]` cannot be filtered, and an empty arm would read as one that
    found nothing. The reason is the one `graph_verified` already gives, so the two cannot disagree."""
    env = {"result": _body([_row("a", evidence="name-matched", seen=None)])}
    monkeypatch.setattr(score, "_run_codeintel", lambda *a, **k: env)

    _, verified, qualified = score.graph_answer("/root", "Chain.resolve", "codeintel")

    assert verified.unavailable and qualified.unavailable
    assert qualified.reason == verified.reason and "rows" in qualified.reason


def test_rows_that_disagree_with_the_printed_rows_leave_the_arm_unanswered(monkeypatch):
    rows = [_row("a", evidence="name-matched", seen=None)]
    env = {"result": _body([*rows, _row("b", evidence="name-matched", seen=None)]), "rows": rows}
    monkeypatch.setattr(score, "_run_codeintel", lambda *a, **k: env)

    _, verified, qualified = score.graph_answer("/root", "Chain.resolve", "codeintel")

    assert verified.unavailable and qualified.unavailable
    assert "do not agree" in qualified.reason, qualified.reason


@pytest.mark.parametrize("env", [
    {"result": None, "reason": "engine-unavailable"},
    {"result": None, "reason": "backend-incompatible"},
    {"result": None, "reason": "harness-error: OSError: boom"},
    {"result": "x", "rows": [], "gaps": [{"kind": "ancestor-scope", "detail": "d"}]},
])
def test_every_way_the_graph_arm_is_unanswerable_is_unanswerable_for_all_three(monkeypatch, env):
    monkeypatch.setattr(score, "_run_codeintel", lambda *a, **k: env)

    graph, verified, qualified = score.graph_answer("/root", "Chain.resolve", "codeintel")

    assert graph.unavailable and verified.unavailable and qualified.unavailable


def test_the_gate_still_reads_graph_verified_and_nothing_else():
    """The precision floor is held against the proven rows. The new arm is a measurement beside the
    gate, not a way to meet it."""
    import inspect

    assert 'direct["graph_verified"]' in inspect.getsource(score.run)
    assert "graph_qualified" not in inspect.getsource(score._gate)
