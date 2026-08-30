"""The backend scores every call edge; these pin that codeintel stops throwing that score away.

The defect this file exists against: `callers describe` on a real TypeScript repo returned 32 rows
— every one a call to vitest's global `describe()`, imported from "vitest" in the file it appears
in — all bound to the project's own `domain.budget.describe` because that was the only indexed
symbol with the name. The backend had stamped 31 of them 0.75 and one 0.38. codeintel selected
those rows, dropped the confidence column, rendered all 32 as plain callers, and stamped the
envelope `confidence: "complete"`. The one real caller, reached through an aliased import, was
absent. Every assertion below is one half of "that answer can no longer be produced".
"""
from __future__ import annotations

import pytest

from codeintel.providers.graph import (
    _EDGE_CONFIDENCE_FLOOR,
    _EDGE_CONFIDENCE_WEAK,
    GraphProvider,
)

ROOT = "/Users/x/Documents/project/codeintel"
LIST_PROJECTS = {"projects": [{"name": "codeintel", "root_path": ROOT}]}


def _rows(*confidences: str | None, name: str = "target") -> list[dict]:
    """`callers` rows for one called symbol, one row per confidence.

    `None` means the column is ABSENT from the row, which is what a backend that does not report
    confidence at all produces — a different fact from an empty string, and the two must not be
    collapsed (see `test_a_backend_that_never_scores_is_not_a_partial_answer`)."""
    out = []
    for i, c in enumerate(confidences):
        row = {
            "a.name": f"caller{i}", "a.qualified_name": f"pkg.caller{i}",
            "a.file_path": f"src/c{i}.py", "labels(a)": "Function", "type(c)": "CALLS",
            "b.name": name, "b.qualified_name": f"pkg.{name}", "b.file_path": "src/t.py",
        }
        if c is not None:
            row["c.confidence"] = c
        out.append(row)
    return out


def _provider(monkeypatch, rows: list[dict]) -> GraphProvider:
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: list(rows))
    return p


def _callers(monkeypatch, rows: list[dict], target: str = "target") -> dict:
    return _provider(monkeypatch, rows).build_result("callers", target, [], 30000, ROOT)


def test_the_query_actually_asks_for_the_confidence_column(monkeypatch):
    """Nothing downstream can work if the SELECT never asked. This is the whole root cause: the
    column existed in the backend the entire time and the Cypher did not name it."""
    seen: list[str] = []
    p = _provider(monkeypatch, _rows("0.95"))
    monkeypatch.setattr(p, "_query_rows",
                        lambda cypher, project, timeout_ms: (seen.append(cypher), _rows("0.95"))[1])
    p.build_result("callers", "target", [], 30000, ROOT)
    assert seen and "c.confidence" in seen[0], seen


def test_a_name_resolved_row_is_badged_counted_and_makes_the_envelope_partial(monkeypatch):
    env = _callers(monkeypatch, _rows("0.75"))
    body = env["result"]
    assert "[?0.75]" in body, body
    assert "1 of 1" in body
    assert env["confidence"] == "partial"
    assert any(g["kind"] == "low-confidence-edges" for g in env["gaps"]), env["gaps"]


def test_a_fuzzy_row_is_marked_apart_from_a_unique_name_row(monkeypatch):
    """The tiers fail differently and a reader has to be able to tell them apart from the row
    alone: `unique_name` is a plausible binding, a string-similarity match usually is not."""
    env = _callers(monkeypatch, _rows("0.75", "0.38"))
    body = env["result"]
    assert "[?0.75]" in body and "[!0.38]" in body, body
    assert "LIKELY SPURIOUS" in body and "UNVERIFIED" in body


def test_a_well_resolved_answer_stays_clean(monkeypatch):
    """The counterweight. A check that fires on good answers is noise, and noise is how a real
    warning stops being read — `runAlerts -> evaluate` is a hand-verified caller, so an answer
    made only of import-resolved rows must carry no badge, no note and no gap."""
    env = _callers(monkeypatch, _rows("0.95", "0.90", str(_EDGE_CONFIDENCE_FLOOR)))
    body = env["result"]
    assert "[?" not in body and "[!" not in body, body
    assert "UNVERIFIED" not in body and "SPURIOUS" not in body
    assert env["confidence"] == "complete", env.get("gaps")


def test_the_floor_is_inclusive_at_its_own_value(monkeypatch):
    """0.85 is the lowest strategy that consults the file's imports, so it is trusted; the tier
    below it is not. Pinned because an off-by-one here silently reclassifies a whole strategy."""
    assert "[?" not in _callers(monkeypatch, _rows(str(_EDGE_CONFIDENCE_FLOOR)))["result"]
    just_under = f"{_EDGE_CONFIDENCE_FLOOR - 0.01:.2f}"
    assert "[?" in _callers(monkeypatch, _rows(just_under))["result"]
    assert "[!" in _callers(monkeypatch, _rows(str(_EDGE_CONFIDENCE_WEAK)))["result"]


def test_an_answer_made_entirely_of_guesses_raises_the_collision_signature(monkeypatch):
    """The `describe` shape itself. A project symbol picks up the odd unverified caller; a name the
    index does not own collects every call site in the repository, and that pattern is the one
    thing distinguishing the two cheaply."""
    env = _callers(monkeypatch, _rows(*["0.75"] * 6))
    assert any(g["kind"] == "all-rows-name-resolved" for g in env["gaps"]), env["gaps"]
    assert "Not one row here was resolved through an import" in env["result"]


def test_one_guess_among_real_rows_is_not_the_collision_signature(monkeypatch):
    """The signature must stay specific enough that the gateway can escalate on it without
    escalating on every answer that contains a single soft row."""
    env = _callers(monkeypatch, _rows("0.95", "0.95", "0.95", "0.95", "0.75"))
    assert not any(g["kind"] == "all-rows-name-resolved" for g in env["gaps"]), env["gaps"]


def test_a_backend_that_never_scores_is_not_a_partial_answer(monkeypatch):
    """A generation that does not return the column at all says nothing about THIS answer. Marking
    every such answer partial would repeat, one level up, the defect `attach_confidence` was written
    to fix: a field that fires everywhere carries no information."""
    env = _callers(monkeypatch, _rows(None, None))
    assert env["confidence"] == "complete", env.get("gaps")
    assert "[?" not in env["result"] and "unknown" not in env["result"]


def test_an_edge_the_backend_declined_to_score_is_still_disclosed(monkeypatch):
    """The other half of the same distinction: the column came back, empty, for this edge. That is
    a fact about the edge, and it stays a gap even when every row in the answer shares it."""
    env = _callers(monkeypatch, _rows("", ""))
    assert env["confidence"] == "partial"
    assert "no confidence from the backend" in env["result"], env["result"]


@pytest.mark.parametrize("op,rows_key", [("callers", "a"), ("callees", "b")])
def test_both_edge_ops_disclose_identically(monkeypatch, op, rows_key):
    """The two ops have drifted apart before — one disclosing while the other stayed silent. Both
    read the same column and must reach the same conclusion from it."""
    env = _provider(monkeypatch, _rows("0.38")).build_result(op, "target", [], 30000, ROOT)
    assert "[!0.38]" in env["result"], (op, env["result"])
    assert env["confidence"] == "partial"
