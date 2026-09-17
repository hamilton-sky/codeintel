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

from codeintel.graph_confidence import _EDGE_CONFIDENCE_FLOOR, _EDGE_CONFIDENCE_WEAK
from codeintel.providers.graph import GraphProvider

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


def test_one_glyph_carries_the_verdict_and_the_number_is_detail(monkeypatch):
    """The glyph used to split on the float — `!` at or below 0.55, `?` above — which put
    `unique_name` at 0.75 and `unique_name` at 0.38 into two visual classes despite being the same
    strategy and the same kind of evidence. One verdict now, with the score behind it. Where the
    backend reports no strategy (this fixture, and every 0.9.x edge) the note still separates the
    tiers, because there the number is the only signal there is."""
    env = _callers(monkeypatch, _rows("0.75", "0.38"))
    body = env["result"]
    assert "[?0.75]" in body and "[?0.38]" in body, body
    assert "[!" not in body, body
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
    # Both sub-floor tiers now carry the same glyph — the number is what separates them.
    assert "[?0.55]" in _callers(monkeypatch, _rows(str(_EDGE_CONFIDENCE_WEAK)))["result"]


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
    assert "[?0.38]" in env["result"], (op, env["result"])
    assert env["confidence"] == "partial"


# ---------------------------------------------------------------------------
# The heading, which is the line a reader actually acts on
# ---------------------------------------------------------------------------
#
# Every assertion above is about the badge on a row or the note beneath the rows. Both are correct
# and both are BELOW the count. Asking a 1,483-file monorepo for `callers` of `StrategyChain.resolve`
# answers `(48 direct, 2 other reference(s))` where five files in the whole repository mention
# `StrategyChain` and the truth is two; the note saying 43 of 50 rows were name-matched sits under
# fifty rows. `_render_edge_answer` already refuses one number when the rows are different KINDS of
# fact — "48 direct, 2 other reference(s)" is that refusal — and this is the same rule on the axis
# that actually misleads.


def test_the_heading_breaks_its_count_down_by_how_rows_were_resolved(monkeypatch):
    env = _callers(monkeypatch, _rows("0.95", "0.90", "0.75", "0.38"))
    head = env["result"].splitlines()[1]
    assert "2 resolved" in head, head
    assert "2 name-matched" in head, head
    # It has to deny the reading that makes the count dangerous, not merely list the tiers.
    assert "counts rows, not confirmed callers" in head


def test_the_breakdown_appears_above_the_rows(monkeypatch):
    """Placement is the entire fix. The same facts already existed underneath a fifty-row list."""
    lines = _callers(monkeypatch, _rows("0.95", "0.38"))["result"].splitlines()
    first_row = next(i for i, line in enumerate(lines) if line.startswith("- "))
    breakdown = next(i for i, line in enumerate(lines) if "name-matched" in line)
    assert breakdown < first_row, lines[:4]


def test_a_single_bucket_keeps_the_plain_heading(monkeypatch):
    """The counterweight, and the reason this is keyed on buckets rather than on badges: when every
    row was resolved the same way, one number IS honest, and a breakdown reading `3 resolved` is
    noise. A line that fires on good answers is how a real one stops being read."""
    body = _callers(monkeypatch, _rows("0.95", "0.90", "0.95"))["result"]
    assert "resolved ·" not in body and "name-matched" not in body, body
    body = _callers(monkeypatch, _rows("0.38", "0.38"))["result"]
    assert "resolved ·" not in body, body


def test_a_backend_that_never_scores_gets_no_breakdown(monkeypatch):
    """A generation that does not return the confidence column stamps every row the same way, which
    is the single-bucket case above. Reporting `3 unstated` on every answer a 0.9.x backend produces
    would be a fact about the backend restated once per query."""
    body = _callers(monkeypatch, _rows(None, None, None))["result"]
    assert "unstated" not in body, body


def test_the_breakdown_counts_every_row_it_was_given(monkeypatch):
    """Arithmetic, because a breakdown that does not sum to the answer is worse than none."""
    env = _callers(monkeypatch, _rows("0.95", "0.90", "0.75", "0.38", "0.30"))
    head = env["result"].splitlines()[1]
    counted = sum(int(part.strip().split()[0])
                  for part in head.split("**")[1].rstrip(".").split("·"))
    assert counted == 5, head


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Discharging the doubt, not only disclosing it
#
# Everything above makes an unverified answer SAY it is unverified. That leaves the reader to
# design their own check — and an agent reading this has `rg`, so the check is cheap, but only if
# it knows which string to grep. That string is derivable at the point of rendering and nowhere
# else, because only there are the target's qualifier, the defining file and the rows in doubt all
# in hand at once.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _guessed(*, n: int, resolved: int = 0, qualified: str = "chain.StrategyChain.resolve",
             leaf: str = "resolve") -> tuple:
    """(provider, groups, target) for `n` name-matched rows and `resolved` resolved ones."""
    from codeintel.graph_backend import BackendClient
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget

    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)          # type: ignore[attr-defined]
    gp._pending_gaps = []                                       # type: ignore[attr-defined]
    gp._answered_root = "/repo"                                 # type: ignore[attr-defined]

    def rows(count, bucket, start):
        return [{"a.name": f"c{i}", "a.qualified_name": f"pkg.agents.c{i}",
                 "a.file_path": f"src/agents/f{i}.ts", "type(c)": "CALLS", "_bucket": bucket}
                for i in range(start, start + count)]

    groups = [_EdgeGroup(leaf, qualified, "src/chain.ts",
                         rows(resolved, "resolved", 0) + rows(n, "name-matched", resolved))]
    return gp, groups, _SymbolTarget(leaf, qualified=qualified)


def _render(gp, groups, wanted, leaf="resolve"):
    return gp._render_edge_answer(
        "callers", "caller", leaf, wanted, groups,
        ("a.name", "a.qualified_name", "a.file_path"), False)


def test_a_dominated_answer_prints_the_command_that_would_settle_it():
    """The `StrategyChain.resolve` shape: 48 rows, 43 name-matched, five files in the whole
    repository mentioning `StrategyChain`. The command printed here is the one a person ran by
    hand to establish that finding."""
    body = _render(*_guessed(n=43, resolved=5))

    assert "rg -n --fixed-strings 'StrategyChain' /repo" in body, body
    assert "43 name-matched caller" in body, body


def test_the_command_never_greps_the_name_that_was_matched():
    """Grepping the LEAF reproduces exactly the population in doubt — it would return the same 48
    files and settle nothing. The token has to be the part of the target the match did not use."""
    body = _render(*_guessed(n=43, resolved=5))

    command = next(ln for ln in body.splitlines() if "rg -n" in ln)
    assert "'resolve'" not in command, command
    assert "'StrategyChain'" in command, command


def test_a_bare_target_falls_back_to_the_module_it_is_defined_in():
    """No qualifier to lean on, so the discriminator is the file the symbol is DEFINED in: any
    caller has to name it in an import to reach it. This is the `describe` shape — 32 rows, none
    resolved."""
    gp, groups, wanted = _guessed(
        n=32, resolved=0, qualified="pkg.domain.budget.describe", leaf="describe")
    body = _render(gp, groups, wanted, leaf="describe")

    assert "rg -n --fixed-strings 'budget' /repo" in body, body


def test_the_command_appears_where_the_breakdown_cannot():
    """When EVERY row is name-matched the evidence headline is deliberately silent — one bucket is
    the single-bucket case it refuses to restate. That is also the answer in most doubt, so the
    command has to fire there or it is missing from the case that needs it most."""
    body = _render(*_guessed(n=12, resolved=0))

    assert "name-matched." not in body, "the headline should stay silent on a single bucket"
    assert "Settle it" in body, body


def test_no_command_when_the_resolved_rows_carry_the_answer():
    """A command on a mostly-evidence answer is noise, and noise is how a disclosure stops being
    read — the same argument the evidence headline makes for its own silence."""
    assert "Settle it" not in _render(*_guessed(n=2, resolved=9))


def test_no_command_when_there_is_nothing_sharper_to_grep():
    """A target with no qualifier and no defining file offers nothing the leaf name does not. An
    unhelpful command would read as diligence while settling nothing, which is worse than silence."""
    from codeintel.graph_edges import _EdgeGroup

    gp, _groups, _wanted = _guessed(n=8)
    from codeintel.graph_targets import _SymbolTarget
    bare = [_EdgeGroup("run", "run", "", [
        {"a.name": f"c{i}", "a.qualified_name": f"pkg.c{i}", "a.file_path": f"src/f{i}.py",
         "type(c)": "CALLS", "_bucket": "name-matched"} for i in range(8)])]

    assert "Settle it" not in _render(gp, bare, _SymbolTarget("run"), leaf="run")


def test_the_command_does_not_claim_more_than_a_grep_proves():
    """It narrows; it does not decide. Overstating what a text search establishes would be this
    repository's own recurring defect, committed by the fix for it."""
    body = _render(*_guessed(n=43, resolved=5))

    assert "not proof of a call" in body, body
    assert "narrows the list" in body, body


def test_the_settle_lines_can_never_be_read_as_result_rows():
    """A cross-component contract worth pinning: `bench/score.py::graph_answer` parses this body
    back into caller keys and takes every line starting with `- ` as a row. Prose that began that
    way would be scored as a fabricated caller — the benchmark measuring the disclosure instead of
    the engine, in the direction that makes the tool look worse."""
    body = _render(*_guessed(n=43, resolved=5))

    added = [ln for ln in body.splitlines() if "Settle it" in ln or "file(s) to check" in ln]
    assert added, body
    for line in added:
        assert not line.startswith("- "), line


def test_the_settle_note_counts_the_rows_it_sends_you_to_check():
    """Both numbers in the note are claims about the answer beneath it: how many rows are in doubt,
    and how many distinct files they sit in. The second is not the first — several rows routinely
    share a file — and stating one while counting the other would send a reader looking for files
    that are not there."""
    gp, groups, wanted = _guessed(n=7, resolved=2)
    # Two of the seven name-matched rows share a file, so the two counts must differ.
    groups[0].rows[-1]["a.file_path"] = groups[0].rows[-2]["a.file_path"]
    body = _render(gp, groups, wanted)

    matched = [r for r in groups[0].rows if r["_bucket"] == "name-matched"]
    files = {r["a.file_path"] for r in matched}
    assert len(files) < len(matched), "the fixture must exercise the difference"

    assert f"{len(matched)} name-matched caller" in body, body
    assert f"The {len(files)} file(s) to check against" in body, body
