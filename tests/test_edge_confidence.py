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

import json

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


def _breakdown(body: str) -> str:
    """The evidence headline, found by what it says rather than by where it sits.

    It used to be line 1 of every answer. The first screen now sits above it, and a test that
    encodes the offset would fail on placement rather than on the breakdown — which is the thing
    these tests are about. `test_the_breakdown_appears_above_the_rows` is where position is pinned.
    """
    return next(ln for ln in body.splitlines() if "resolved ·" in ln)


def test_the_heading_breaks_its_count_down_by_how_rows_were_resolved(monkeypatch):
    env = _callers(monkeypatch, _rows("0.95", "0.90", "0.75", "0.38"))
    head = _breakdown(env["result"])
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
    head = _breakdown(env["result"])
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

    assert "rg -n --fixed-strings --hidden --no-ignore 'StrategyChain' /repo" in body, body
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

    assert "rg -n --fixed-strings --hidden --no-ignore 'budget' /repo" in body, body


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


def test_the_candidate_listing_can_never_be_read_as_result_rows():
    """The THIRD site of the same contract, and the one that was still open.

    When a qualified target matches nothing, the answer lists the symbols that DO carry the bare
    name — the opposite of a caller of the target. Those bullets were rendered flat, so
    `bench/score.py` read them as caller rows. Two consequences, both measured on
    `bench/fixtures/corpus_ts` before this was fixed:

    * the `graph` arm scored the listing as a fabricated caller whenever the named file happened to
      hold a decidable site — it did not here, which made a real defect look like a clean run;
    * `graph_verified` refused the target outright. That arm rejects any answer whose body shows
      rows the envelope does not publish, and says so as "this build publishes no structured
      `rows`" — pointing a reader at their `codeintel` install for a defect in the renderer.

    The envelope half was already handled: `_pending_nonrow_lines` withholds the row summary. This
    is the body half.
    """
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget

    gp, _, _ = _guessed(n=1)
    others = [_EdgeGroup("resolve", f"pkg.b{i}.Other.resolve", f"src/b{i}.ts", []) for i in range(3)]
    note = gp._no_symbol_matched_the_hint(
        "callers", "StrategyChain.resolve",
        _SymbolTarget("resolve", qualified="chain.StrategyChain.resolve"), others)

    for line in note.splitlines():
        assert not line.startswith("- "), f"a candidate bullet in result-row shape: {line!r}"
    # Still a list to a person — the fix is the prefix, not the removal.
    assert sum(1 for ln in note.splitlines() if ln.startswith("> - ")) == 3, note


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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The same answer as FIELDS — `rows` and `evidence` on the envelope
#
# Everything above makes the prose honest. An agent that has to parse that prose to act on it is
# reading markdown this project reserves the right to reword, so the same facts ride the envelope
# in a shape it can branch on. The tests here are all one question: does the structured form agree
# with the body it came from? A structured summary that disagrees with its own rows would be this
# repository's recurring defect arriving through the field added to prevent it.
# ══════════════════════════════════════════════════════════════════════════════════════════════

_LIMIT = 50           # _EDGE_ROW_LIMIT: at or above it, the backend's list is capped
_CANDIDATES = 12      # _CANDIDATE_CAP: same-named symbols rendered before the rest are withheld


def _edge(i: int, *, strategy: str, confidence: str, qn: str = "pkg.target",
          tfile: str = "src/t.py") -> dict:
    """One caller row, with the provenance the bucketing actually reads."""
    return {"a.name": f"c{i}", "a.qualified_name": f"pkg.c{i}", "a.file_path": f"src/c{i}.py",
            "labels(a)": "Function", "type(c)": "CALLS", "c.confidence": confidence,
            "strategy": strategy,
            "b.name": "target", "b.qualified_name": qn, "b.file_path": tfile}


def _verified(n: int, start: int = 0, **kw) -> list[dict]:
    return [_edge(i, strategy="import_map", confidence="0.95", **kw) for i in range(start, start + n)]


def _possible(n: int, start: int = 0, **kw) -> list[dict]:
    return [_edge(i, strategy="suffix_match", confidence="0.55", **kw) for i in range(start, start + n)]


def _sided(monkeypatch, callers: list[dict], callees: list[dict]) -> GraphProvider:
    """A provider whose two edge queries answer differently, so `impact` has two real halves.

    `_provider` returns one row set to every query, which makes an impact answer whose callees are
    its callers. That is fine for the single-op tests above and useless for the one thing impact is
    tested for here — that the envelope summarises BOTH halves of a body containing both."""
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: (
        list(callers) if 'WHERE b.name=' in cypher else list(callees)))
    return p


def _row_lines(body: str) -> list[str]:
    """Every line the body offers as a result row — and exactly what `bench/score.py` reads."""
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


def _shape(monkeypatch, shape: str) -> tuple[dict, bool]:
    """(envelope, the backend's own row cap was hit) for one answer shape."""
    if shape == "clean":
        return _callers(monkeypatch, _verified(4)), False
    if shape == "name-matched":
        return _callers(monkeypatch, _possible(6)), False
    if shape == "mixed":
        return _callers(monkeypatch, _verified(3) + _possible(5, start=3)), False
    if shape == "row-cap":
        return _callers(monkeypatch, _verified(_LIMIT)), True
    if shape == "candidate-cap":
        # One row under each of thirteen distinct symbols sharing the name: twelve are rendered,
        # the thirteenth is withheld and counted.
        rows = [_verified(1, start=i, qn=f"pkg.m{i}.target", tfile=f"src/t{i}.py")[0]
                for i in range(_CANDIDATES + 1)]
        return _callers(monkeypatch, rows), False
    if shape == "impact":
        p = _sided(monkeypatch, _verified(3), _possible(4, start=3))
        return p.build_result("impact", "target", [], 30000, ROOT), False
    raise AssertionError(shape)


@pytest.mark.parametrize(
    "shape", ["clean", "name-matched", "mixed", "row-cap", "candidate-cap", "impact"])
def test_the_evidence_summary_agrees_with_the_rows_it_summarises(monkeypatch, shape):
    """THE verifier for `evidence`, which is a summary and therefore owed one.

    Its referent is not a number the renderer happens to have: it is the rows the body PRINTED.
    Four things have to hold at once, and each one is a way this summary has been wrong in some
    other shape already in this repository.

    * the three buckets partition `returned` — a breakdown that does not add up to its own list;
    * `returned` is the rows the body actually printed — a count true of what was retrieved and
      false of what was shown (`impact` recorded one of its two halves before this was fixed);
    * `total` is `None` EXACTLY when the backend's cap was hit — unknown stated as known is how a
      capped list comes to read as a complete one;
    * `truncated` is set whenever the printed list is not all of it, from either cap.
    """
    env, backend_capped = _shape(monkeypatch, shape)
    ev, rows, body = env["evidence"], env["rows"], env["result"]

    assert ev["verified"] + ev["possible"] + ev["unstated"] == ev["returned"], (
        f"the breakdown does not partition its own list: {ev}")
    assert len(rows) == ev["returned"], f"{len(rows)} structured rows summarised as {ev}"
    assert len(_row_lines(body)) == ev["returned"], (
        f"the body printed {len(_row_lines(body))} rows, the summary claims {ev['returned']}")
    for bucket, count in (("resolved", ev["verified"]), ("name-matched", ev["possible"]),
                          ("unstated", ev["unstated"])):
        assert sum(1 for r in rows if r["evidence"] == bucket) == count, (bucket, ev)

    assert (ev["total"] is None) is backend_capped, (
        f"`total` is the one field that says the size is UNKNOWN; {shape} reports {ev['total']}")
    if ev["total"] is not None:
        assert ev["total"] >= ev["returned"], ev
    assert ev["truncated"] == bool(backend_capped or ev["total"] != ev["returned"]), ev


def test_a_capped_answer_counts_the_rows_it_withheld_rather_than_forgetting_them(monkeypatch):
    """The two caps are not the same fact and must not be reported as one.

    The backend's cap leaves the total unknown. Ours leaves it known — those rows were retrieved,
    we simply did not print them — and reporting `None` there would understate what is in hand as
    firmly as reporting `returned` would overstate it."""
    env, _ = _shape(monkeypatch, "candidate-cap")
    ev = env["evidence"]

    assert ev["returned"] == _CANDIDATES, ev
    assert ev["total"] == _CANDIDATES + 1, ev
    assert ev["truncated"] is True, ev
    assert "+1 more symbol(s) with this name, not shown" in env["result"]


def test_a_structured_row_never_disagrees_with_the_line_it_was_rendered_from(monkeypatch):
    """`verified` and `confidence` are per-row claims, and the row is printed inches away.

    The badge is the reader's channel and the field is the agent's. They are derived from the same
    `_bucket` on the same dict, so the only way they diverge is a change to one of them — which is
    exactly the commit this should fail on."""
    env = _callers(monkeypatch, _verified(3) + _possible(4, start=3))
    rows, lines = env["rows"], _row_lines(env["result"])
    assert len(rows) == len(lines)

    for row, line in zip(rows, lines, strict=True):
        badged = "[?" in line
        assert row["verified"] is not badged, (
            f"{row['name']}: verified={row['verified']} but the printed line says {line!r}")
        assert row["verified"] == (row["evidence"] == "resolved"), row
        if row["confidence"] is not None and badged:
            assert f"{row['confidence']:.2f}" in line, (row, line)
    # And the row's own name is the one on the line, so a filter never returns a row a reader
    # cannot find in the answer.
    for row, line in zip(rows, lines, strict=True):
        assert row["qualified_name"] in line or row["name"] in line, (row, line)


def test_an_agent_can_filter_on_structured_fields_alone(monkeypatch):
    """Phase 3's first acceptance criterion, stated as the thing an agent would actually do.

    Filtering `rows` on `verified` has to produce the same set as reading the badges out of the
    prose, or the structured form is a second opinion rather than the same answer."""
    env = _callers(monkeypatch, _verified(3) + _possible(5, start=3))
    rows, ev = env["rows"], env["evidence"]

    kept = [r for r in rows if r["verified"]]
    assert len(kept) == ev["verified"] == 3, ev
    assert all(r["evidence"] == "resolved" and r["strategy"] == "import_map" for r in kept)
    # Every field the readiness doc asks for, present on every row, with no `None` standing in for
    # a fact the row does have.
    for r in rows:
        assert set(r) >= {"relation", "name", "qualified_name", "file", "edge", "verified",
                          "evidence", "strategy", "confidence", "why"}, r
        assert r["relation"] == "caller", r
        assert r["edge"] == "CALLS", r
        assert r["why"], r
    # The dropped rows are the ones the prose warns about, not a different population.
    assert len([r for r in rows if not r["verified"]]) == ev["possible"] == 5


def test_qualified_caller_queries_show_verified_results_before_possible_ones(monkeypatch):
    """Phase 3's second acceptance criterion. A reader who stops after the first few rows should
    be stopping on the evidence, not on whichever guess the backend returned first."""
    # Interleaved on the way in, so passing this cannot be an accident of input order.
    incoming = []
    for i in range(0, 8, 2):
        incoming += [_possible(1, start=i)[0], _verified(1, start=i + 1)[0]]
    env = _callers(monkeypatch, incoming)

    verdicts = [r["verified"] for r in env["rows"]]
    assert verdicts == sorted(verdicts, reverse=True), verdicts
    assert [("[?" in ln) for ln in _row_lines(env["result"])] == [not v for v in verdicts], (
        "the printed order has to be the structured order, or they are two different answers")


def test_truncation_cannot_be_mistaken_for_completeness(monkeypatch):
    """Phase 3's third acceptance criterion, checked in all four channels that could claim it."""
    env, _ = _shape(monkeypatch, "row-cap")
    ev = env["evidence"]

    assert ev["truncated"] is True and ev["total"] is None, ev
    assert ev["safe_for_destructive"] is False, ev
    assert env["confidence"] == "partial", env
    assert any(g["kind"] == "row-cap-reached" for g in env["gaps"]), env["gaps"]
    assert "> Truncated: yes — 50 shown, total unknown" in env["result"], env["result"][:400]


def test_the_envelope_never_calls_an_answer_safe_while_calling_it_partial(monkeypatch):
    """The cross-check between the two summaries, and the reason `safe_for_destructive` is settled
    after the body rather than inside it.

    `target-ambiguous` and `non-call-relationships` are recorded by the renderer AFTER its rows are
    printed, and `ancestor-scope` after the renderer has returned. A verdict computed at render
    time reads a gap list that is not yet the answer's — measured, before this was split apart: an
    answer over two same-named symbols came back `partial`, `gaps: [target-ambiguous]`, and
    `safe_for_destructive: true`, which is the envelope contradicting itself in the one direction
    that ends in a deletion."""
    ambiguous = (_verified(2, qn="pkg.a.target", tfile="src/a.py")
                 + _verified(2, start=2, qn="pkg.b.target", tfile="src/b.py"))
    env = _callers(monkeypatch, ambiguous)

    assert env["confidence"] == "partial"
    assert [g["kind"] for g in env["gaps"]] == ["target-ambiguous"]
    assert env["evidence"]["safe_for_destructive"] is False, env["evidence"]

    for shape in ("clean", "name-matched", "mixed", "row-cap", "candidate-cap", "impact"):
        each, _ = _shape(monkeypatch, shape)
        if each["evidence"]["safe_for_destructive"]:
            assert each["confidence"] == "complete", (shape, each["evidence"], each.get("gaps"))
            assert not each.get("gaps"), (shape, each["gaps"])


def test_an_impact_answer_summarises_both_of_its_halves(monkeypatch):
    """`impact` renders callers and callees into one body, so one of them being summarised is the
    aggregate defect with the rows still in hand. `relation` is what keeps the merged list usable:
    without it "what calls this" and "what this calls" are one undifferentiated array."""
    env, _ = _shape(monkeypatch, "impact")
    rows, ev = env["rows"], env["evidence"]

    assert ev["returned"] == 7, ev
    assert len(_row_lines(env["result"])) == 7
    assert [r["relation"] for r in rows].count("caller") == 3
    assert [r["relation"] for r in rows].count("callee") == 4
    # One banner over the whole answer, not one per half.
    assert env["result"].count("> Safe for destructive decisions:") == 1, env["result"][:600]


def test_a_clean_answer_gets_no_first_screen(monkeypatch):
    """Silence is the point. A banner printed over every result is furniture, and furniture is not
    read — the same argument the evidence headline makes for its own silence."""
    env, _ = _shape(monkeypatch, "clean")

    assert env["evidence"]["safe_for_destructive"] is True, env["evidence"]
    assert env["confidence"] == "complete"
    assert not [ln for ln in env["result"].splitlines() if ln.startswith("> ")], env["result"]
    assert env["result"].startswith("## Callers of target"), env["result"][:200]


def test_the_first_screen_says_the_same_thing_the_envelope_does(monkeypatch):
    """Two renderings of one verdict. The banner is what a model reads; `evidence` is what an
    integration branches on; a reader given different answers by the two has no way to tell which
    one the tool meant."""
    env, _ = _shape(monkeypatch, "mixed")
    banner = [ln for ln in env["result"].splitlines() if ln.startswith("> ")]
    ev = env["evidence"]

    assert banner[0] == f"> **Confidence: {env['confidence']}**", banner
    assert (f"> Verified callers: {ev['verified']} · possible: {ev['possible']} · "
            f"unstated: {ev['unstated']}") in banner, banner
    assert banner[-1] == "> Safe for destructive decisions: **no**", banner
    assert env["result"].startswith("> **Confidence:"), "the first screen is not first"


def test_the_first_screen_can_never_be_read_as_result_rows(monkeypatch):
    """The same cross-component contract the settle lines are pinned against: `bench/score.py::
    graph_answer` parses this body back into caller keys and takes every line starting with `- ` as
    a row. A banner line in that shape would be scored as a fabricated caller — the benchmark
    measuring the disclosure instead of the engine."""
    for shape in ("name-matched", "mixed", "row-cap", "candidate-cap", "impact"):
        env, _ = _shape(monkeypatch, shape)
        banner = [ln for ln in env["result"].splitlines() if ln.startswith("> ")]
        assert banner, shape
        for line in banner:
            assert not line.startswith("- "), (shape, line)
        # And it adds no row lines of its own: the body's row count is still the summary's.
        assert len(_row_lines(env["result"])) == env["evidence"]["returned"], shape


def test_the_evidence_class_follows_the_rows_and_not_only_the_op(monkeypatch):
    """The end-to-end half of `evidence_class`: two `callers` answers, same op, different class.

    This is the distinction Phase 3 item 2 asks for, and the reason it could not be a constant
    printed per op. The 48-row `StrategyChain.resolve` answer and a four-row import-resolved one
    are both `callers`; only one of them settles anything, and an agent choosing whether to delete
    reads the envelope, not the op it typed."""
    clean, _ = _shape(monkeypatch, "clean")
    assert clean["evidence_class"] == "evidence", clean["evidence_class"]
    assert clean["evidence"]["safe_for_destructive"] is True

    for shape in ("name-matched", "mixed", "row-cap", "candidate-cap"):
        env, _ = _shape(monkeypatch, shape)
        assert env["evidence_class"] == "advisory", (shape, env["evidence_class"])

    # `impact` is advisory whatever its rows say — it is a blast-radius judgement assembled from
    # two traversals plus non-call reference edges. Its rows still carry `verified`, so the
    # evidence-grade subset is reachable; the WHOLE answer is not evidence.
    impact, _ = _shape(monkeypatch, "impact")
    assert impact["evidence_class"] == "advisory", impact["evidence_class"]
    assert any(r["verified"] for r in impact["rows"]), "the per-row verdict is still there"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `same_module` — a lookup by scope, described and counted as if it were an import
#
# `callers run@bench/score.py` listed `_run_codeintel` and `_provenance` as `resolved` callers of
# `bench.score.run`, with the sentence "followed an import or a language-server binding". Both
# functions call `subprocess.run`. The backend had looked `run` up among the symbols the caller's own
# file defines, found one, and stamped it 0.90; codeintel believed the stamp and printed a sentence
# that is false for `same_module` in every case. The edge records the call as it was written
# (`c.callee`), which is what separates `run()` from `subprocess.run()`.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _same_module(i: int, callee: str | None, *, caller_file: str = "src/t.py",
                 strategy: str = "same_module", conf: str = "0.90") -> dict:
    """A `callers` row for `pkg.t.target`, resolved by `same_module`, with the call text as written.

    `callee=None` leaves the key out, which is what an index built before the backend recorded call
    text — or any edge it left blank — produces."""
    row = {
        "a.name": f"caller{i}", "a.qualified_name": f"pkg.t.caller{i}", "a.file_path": caller_file,
        "labels(a)": "Function", "type(c)": "CALLS", "c.confidence": conf, "strategy": strategy,
        # A `same_module` edge never leaves its file: the callee is defined in the caller's own.
        "b.name": "target", "b.qualified_name": "pkg.t.target", "b.file_path": caller_file,
    }
    if callee is not None:
        row["callee"] = callee
    return row


def test_the_query_asks_for_the_call_text_the_backend_recorded(monkeypatch):
    """Nothing downstream can tell `run()` from `subprocess.run()` if the SELECT never asked."""
    seen: list[str] = []
    p = _provider(monkeypatch, _rows("0.95"))
    monkeypatch.setattr(p, "_query_rows",
                        lambda cypher, project, timeout_ms: (seen.append(cypher), _rows("0.95"))[1])
    p.build_result("callers", "target", [], 30000, ROOT)
    p.build_result("callees", "target", [], 30000, ROOT)
    assert len(seen) == 2 and all("c.callee AS callee" in q for q in seen), seen


def test_a_same_module_edge_called_through_a_receiver_is_unverified_and_a_bare_one_is_not(
        monkeypatch):
    """The reproduction, reduced. Both rows carry the same strategy and the same 0.90; only the call
    text differs, and it decides the bucket. The foreign row is KEPT — ranking and labelling are
    allowed here, filtering is the trade this project refuses — and says why it is doubted."""
    env = _callers(monkeypatch, [
        _same_module(0, "subprocess.target"),
        _same_module(1, "target"),
    ])
    by_name = {r["name"]: r for r in env["rows"]}

    foreign, bare = by_name["caller0"], by_name["caller1"]
    assert foreign["verified"] is False and foreign["evidence"] == "name-matched", foreign
    assert foreign["strategy"] == "same_module" and foreign["confidence"] == 0.9, foreign
    assert "subprocess.target" in foreign["why"] and "receiver" in foreign["why"], foreign["why"]
    assert bare["verified"] is True and bare["evidence"] == "resolved", bare

    lines = _row_lines(env["result"])
    assert len(lines) == len(env["rows"]) == 2, "a downgraded row must never be dropped"
    assert "[?0.90 qualified call]" in next(ln for ln in lines if "caller0" in ln), lines
    assert "[?" not in next(ln for ln in lines if "caller1" in ln), lines
    assert env["evidence"]["verified"] == 1 and env["evidence"]["possible"] == 1


def test_the_downgrade_moves_the_envelope_through_the_machinery_that_already_exists(monkeypatch):
    """`confidence` and `evidence_class` are not set by the downgrade; they are DERIVED from the
    bucket, so a second code path to them would be the thing that lets the row and the envelope
    disagree. Same answer, only the call text changed."""
    foreign = _callers(monkeypatch, [_same_module(0, "subprocess.target"),
                                     _same_module(1, "os.target")])
    bare = _callers(monkeypatch, [_same_module(0, "target"), _same_module(1, "target")])

    assert bare["confidence"] == "complete" and bare["evidence_class"] == "evidence", bare
    assert bare["evidence"]["safe_for_destructive"] is True

    assert foreign["confidence"] == "partial", foreign
    assert foreign["evidence_class"] == "advisory", foreign["evidence_class"]
    assert foreign["evidence"]["safe_for_destructive"] is False
    assert foreign["evidence"]["verified"] == 0 and foreign["evidence"]["possible"] == 2
    assert any(g["kind"] == "low-confidence-edges" for g in foreign["gaps"]), foreign["gaps"]
    # The note names the mechanism, so a reader knows what the doubt is about.
    assert "`same_module` matches whose call is written through a receiver" in foreign["result"]
    assert "(`same_module`)" in foreign["result"]


_UNCHECKED_LANGUAGES = "only defined for Python and JavaScript/TypeScript"


@pytest.mark.parametrize("callee, caller_file, why_fragment", [
    (None, "src/t.py", "recorded no call text"),            # an older index, or a blank edge
    ("", "src/t.py", "recorded no call text"),
    (None, "src/t.ts", "recorded no call text"),
    ("subprocess.target", "src/t.go", _UNCHECKED_LANGUAGES),   # a rule borrowed from Python
    ("this.target", "src/t.rs", _UNCHECKED_LANGUAGES),
    ("alias_for_something_else", "src/t.py", "is not a call of this symbol"),   # not THIS symbol
    ("alias_for_something_else", "src/t.ts", "is not a call of this symbol"),
])
def test_a_same_module_edge_the_check_cannot_judge_stays_resolved_and_says_so(
        monkeypatch, callee, caller_file, why_fragment):
    """A check that cannot run must not move a row — the rule `source-unreadable` already follows.
    Unknown is a statement, so it is made: the row stays `resolved` (so bench numbers for these
    languages do not move) but its `why` says what scope did, that scope only binds a bare call,
    and that whether this call is bare could not be checked — instead of the old sentence, which
    said no binding was followed beside `verified: true`."""
    env = _callers(monkeypatch, [_same_module(0, callee, caller_file=caller_file)])
    row = env["rows"][0]

    assert row["verified"] is True and row["evidence"] == "resolved", row
    assert "scope resolved this inside the caller's own module" in row["why"], row["why"]
    assert "only binds a call written bare" in row["why"], row["why"]
    assert "could not be checked" in row["why"] and why_fragment in row["why"], row["why"]
    assert "counted as resolved" in row["why"] and "the binding is a guess" in row["why"], row["why"]
    assert "followed an import" not in row["why"], row["why"]
    assert _CONTRADICTION not in row["why"], row["why"]
    assert "[?" not in env["result"], env["result"]


# The sentence a `verified: true` row used to carry: it said, of a row counted as resolved, that no
# binding was followed. Both claims cannot be true of one row, and an agent filtering on `verified`
# never reads the sentence.
_CONTRADICTION = "no import or language-server binding was followed"


def test_no_row_that_is_verified_says_that_no_binding_was_followed(monkeypatch):
    """The invariant, over every way a `same_module` row can reach `resolved` — and over the other
    strategies for good measure. `verified` is the field integrations branch on and `why` is the field
    people read, so they may not disagree about the one thing the row exists to say."""
    rows = [
        _same_module(0, "target"),                                        # bare, own
        _same_module(1, "self.target"),                                   # own receiver
        _same_module(2, None),                                            # no call text
        _same_module(3, ""),
        _same_module(4, "other_leaf"),                                    # not this symbol's name
        _same_module(5, "subprocess.target", caller_file="src/t.go"),     # a language it cannot read
        _same_module(6, "pkg.target", caller_file="src/t.rs"),
        _same_module(7, "this.target", caller_file="src/t.ts"),
        _same_module(8, None, caller_file="src/t.ts"),
        _same_module(9, "self.target", strategy="lsp_direct", conf="0.95"),
        _same_module(10, None, strategy="import_map", conf="0.95"),
        _same_module(11, None, strategy="", conf="0.97"),                 # a score, no strategy
    ]
    # One answer per row: rows in different languages are different symbols to `callers`, which
    # would group them apart, and this is about each row's own sentence, not about the grouping.
    answered = [_callers(monkeypatch, [row])["rows"][0] for row in rows]

    verified = [r for r in answered if r["verified"]]
    assert len(verified) == len(rows), "every row here is one the backend resolved and nothing downgrades"
    for r in verified:
        assert _CONTRADICTION not in r["why"], r


@pytest.mark.parametrize("callee, verdict", [
    ("console.target", "foreign"),          # the global `console`, bound to this module's own `log`
    ("obj.target", "foreign"),
    ("this.handlers.target", "foreign"),    # a member of `this`, not `this`
    ("target", "own"),                      # a bare call binds by scope
    ("this.target", "own"),
    ("super.target", "own"),
    ("this?.target", "own"),
    ("t.target", "own"),                    # a segment of the symbol's own qualified name: its module
])
def test_a_same_module_edge_in_typescript_is_checked_like_one_in_python(
        monkeypatch, callee, verdict):
    """`console.log(x)` in a module that defines its own `log` is a call to the global, which the
    backend bound to the local `log` because the leaf matched — the same defect as `subprocess.run`
    in Python, in a language whose member-call semantics are the same: `this`/`super` are the
    enclosing object, anything else before the dot is some other value."""
    env = _callers(monkeypatch, [_same_module(0, callee, caller_file="src/t.ts")])
    row = env["rows"][0]
    if verdict == "foreign":
        assert row["verified"] is False and row["evidence"] == "name-matched", row
        assert callee in row["why"] and "receiver" in row["why"], row["why"]
        assert row["strategy"] == "same_module" and row["confidence"] == 0.9, row
        assert "[?0.90 qualified call]" in env["result"], env["result"]
        assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False
    else:
        assert row["verified"] is True and row["evidence"] == "resolved", row
        assert "scope binds it" in row["why"], row["why"]
        assert "[?" not in env["result"], env["result"]
        assert env["confidence"] == "complete", env


def test_console_log_in_a_module_that_defines_its_own_log_is_not_a_caller_of_it(monkeypatch):
    """The brief's own example, with the names it used. `utils.ts` defines `log` and calls
    `console.log(x)`; the backend binds the call to the local `log` by scope. The row is a guess about
    what `console` is, so it is badged, counted and unverified — and `this.log()` beside it is not."""
    def edge(i: int, callee: str) -> dict:
        return {
            "a.name": f"caller{i}", "a.qualified_name": f"pkg.utils.caller{i}",
            "a.file_path": "src/utils.ts", "labels(a)": "Function", "type(c)": "CALLS",
            "c.confidence": "0.90", "strategy": "same_module", "callee": callee,
            "b.name": "log", "b.qualified_name": "pkg.utils.log", "b.file_path": "src/utils.ts",
        }

    env = _callers(monkeypatch, [edge(0, "console.log"), edge(1, "this.log"), edge(2, "log")],
                   target="log")
    by_name = {r["name"]: r for r in env["rows"]}

    assert by_name["caller0"]["verified"] is False, by_name["caller0"]
    assert by_name["caller0"]["evidence"] == "name-matched"
    assert "console.log" in by_name["caller0"]["why"], by_name["caller0"]["why"]
    assert by_name["caller1"]["verified"] is True and by_name["caller2"]["verified"] is True
    assert env["evidence"]["verified"] == 2 and env["evidence"]["possible"] == 1, env["evidence"]
    assert any(g["kind"] == "low-confidence-edges" for g in env["gaps"]), env["gaps"]


def test_a_downgraded_typescript_row_is_kept_and_a_bare_one_beside_it_is_not(monkeypatch):
    """The reproduction, in TypeScript, reduced: both rows carry the same strategy and score, only
    the call text differs, and neither is dropped."""
    env = _callers(monkeypatch, [
        _same_module(0, "console.target", caller_file="src/a.ts"),
        _same_module(1, "target", caller_file="src/b.ts"),
    ])
    by_name = {r["name"]: r for r in env["rows"]}
    assert by_name["caller0"]["verified"] is False and by_name["caller1"]["verified"] is True
    assert len(_row_lines(env["result"])) == len(env["rows"]) == 2


@pytest.mark.parametrize("callee", [
    "self.target", "cls.target", "super().target", "super(Base, self).target",
    "t.target",                 # `t` is a segment of the symbol's own qualified name: its module
    "target",
])
def test_a_receiver_that_names_the_modules_own_symbol_is_never_downgraded(monkeypatch, callee):
    """The counterweight, and the half that keeps this from over-filtering. `self.helper()` resolving
    to a method of the same class is the rule working; so is `Config.load` inside the module that
    defines `Config`. A check that fired on every attribute call would have retired a lot of
    correct callers to fix two wrong ones."""
    env = _callers(monkeypatch, [_same_module(0, callee)])
    row = env["rows"][0]

    assert row["verified"] is True, (callee, row)
    assert "scope binds it" in row["why"], (callee, row["why"])
    assert env["evidence_class"] == "evidence" and env["confidence"] == "complete", env


def test_the_same_module_call_check_states_the_rule_it_applies():
    """The check as a function, one line per case, so a change to the rule is a change to this table."""
    from codeintel.graph_confidence import (
        _SAME_MODULE_FOREIGN,
        _SAME_MODULE_OWN,
        _SAME_MODULE_UNCHECKED,
        _same_module_call,
    )

    def row(callee, *, file="src/t.py", target="target", qn="pkg.t.Owner.target"):
        return {"callee": callee, "a.file_path": file, "b.name": target, "b.qualified_name": qn}

    assert _same_module_call(row("target")) == _SAME_MODULE_OWN
    assert _same_module_call(row("self.target")) == _SAME_MODULE_OWN
    assert _same_module_call(row("Owner.target")) == _SAME_MODULE_OWN      # the class that owns it
    assert _same_module_call(row("subprocess.target")) == _SAME_MODULE_FOREIGN
    assert _same_module_call(row("self.helper.target")) == _SAME_MODULE_FOREIGN
    assert _same_module_call(row("p.target")) == _SAME_MODULE_FOREIGN
    assert _same_module_call(row("x[0].target")) == _SAME_MODULE_FOREIGN
    assert _same_module_call(row("")) == _SAME_MODULE_UNCHECKED
    assert _same_module_call(row("subprocess.target", file="src/t.go")) == _SAME_MODULE_UNCHECKED
    assert _same_module_call(row("subprocess.target", file="src/t.rs")) == _SAME_MODULE_UNCHECKED
    assert _same_module_call(row("subprocess.other")) == _SAME_MODULE_UNCHECKED  # not this symbol
    # JavaScript and TypeScript: `this`/`super` are the enclosing object where Python has `self`.
    assert _same_module_call(row("target", file="src/t.ts")) == _SAME_MODULE_OWN
    assert _same_module_call(row("this.target", file="src/t.tsx")) == _SAME_MODULE_OWN
    assert _same_module_call(row("super.target", file="src/t.js")) == _SAME_MODULE_OWN
    assert _same_module_call(row("Owner.target", file="src/t.ts")) == _SAME_MODULE_OWN
    assert _same_module_call(row("console.target", file="src/t.ts")) == _SAME_MODULE_FOREIGN
    assert _same_module_call(row("this.helper.target", file="src/t.ts")) == _SAME_MODULE_FOREIGN
    assert _same_module_call(row("self.target", file="src/t.ts")) == _SAME_MODULE_FOREIGN  # not JS
    assert _same_module_call(row("this.target")) == _SAME_MODULE_FOREIGN                   # not Python
    assert _same_module_call(row("console.other", file="src/t.ts")) == _SAME_MODULE_UNCHECKED


def test_why_says_what_each_strategy_actually_did(monkeypatch):
    """One sentence per mechanism, because the bucket the old sentence covered held three of them.

    "followed an import or a language-server binding" is true of `lsp_*` and `import_map`, and it was
    attached to `same_module` and to an unnamed 0.9x edge as well. Each claim below is exactly as
    strong as what the backend did, and the two that did not follow an import say so."""
    def edge(i, strategy, conf, callee=None):
        row = _same_module(i, callee, strategy=strategy, conf=conf)
        row["a.file_path"] = f"src/c{i}.py"
        return row

    rows = [
        edge(0, "lsp_direct", "0.95"),
        edge(1, "import_map", "0.95"),
        edge(2, "same_module", "0.90", "target"),
        edge(3, "", "0.97"),                                 # a high score, no strategy named
        edge(4, "suffix_match", "0.40"),
        edge(5, "same_module", "0.90", "subprocess.target"),
    ]
    why = {r["name"]: r["why"] for r in _callers(monkeypatch, rows)["rows"]}

    assert "language server" in why["caller0"] and "lsp_direct" in why["caller0"], why
    assert "imports this symbol" in why["caller1"], why
    assert "scope binds it" in why["caller2"] and "no import" in why["caller2"], why
    assert "no import or language-server binding was followed" not in " ".join(why.values()), why
    assert "without naming a strategy" in why["caller3"], why
    assert "matched by name (suffix_match)" in why["caller4"], why
    assert "through a receiver" in why["caller5"], why
    # No sentence outside the two that DID follow a binding may claim one was followed.
    for who in ("caller2", "caller3", "caller4", "caller5"):
        assert "followed an import or a language-server binding" not in why[who], (who, why[who])
    assert len(set(why.values())) == len(why), "two strategies share one sentence"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Advice the tool cannot honour
#
# A name-matched answer tells the reader to confirm with `--engine lsp`. The language server has no
# `callers`, so that command answers `unsupported-op` — the tool recommended a check it then
# refused to run, at the moment the reader had just been told not to trust the answer.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _lsp_ops() -> set[str]:
    """The ops the LSP engine's dispatcher actually handles, read from the dispatcher itself."""
    import inspect
    import re

    from codeintel.providers.lsp import LspProvider

    return set(re.findall(r'op == "(\w+)"', inspect.getsource(LspProvider._dispatch)))


def _advice_spans(text: str, answered_op: str) -> list[tuple[str, str]]:
    """Every `(engine, op)` a backticked command in *text* tells the reader to run.

    A command that names an engine and no op means the op the reader just asked, which is the very
    reading that made the old advice wrong."""
    import re

    out = []
    for span in re.findall(r"`([^`]*--engine\s+\w+[^`]*)`", text):
        engine = re.search(r"--engine\s+(\w+)", span).group(1)
        op = re.search(r"--op\s+(\w+)", span)
        out.append((engine, op.group(1) if op else answered_op))
    return out


def test_no_hint_recommends_an_engine_and_op_that_returns_unsupported_op(monkeypatch):
    from codeintel.providers.graph import _GRAPH_OPS

    lsp_ops = _lsp_ops()
    assert lsp_ops and "callers" not in lsp_ops, (
        "the premise of this test is that the language server answers symbol-shaped ops only; if "
        f"that changed, the advice is allowed to change with it. dispatcher ops: {lsp_ops}")
    supported = {"lsp": lsp_ops, "graph": set(_GRAPH_OPS), "semantic": {"search"}}

    # 1. the name-collision signature: five rows, none of them resolved through an import.
    collision = _callers(monkeypatch, _rows(*["0.75"] * 6))
    assert any(g["kind"] == "all-rows-name-resolved" for g in collision["gaps"]), collision["gaps"]

    # 2. the `no-edges` safe-null, which tells the reader how to confirm a symbol nobody calls.
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        LIST_PROJECTS if method == "list_projects" else None))
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: (
        [] if "-[" in cypher else [{"n.qualified_name": "pkg.target", "n.file_path": "src/t.py"}]))
    no_edges = p.build_result("callers", "target", [], 30000, ROOT)
    assert no_edges["reason"] == "no-edges", no_edges

    for label, text in (("collision note", collision["result"]), ("no-edges hint", no_edges["hint"])):
        advice = _advice_spans(text, "callers")
        assert advice, f"the {label} no longer names a command; this test has nothing to pin"
        for engine, op in advice:
            assert op in supported.get(engine, set()), (
                f"the {label} tells the reader to run `{op}` on the {engine} engine, which answers "
                f"`unsupported-op`:\n{text}")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The qualifier scan — running the check the settle note used to tell a reader to run
#
# The settle note derives the token a name match did not use and printed `rg -n --fixed-strings
# 'StrategyChain' <root>`. The token, the root and the rows in doubt are all in hand at render time, so
# the answer now RUNS that check, states the result, publishes it per row as `qualifier_seen`, counts
# it, labels the row and ranks the refuted rows last. Everything below is about what the result may
# and may not be used to claim: the scan narrows an answer, it never empties one.
#
# These go through `build_result` against a REAL tree, because the scan reads files — a stub that
# never opens one cannot show it. Each "never scanned" test carries a CONTROL: the same graph with the
# one fact changed, in which the scan does run and does refute. A negative that cannot fail before the
# change proves nothing on its own, so each is paired with an assertion that can.
# ══════════════════════════════════════════════════════════════════════════════════════════════

_TOKEN = "StrategyChain"
_CHAIN_FILE = "src/chain.ts"


def _chain_row(i: int, *, file: str | None = None, strategy: str = "unique_name",
               conf: str = "0.75", callee: str = "resolve", caller_label: str = "Function",
               target_label: str = "Method", target_qn: str = "pkg.chain.StrategyChain.resolve",
               target_file: str = _CHAIN_FILE, target_name: str = "resolve",
               name: str | None = None, kind: str = "CALLS") -> dict:
    """One `callers` row for `StrategyChain.resolve`, in the shape the real backend returns it."""
    return {
        "a.name": name or f"c{i}", "a.qualified_name": f"pkg.agents.{name or f'c{i}'}",
        "a.file_path": file or f"src/agents/f{i}.ts", "labels(a)": json.dumps([caller_label]),
        "type(c)": kind, "c.confidence": conf, "strategy": strategy, "callee": callee,
        "b.name": target_name, "b.qualified_name": target_qn, "b.file_path": target_file,
        "labels(b)": json.dumps([target_label]),
    }


def _write_tree(root, rows: list[dict], *, naming: set[int] = frozenset(),  # type: ignore[assignment]
                missing: set[int] = frozenset()) -> None:  # type: ignore[assignment]
    """Every row's file on disk. `naming` is the row indices whose file writes `StrategyChain`;
    `missing` the ones whose file is not written at all, which is how an unreadable path reaches the
    scan. The defining file always writes the class, as the file that declares it does."""
    for i, row in enumerate(rows):
        if i in missing:
            continue
        path = root / row["a.file_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "export const x = 1;\n"
        if i in naming:
            body += f"import {{ {_TOKEN} }} from './chain';\n"
        if not path.exists() or i in naming:
            path.write_text(body)
    for defining in {row["b.file_path"] for row in rows} | {_CHAIN_FILE}:
        chain = root / defining
        chain.parent.mkdir(parents=True, exist_ok=True)
        chain.write_text(f"export class {_TOKEN} {{ resolve() {{}} }}\n")


def _scanned_callers(monkeypatch, tmp_path, rows: list[dict], *, naming=frozenset(),
                     missing=frozenset(), target: str = f"{_TOKEN}.resolve",
                     op: str = "callers", owner: str | None = "defining",
                     queries: list[str] | None = None) -> dict:
    """The envelope for `target` over *rows*, answered from a tree rooted at `tmp_path`.

    *owner* is what the backend says about the class that defines the called method, because the scan
    asks (`DEFINES_METHOD`) before it reads a file: `"defining"` answers with the class the method's
    qualified name sits under, which is what a real index holds; `None` answers with no class at all,
    the shape of a method with no class node behind it; any other string is the class named. Every
    query sent to the backend is appended to *queries* when it is given."""
    _write_tree(tmp_path, rows, naming=set(naming), missing=set(missing))
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    listing = {"projects": [{"name": "codeintel", "root_path": str(tmp_path)}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))

    def answer(cypher, project, timeout_ms):
        if queries is not None:
            queries.append(cypher)
        if "[:DEFINES_METHOD]" in cypher:
            if owner is None:
                return []
            out = []
            for qn in sorted({r["b.qualified_name"] for r in rows}):
                cls = qn.rsplit(".", 1)[0] if owner == "defining" else owner
                out.append({"c.qualified_name": cls, "c.name": cls.rsplit(".", 1)[-1],
                            "c.file_path": _CHAIN_FILE, "c.base_classes": None,
                            "m.qualified_name": qn})
            return out
        return [dict(r) for r in rows]

    monkeypatch.setattr(p, "_query_rows", answer)
    return p.build_result(op, target, [], 30000, str(tmp_path))


def _seen(env: dict) -> list:
    return [r["qualifier_seen"] for r in env["rows"]]


def _checked_counts(env: dict) -> tuple[int, int]:
    """`(N, M)` from the printed `Checked: **N of M**` line AFTER re-deriving both from `rows[]` and
    asserting they agree — which is the verifier `test_summary_integrity` registers for the note.

    M is the name-matched rows of the DIRECT list as printed: `rows[]` also carries the callers
    through a base (`via`), which are rendered apart and never scanned, so they are not counted. N is
    the rows whose file never writes the qualifier."""
    import re

    stated = re.search(r"_Checked: \*\*(\d+) of (\d+)\*\*", env["result"])
    assert stated, env["result"]
    derived_m = sum(1 for r in env["rows"] if r["evidence"] == "name-matched" and not r.get("via"))
    derived_n = sum(1 for r in env["rows"] if r["qualifier_seen"] is False)
    assert (int(stated.group(1)), int(stated.group(2))) == (derived_n, derived_m), (
        "the note's `N of M` is not the rows it describes", stated.group(0), derived_n, derived_m)
    return derived_n, derived_m


def _guesses(n: int, **kw) -> list[dict]:
    return [_chain_row(i, **kw) for i in range(n)]


def test_the_note_states_what_the_scan_found_rather_than_how_to_find_it(monkeypatch, tmp_path):
    """The whole point of the change. The settle note told a reader to run `rg`; the token, the root
    and the rows in doubt are all in hand at render time, so the answer states the result.

    The command stays in the body underneath. A claim a reader cannot re-run is a claim they have to
    take on trust, which is the thing this note exists to avoid."""
    env = _scanned_callers(monkeypatch, tmp_path, _guesses(4))
    body = env["result"]

    assert ("_Checked: **4 of 4** name-matched callers shown are in files that never write "
            "`StrategyChain`") in body, body
    assert "the name it is qualified by" in body, body
    assert "rg -n --fixed-strings -- 'StrategyChain' " in body, body
    assert "Settle it" not in body, "the instruction is replaced by its result, not printed beside it"
    assert _seen(env) == [False] * 4, env["rows"]
    # The note's `N of M` is re-derived from `rows[]` and from the envelope's own counts, not read back
    # from a second derivation that could drift.
    assert _checked_counts(env) == (4, 4)
    assert (env["evidence"]["qualifier_absent"], env["evidence"]["possible"]) == (4, 4), env["evidence"]


def test_the_scan_never_removes_a_row(monkeypatch, tmp_path):
    """The decided constraint, pinned where it would be easiest to break. Dropping a disproved row
    would trade a false positive for a false negative, and `corpus-ts` prices that trade: filtering
    the three fabricated rows off `FallbackChain.resolve` also empties an answer for a symbol that
    has a caller. The scan ranks and labels; the caller decides."""
    rows = [_chain_row(0, strategy="import_map", conf="0.95"), *(_chain_row(i) for i in range(1, 5))]
    env = _scanned_callers(monkeypatch, tmp_path, rows)

    assert _seen(env).count(False) == 4, env["rows"]       # the check ran, and refuted four of them
    assert len(_row_lines(env["result"])) == 5, env["result"]
    assert env["evidence"]["returned"] == 5 and len(env["rows"]) == 5, env["evidence"]


def test_a_disproved_row_ranks_last_but_never_above_a_binding(monkeypatch, tmp_path):
    """Ordering is the lever the scan is allowed to pull, and only within a bucket. A name-matched
    row whose file writes the qualifier sorts above one nobody could judge, which sorts above one
    whose file does not — and none of them can rise above a `resolved` row, because the scan is
    weaker evidence than a followed binding and ordering it as though it were equal would be the same
    over-claim in a new place.

    The names that write the qualifier are the LATER ones in the backend's order, so an answer that
    ignored the scan would print them last."""
    rows = [_chain_row(0, strategy="import_map", conf="0.95"), *(_chain_row(i) for i in range(1, 6))]
    env = _scanned_callers(monkeypatch, tmp_path, rows, naming={4, 5}, missing={3})

    assert [r["name"] for r in env["rows"]] == ["c0", "c4", "c5", "c3", "c1", "c2"], env["rows"]
    assert _seen(env) == [None, True, True, None, False, False], env["rows"]
    assert [r["evidence"] for r in env["rows"]][:1] == ["resolved"], env["rows"]


def test_a_verdict_never_puts_a_test_ahead_of_production_within_the_same_evidence(
        monkeypatch, tmp_path):
    """The truncation note promises "Production code is listed first and test files last", and the
    scan verdict is a finer cut of evidence, not a reason to break it. The verdict is the LAST sort
    key: kind, evidence bucket, production-versus-test, and only then the verdict. A spec that
    imports the class (`true`) used to print ahead of a production caller that injects it (`false`) —
    the one place a name-matched test outranked name-matched production, and the answer was still
    telling the reader the opposite.

    Within each partition the verdict still orders: names it, unjudged, never writes it."""
    rows = [
        _chain_row(0, file="tests/a.test.ts", name="test_never"),      # refuted, a test
        _chain_row(1, file="src/agents/prod_never.ts", name="prod_never"),  # refuted, production
        _chain_row(2, file="tests/b.test.ts", name="test_names"),      # names it, a test
        _chain_row(3, file="src/agents/prod_names.ts", name="prod_names"),  # names it, production
    ]
    env = _scanned_callers(monkeypatch, tmp_path, rows, naming={2, 3})

    assert [r["name"] for r in env["rows"]] == [
        "prod_names", "prod_never", "test_names", "test_never"], env["rows"]
    assert _seen(env) == [True, False, True, False], env["rows"]


def test_a_resolved_row_is_never_marked_by_the_scan(monkeypatch, tmp_path):
    """`null`, not `true` and not `false`. A row that followed a real binding is not in the scanned
    population, so reporting either verdict for it would be a claim about a file nobody opened — and
    `false` would invite the reading that a proven caller is suspect for not writing the class's name.
    That includes the class-hierarchy resolution (`self_mro`) and a row with no provenance at all."""
    rows = [
        _chain_row(0, strategy="import_map", conf="0.95", name="by_import"),
        _chain_row(1, strategy="lsp_direct", conf="0.95", name="by_lsp"),
        _chain_row(2, strategy="self_mro", conf="0.90", name="by_hierarchy"),
        _chain_row(3, strategy="", conf="", name="no_provenance"),
        *(_chain_row(i, name=f"guess{i}") for i in range(4, 8)),
    ]
    env = _scanned_callers(monkeypatch, tmp_path, rows)
    by_name = {r["name"]: r for r in env["rows"]}

    assert all(by_name[n]["qualifier_seen"] is False for n in ("guess4", "guess5", "guess6", "guess7")), (
        "the control: the scan ran, and refuted the guesses", env["rows"])
    for n in ("by_import", "by_lsp", "by_hierarchy", "no_provenance"):
        assert by_name[n]["qualifier_seen"] is None and by_name[n]["qualifier"] is None, by_name[n]
        assert "never writes" not in by_name[n]["why"], by_name[n]
    assert by_name["by_hierarchy"]["evidence"] == "resolved", by_name["by_hierarchy"]


def test_an_unreadable_file_is_unknown_and_not_counted_as_absent(monkeypatch, tmp_path):
    """The distinction every summary in this project exists to keep. "This file does not write
    `StrategyChain`" and "we could not open this file" are different facts, and collapsing them
    would let a missing path argue that a row is spurious."""
    env = _scanned_callers(monkeypatch, tmp_path, _guesses(4), missing={2, 3})
    body = env["result"]

    seen = _seen(env)
    assert seen.count(None) == 2 and seen.count(False) == 2, seen
    assert "_Checked: **2 of 4**" in body, body
    assert "2 could not be judged" in body and "`qualifier_seen: null`" in body, body
    assert env["evidence"]["qualifier_absent"] == 2, env["evidence"]


def test_a_file_that_cannot_be_judged_at_all_leaves_the_answer_as_it_was(monkeypatch, tmp_path):
    """Nothing readable means nothing judged: the answer prints the command it always printed and no
    row is marked, rather than reporting "0 of 4" about files nobody opened."""
    env = _scanned_callers(monkeypatch, tmp_path, _guesses(4), missing={0, 1, 2, 3})
    body = env["result"]

    assert "_Settle it:" in body and "Checked:" not in body, body
    assert _seen(env) == [None] * 4, env["rows"]


def test_the_evidence_block_counts_what_the_scan_decided(monkeypatch, tmp_path):
    """The envelope's counts are the rows' own values, not a second derivation that could drift."""
    rows = [_chain_row(0, strategy="import_map", conf="0.95"), *(_chain_row(i) for i in range(1, 5))]
    env = _scanned_callers(monkeypatch, tmp_path, rows, naming={1})
    ev = env["evidence"]

    assert ev["qualifier_present"] == 1, ev
    assert ev["qualifier_absent"] == 3, ev
    assert ev["qualifier_present"] + ev["qualifier_absent"] == ev["possible"], ev


def test_the_note_says_so_when_every_row_that_could_be_judged_does_write_the_qualifier(
        monkeypatch, tmp_path):
    """A result of "0 of N" is still a result, and it must not borrow the refuted wording. When the
    check narrows nothing the note says exactly that, and claims no row is ranked last."""
    env = _scanned_callers(monkeypatch, tmp_path, _guesses(4), naming={0, 1, 2, 3})
    body = env["result"]

    assert "none of the 4** name-matched callers shown that could be judged" in body, body
    assert "narrows nothing here" in body, body
    assert "ranked last" not in body and "qualifier_seen: false" not in body, body
    assert "[never writes" not in body, "no row is refuted, so no row is labelled"
    assert env["evidence"]["qualifier_absent"] == 0 and env["evidence"]["qualifier_present"] == 4


def test_a_refuted_row_says_so_on_its_own_line_and_in_its_why(monkeypatch, tmp_path):
    """A reader scanning a list reads rows rather than notes — and `changed <ref>` has no note at all.
    The fact is on the line it is about, in the same words everywhere, and `why` says what it does
    and does not establish."""
    rows = _guesses(3)
    env = _scanned_callers(monkeypatch, tmp_path, rows, naming={0})
    lines = _row_lines(env["result"])
    by_name = {r["name"]: r for r in env["rows"]}

    assert "[never writes `StrategyChain`]" not in lines[0], lines
    assert all("[?0.75] [never writes `StrategyChain`]" in ln for ln in lines[1:]), lines
    assert by_name["c1"]["qualifier"] == "StrategyChain" and by_name["c1"]["qualifier_seen"] is False
    assert "never writes `StrategyChain`" in by_name["c1"]["why"], by_name["c1"]
    assert "not proof that the call cannot reach this symbol" in by_name["c1"]["why"], by_name["c1"]
    assert "its file writes `StrategyChain`" in by_name["c0"]["why"], by_name["c0"]
    assert "which is text and not a call" in by_name["c0"]["why"], by_name["c0"]


def test_without_a_readable_root_the_answer_falls_back_to_printing_the_command(monkeypatch, tmp_path):
    """No scan, no claim. A provider answering about a root that is not on disk must not report
    "0 of 43 name it" — it must hand the reader the check, which is the behaviour from before the
    scan, unchanged. CONTROL: the identical graph over a real tree is scanned."""
    rows = _guesses(5)
    scanned = _scanned_callers(monkeypatch, tmp_path, rows)
    assert _seen(scanned) == [False] * 5, scanned["rows"]

    env = _callers(monkeypatch, rows, target=f"{_TOKEN}.resolve")        # ROOT is not a directory
    assert "_Settle it:" in env["result"] and "Checked:" not in env["result"], env["result"]
    assert _seen(env) == [None] * 5, env["rows"]
    assert env["evidence"]["qualifier_absent"] == 0, env["evidence"]


def test_the_note_that_hands_over_the_command_no_longer_claims_a_miss_is_spurious(monkeypatch):
    """The printed-command note said "a file that never names it cannot be reaching this symbol, so
    any … below whose file is absent from that output is spurious". That is false: an instance obtained
    elsewhere, an interface-typed field, a subclass or a renaming re-export all reach a method without
    naming its class. It stays a next step; it stops being a verdict — and it no longer calls the
    absence "very unlikely" to be a caller, which is a probability nobody measured."""
    env = _callers(monkeypatch, _guesses(6), target=f"{_TOKEN}.resolve")
    note = env["result"]

    assert "_Settle it:" in note, note
    assert "cannot be reaching" not in note and "is spurious" not in note, note
    assert "very unlikely" not in note, note
    assert "an instance the file gets from elsewhere" in note, note
    assert "the ones to doubt first" in note and "not proof" in note, note


def test_a_bare_target_is_never_scanned_because_a_re_export_defeats_the_stem(monkeypatch, tmp_path):
    """The limit that a measurement imposed, not a cautious guess. For a bare target the discriminator
    falls back to the stem of the defining file, and a caller reaching the symbol through a re-export
    never writes that stem: on `corpus-ts`, `callerFacade.ts` imports `forwardReleasedItem` from
    `./facade`, contains no `proxy`, and a scan disproved a TRUE caller. A bare target also gets the
    qualifier the BACKEND recorded, so a method named bare is no different — the caller did not write
    one. CONTROL: the identical rows under the dotted target are scanned."""
    rows = _guesses(4)
    dotted = _scanned_callers(monkeypatch, tmp_path, rows)
    assert _seen(dotted) == [False] * 4, dotted["rows"]

    bare = _scanned_callers(monkeypatch, tmp_path, rows, target="resolve")
    assert _seen(bare) == [None] * 4, bare["rows"]
    assert "Checked:" not in bare["result"] and "_Settle it:" in bare["result"], bare["result"]


def test_a_dotted_target_that_names_a_module_and_not_a_class_is_never_scanned(monkeypatch, tmp_path):
    """The weak case that survives a gate on "the caller wrote a dotted target": `proxy.forwardReleasedItem`
    is dotted, and `proxy` is a MODULE — a path a caller may reach through `./facade` without ever
    spelling it. The text of the target cannot tell it from `StrategyChain.resolve`, and neither can the
    node's `Method` label alone: a backend that labels an object-literal method or a Go receiver
    `Method` hands the scan a qualified name with no class in it. What can is the class the backend
    records as DEFINING the method, so a `Method` node with no class behind it, or behind it a class
    that is not the one the target named, is never scanned. CONTROL: the same rows over a method whose
    defining class IS the qualifier."""
    def rows(label: str = "Method") -> list[dict]:
        return [_chain_row(i, target_label=label, target_qn="pkg.proxy.forwardReleasedItem",
                           target_name="forwardReleasedItem", target_file="src/proxy.ts",
                           callee="forwardReleasedItem") for i in range(4)]

    defined_by_it = _scanned_callers(
        monkeypatch, tmp_path, rows(), target="proxy.forwardReleasedItem", owner="pkg.proxy")
    assert _seen(defined_by_it) == [False] * 4, "the control: a class named `proxy` defines it"

    no_class = _scanned_callers(monkeypatch, tmp_path, rows(), target="proxy.forwardReleasedItem",
                                owner=None)
    assert _seen(no_class) == [None] * 4, no_class["rows"]
    assert "Checked:" not in no_class["result"] and "_Settle it:" in no_class["result"], no_class["result"]

    other_class = _scanned_callers(monkeypatch, tmp_path, rows(), target="proxy.forwardReleasedItem",
                                   owner="pkg.facade.Facade")
    assert _seen(other_class) == [None] * 4, other_class["rows"]

    function = _scanned_callers(monkeypatch, tmp_path, rows("Function"),
                                target="proxy.forwardReleasedItem", owner="pkg.proxy")
    assert _seen(function) == [None] * 4, "a `Function` node is no member of a class"
    assert "Checked:" not in function["result"], function["result"]


def test_a_failed_lookup_of_the_defining_class_leaves_the_answer_as_it_was(monkeypatch, tmp_path):
    """The lookup is an improvement on an answer that is already whole, so it fails closed: nothing is
    scanned, the reader is handed the command as before, and the failure is not a gap — the answer
    claims nothing it did not check. CONTROL: the same rows with the lookup answering are scanned."""
    rows = _guesses(4)
    control = _scanned_callers(monkeypatch, tmp_path, rows)
    assert _seen(control) == [False] * 4, control["rows"]

    _write_tree(tmp_path, rows)
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    listing = {"projects": [{"name": "codeintel", "root_path": str(tmp_path)}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))

    def answer(cypher, project, timeout_ms):
        if "[:DEFINES_METHOD]" in cypher:
            if "m.qualified_name IN" in cypher:
                raise RuntimeError("the lookup blew up")      # the one that asks for the owner
            return []
        return [dict(r) for r in rows]

    monkeypatch.setattr(p, "_query_rows", answer)
    env = p.build_result("callers", f"{_TOKEN}.resolve", [], 30000, str(tmp_path))

    assert _seen(env) == [None] * 4, env["rows"]
    assert "_Settle it:" in env["result"] and "Checked:" not in env["result"], env["result"]
    assert {g["kind"] for g in env["gaps"]} == {g["kind"] for g in control["gaps"]}, env["gaps"]


def test_a_row_in_the_file_that_defines_the_symbol_is_never_scanned(monkeypatch, tmp_path):
    """The `same_module` case. The backend binds that strategy inside the caller's OWN file, so the
    file is the one that defines the callee — and the file that declares `class StrategyChain` writes
    `StrategyChain` whatever the call does. The verdict could only ever be `true`, which would rank a
    row downgraded for being written through a receiver (`subprocess.resolve(...)`) ABOVE its peers on
    evidence that cannot discriminate. So it is left unjudged, keeps its `qualified call` badge, and
    sits between the two verdicts. CONTROL: its peers, in other files, are refuted."""
    foreign = _chain_row(0, file=_CHAIN_FILE, strategy="same_module", conf="0.90",
                         callee="subprocess.resolve", name="shells_out")
    rows = [foreign, *(_chain_row(i) for i in range(1, 5))]
    env = _scanned_callers(monkeypatch, tmp_path, rows)
    by_name = {r["name"]: r for r in env["rows"]}

    assert _TOKEN in (tmp_path / _CHAIN_FILE).read_text(), "premise: a scan of it could only say true"
    assert by_name["shells_out"]["evidence"] == "name-matched", by_name["shells_out"]
    assert by_name["shells_out"]["qualifier_seen"] is None, by_name["shells_out"]
    assert [r["qualifier_seen"] for r in env["rows"] if r["name"] != "shells_out"] == [False] * 4
    line = next(ln for ln in _row_lines(env["result"]) if "shells_out" in ln)
    assert "[?0.90 qualified call]" in line and "never writes" not in line, line
    assert "_Checked: **4 of 5**" in env["result"] and "1 could not be judged" in env["result"]


@pytest.mark.parametrize("file,callee,defined_in", [
    ("src/agents/f{i}.ts", "this.resolve", "src/chain.ts"),
    ("src/agents/f{i}.py", "self.resolve", "src/chain.py")])
def test_a_call_on_the_enclosing_object_is_never_scanned(
        monkeypatch, tmp_path, file, callee, defined_in):
    """`self.m()` and `this.m()` reach the method through the CALLER'S OWN class, by inheritance, and
    never by writing the ancestor's name — the class hierarchy is the question, and a text search
    cannot ask it. A subclass in another file that inherits `resolve` through a base this index could
    not place is exactly the row a scan would wrongly refute. CONTROL: `this.chain.resolve` is a
    field's method, and is scanned."""
    own = _scanned_callers(
        monkeypatch, tmp_path,
        [_chain_row(i, file=file.format(i=i), callee=callee, target_file=defined_in)
         for i in range(3)])
    assert _seen(own) == [None] * 3, own["rows"]
    assert "Checked:" not in own["result"], own["result"]

    field = callee.replace(".resolve", ".chain.resolve")
    scanned = _scanned_callers(
        monkeypatch, tmp_path,
        [_chain_row(i, file=file.format(i=i), callee=field, target_file=defined_in)
         for i in range(3)])
    assert _seen(scanned) == [False] * 3, scanned["rows"]


def test_module_scope_code_is_never_scanned(monkeypatch, tmp_path):
    """The backend attributes a module's scope to the file path it has for that node and has been
    seen naming a SIBLING file there (`aliases.ini` for `aliases.py`), so a miss in that file is not
    evidence about the code the row is about. CONTROL: a function in the same file is scanned."""
    module = _chain_row(0, file="src/agents/top.ts", caller_label="Module", name="top")
    module["a.qualified_name"] = "pkg.agents.top.__file__"
    function = _chain_row(1, file="src/agents/top.ts", name="inside")
    rows = [module, function, *(_chain_row(i) for i in range(2, 5))]
    env = _scanned_callers(monkeypatch, tmp_path, rows)

    scope = next(r for r in env["rows"] if r["module_scope"])
    assert scope["qualifier_seen"] is None, scope
    assert next(r for r in env["rows"] if r["name"] == "inside")["qualifier_seen"] is False, env["rows"]


def test_the_scan_applies_to_callers_and_never_to_callees(monkeypatch, tmp_path):
    """On `callees` the displayed rows are what the target CALLS, and a callee's file has no reason to
    write the target's own class — a scan there would refute every callee that lives elsewhere.
    CONTROL: the same target asked as `callers` is scanned."""
    rows = _guesses(4)
    as_callers = _scanned_callers(monkeypatch, tmp_path, rows)
    assert _seen(as_callers) == [False] * 4, as_callers["rows"]

    callees = []
    for i in range(4):
        row = _chain_row(i)
        callees.append({
            "a.name": "resolve", "a.qualified_name": "pkg.chain.StrategyChain.resolve",
            "a.file_path": _CHAIN_FILE, "labels(a)": json.dumps(["Method"]),
            "type(c)": "CALLS", "c.confidence": "0.75", "strategy": "unique_name", "callee": f"h{i}",
            "b.name": f"h{i}", "b.qualified_name": f"pkg.helpers.Helper.h{i}",
            "b.file_path": row["a.file_path"], "labels(b)": json.dumps(["Method"]),
        })
    env = _scanned_callers(monkeypatch, tmp_path, callees, op="callees")

    assert env["rows"] and all(r["relation"] == "callee" for r in env["rows"]), env["rows"]
    assert all(r["qualifier_seen"] is None for r in env["rows"]), env["rows"]
    assert "Checked:" not in env["result"] and "never writes" not in env["result"], env["result"]


def test_the_scan_lines_can_never_be_read_as_result_rows(monkeypatch, tmp_path):
    """`bench/score.py::graph_answer` parses the body back into caller keys and takes every line
    starting with `- ` as a row. The result line is prose in the settle note's position, and it must
    stay out of that shape — or the benchmark would score the disclosure as a fabricated caller."""
    env = _scanned_callers(monkeypatch, tmp_path, _guesses(4), missing={3})
    added = [ln for ln in env["result"].splitlines() if "Checked:" in ln or "Re-run it yourself" in ln]

    assert len(added) == 2, env["result"]
    assert not any(ln.startswith("- ") for ln in added), added
    assert len(_row_lines(env["result"])) == env["evidence"]["returned"] == 4, env["result"]


@pytest.mark.parametrize("ext,callee", [
    ("java", "this.resolve"), ("java", "resolve"), ("kt", "resolve"), ("rb", "resolve"),
    ("go", "c.resolve"), ("rs", "self.resolve"), ("cs", "resolve")])
def test_a_row_in_a_language_with_no_own_receiver_rule_is_never_scanned(
        monkeypatch, tmp_path, ext, callee):
    """`_OWN_RECEIVERS` is defined for Python, JavaScript and TypeScript only. In Java, Kotlin, C# or
    Ruby an inherited member is reached with NO receiver (`resolve()`), and `this`/`self` do it in every
    language here — from a file that never names the ancestor, which is the file a scan would refute.
    Where the language's rule is not defined the row is left `null`, the same fail-closed answer
    `_same_module_call` gives. CONTROL: a TypeScript row written through a receiver that is not the
    enclosing object is scanned."""
    rows = [_chain_row(i, file=f"src/agents/F{i}.{ext}", callee=callee,
                       target_file=f"src/Chain.{ext}") for i in range(4)]
    env = _scanned_callers(monkeypatch, tmp_path, rows)
    assert _seen(env) == [None] * 4, env["rows"]
    assert "Checked:" not in env["result"] and "never writes" not in env["result"], env["result"]

    control = _scanned_callers(monkeypatch, tmp_path, _guesses(4, callee="chain.resolve"))
    assert _seen(control) == [False] * 4, control["rows"]


def test_a_row_with_no_recorded_call_text_is_never_scanned(monkeypatch, tmp_path):
    """A blank `callee` says nothing about the receiver — an older index, or an edge the extractor left
    blank — so a `self.m()` call could be hiding behind it, and `_same_module_call` treats it as
    unchecked for that reason. A scan that judged it would fail open. CONTROL: the same rows with the
    text recorded."""
    blank = _scanned_callers(monkeypatch, tmp_path, _guesses(4, callee=""))
    assert _seen(blank) == [None] * 4, blank["rows"]
    assert "Checked:" not in blank["result"], blank["result"]

    recorded = _scanned_callers(monkeypatch, tmp_path, _guesses(4, callee="chain.resolve"))
    assert _seen(recorded) == [False] * 4, recorded["rows"]


def test_the_scan_and_the_same_module_rule_share_one_definition_of_the_enclosing_object():
    """Two copies of "is this receiver the enclosing object" drift: one learns `this?.` or `super(`
    and the other does not. They ask `_is_own_receiver` and `_own_receivers_of`."""
    import inspect

    from codeintel.graph_confidence import _same_module_call

    for fn in (GraphProvider._text_can_judge, _same_module_call):
        source = inspect.getsource(fn)
        assert "_is_own_receiver(" in source and "_own_receivers_of(" in source, fn.__name__
        assert "_OWN_RECEIVERS" not in source.split('"""')[2], (
            f"{fn.__name__} reads the table itself instead of asking the shared helper")


def test_below_the_floor_nothing_is_scanned_badged_or_re_sorted(monkeypatch, tmp_path):
    """The note is printed only when name-matched rows are at least `_SETTLE_FLOOR` and at least half
    the answer. A scan that ran below that floor marked rows `qualifier_seen: false` and badged them
    `[never writes …]` with no `Checked:` line, no command and no caveat anywhere in the body — a mark
    with its qualification left off. So below the floor nothing is read, asked, marked or re-sorted.
    CONTROL: at the floor, the same shape is scanned."""
    two = _guesses(2)
    queries: list[str] = []
    env = _scanned_callers(monkeypatch, tmp_path, two, queries=queries)
    assert _seen(env) == [None, None], env["rows"]
    assert "never writes" not in env["result"] and "Checked:" not in env["result"], env["result"]
    assert [r["name"] for r in env["rows"]] == ["c0", "c1"], "re-sorted although nothing was judged"
    assert not any("m.qualified_name IN" in q for q in queries), "asked for a class it would not use"

    resolved = [_chain_row(i, strategy="import_map", conf="0.95") for i in range(5)]
    outweighed = _scanned_callers(monkeypatch, tmp_path, [*resolved, *(_chain_row(i) for i in range(5, 8))])
    assert _seen(outweighed) == [None] * 8, "3 name-matched rows of 8 are under half the answer"
    assert "never writes" not in outweighed["result"], outweighed["result"]

    at_floor = _scanned_callers(monkeypatch, tmp_path, _guesses(3))
    assert _seen(at_floor) == [False] * 3, at_floor["rows"]
    half = _scanned_callers(monkeypatch, tmp_path, [*resolved[:3], *(_chain_row(i) for i in range(3, 6))])
    assert _seen(half).count(False) == 3, "3 of 6 is half the answer, which is enough"


def test_every_answer_that_badges_a_row_carries_the_note_and_the_command(monkeypatch, tmp_path):
    """The invariant behind `server.py`'s "it has already run it, and says what it found" and the
    guide's "the command is kept beneath": over a spread of answers, a body that contains a
    `[never writes …]` badge also contains the `Checked:` line, the caveat and the `rg` command."""
    resolved = [_chain_row(i, strategy="import_map", conf="0.95", name=f"r{i}") for i in range(3)]
    shapes = {
        "all guesses": _guesses(5),
        "guesses and some resolved": [*resolved[:2], *(_chain_row(i) for i in range(2, 7))],
        "outweighed": [*resolved, *(_chain_row(i) for i in range(3, 5))],
        "two guesses": _guesses(2),
        "none": [],
    }
    badged = 0
    for label, rows in shapes.items():
        if not rows:
            continue
        body = _scanned_callers(monkeypatch, tmp_path, rows)["result"]
        if "[never writes" in body:
            badged += 1
            assert "_Checked:" in body, (label, body)
            assert "a text search of the files as they are on disk" in body, (label, body)
            assert "rg -n --fixed-strings -- 'StrategyChain' " in body, (label, body)
    assert badged >= 2, "the spread must include answers that badge, or this proves nothing"


def test_the_count_is_over_the_symbols_the_answer_prints(monkeypatch, tmp_path):
    """`N of M` is about the rows SHOWN. When thirteen distinct symbols carry the name, the answer
    prints twelve and withholds the rest (`_CANDIDATE_CAP`), and a count taken over the thirteenth's
    rows is a count of rows no reader can see — one a verifier re-deriving it from `rows[]` could not
    reproduce."""
    rows = [_chain_row(i, target_qn=f"pkg.m{i}.StrategyChain.resolve", target_file=f"src/m{i}.ts")
            for i in range(13)]
    env = _scanned_callers(monkeypatch, tmp_path, rows)

    assert len(env["rows"]) == 12 and env["evidence"]["total"] == 13, env["evidence"]
    assert _checked_counts(env) == (12, 12), env["result"][:900]
    assert "_Checked: **12 of 12** name-matched callers shown" in env["result"], env["result"][:900]
