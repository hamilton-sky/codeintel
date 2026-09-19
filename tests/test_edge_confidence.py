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


# --- the qualifier scan ---------------------------------------------------------------------------
#
# `#34` derived the discriminating token and printed the `rg` for it. These pin the step after: the
# answer runs that check itself and publishes the result per row. Everything here is about what the
# result may and may not be used to claim — the scan narrows an answer, it never empties one.

def _scanned(tmp_path, *, naming: set[int], n: int = 4, resolved: int = 1, missing: set[int] = ()):
    """Render `_guessed` against a REAL tree, so the scan actually runs.

    `naming` is the indices of the name-matched rows whose file mentions `StrategyChain`; `missing`
    the ones whose file is not written at all, which is how an unreadable path reaches the scan.
    """
    gp, groups, wanted = _guessed(n=n, resolved=resolved)
    src = tmp_path / "src" / "agents"
    src.mkdir(parents=True)
    for i in range(resolved + n):
        if i in missing:
            continue
        body = "export const x = 1;\n"
        if i in naming:
            body += "import { StrategyChain } from './chain';\n"
        (src / f"f{i}.ts").write_text(body)
    (tmp_path / "src" / "chain.ts").write_text("export class StrategyChain {}\n")
    gp._answered_root = str(tmp_path)
    return gp, _render(gp, groups, wanted)


def test_the_note_states_what_the_scan_found_rather_than_how_to_find_it(tmp_path):
    """The whole point of the change. `#34` told a reader to run `rg`; the token, the root and the
    rows in doubt are all in hand at render time, so the answer states the result.

    The command stays in the body underneath. A claim a reader cannot re-run is a claim they have to
    take on trust, which is the thing this note exists to avoid.
    """
    _, body = _scanned(tmp_path, naming=set(), n=4)

    assert "_Checked: **4 of 4**" in body, body
    assert "never write `StrategyChain`" in body, body
    assert "rg -n --fixed-strings 'StrategyChain'" in body, body


def test_the_scan_never_removes_a_row(tmp_path):
    """The decided constraint, pinned where it would be easiest to break.

    Dropping a disproved row would trade a false positive for a false negative, and `corpus-ts`
    prices that trade: filtering the three fabricated rows off `FallbackChain.resolve` also empties
    an answer for a symbol that has a caller. The scan ranks and labels; the caller decides.
    """
    _, body = _scanned(tmp_path, naming=set(), n=4)

    assert len(_row_lines(body)) == 5, body        # 4 name-matched + 1 resolved, all still printed


def test_a_disproved_row_ranks_last_but_never_above_a_binding(tmp_path):
    """Ordering is the lever the scan is allowed to pull, and only within a bucket.

    A row whose file names the qualifier sorts above one whose file does not — but neither can rise
    above a `resolved` row, because the scan is weaker evidence than a followed binding and ordering
    it as though it were equal would be the same over-claim in a new place.
    """
    gp, _ = _scanned(tmp_path, naming={1, 2}, n=4, resolved=1)

    seen = [r.get("qualifier_seen") for r in gp._pending_rows]
    assert seen[0] is None, seen                   # the resolved row, never scanned
    assert seen[1:3] == [True, True], seen
    assert seen[3:] == [False, False], seen


def test_a_resolved_row_is_never_marked_by_the_scan(tmp_path):
    """`null`, not `true`. A row that followed a real binding is not in the scanned population, so
    reporting either verdict for it would be a claim about a file nobody opened — and `false` would
    invite the reading that a proven caller is suspect for not writing the class's name."""
    gp, _ = _scanned(tmp_path, naming=set(), n=2, resolved=2)

    resolved = [r for r in gp._pending_rows if r["verified"]]
    assert resolved, gp._pending_rows
    assert all(r["qualifier_seen"] is None for r in resolved), resolved


def test_an_unreadable_file_is_unknown_and_not_counted_as_absent(tmp_path):
    """The distinction every summary in this project exists to keep. "This file does not name
    `StrategyChain`" and "we could not open this file" are different facts, and collapsing them
    would let a missing path argue that a row is spurious."""
    gp, body = _scanned(tmp_path, naming=set(), n=4, missing={3, 4})

    seen = [r.get("qualifier_seen") for r in gp._pending_rows if not r["verified"]]
    assert seen.count(None) == 2, seen
    assert seen.count(False) == 2, seen
    assert "_Checked: **2 of 4**" in body, body
    assert "2 could not be read and are left unjudged" in body, body


def test_the_evidence_block_counts_what_the_scan_decided(tmp_path):
    """The envelope's counts are the rows' own values, not a second derivation that could drift."""
    gp, _ = _scanned(tmp_path, naming={1}, n=4, resolved=1)
    ev = gp._settle_evidence()

    assert ev["qualifier_present"] == 1, ev
    assert ev["qualifier_absent"] == 3, ev
    assert ev["qualifier_present"] + ev["qualifier_absent"] == ev["possible"], ev


def test_without_a_readable_root_the_answer_falls_back_to_printing_the_command(tmp_path):
    """No scan, no claim. A provider answering about a root that is not on disk must not report
    "0 of 43 name it" — it must hand the reader the check, which is `#34`'s behaviour unchanged."""
    gp, groups, wanted = _guessed(n=43, resolved=2)
    gp._answered_root = str(tmp_path / "does-not-exist")
    body = _render(gp, groups, wanted)

    assert "_Settle it:" in body, body
    assert "Checked:" not in body, body
    assert all(r["qualifier_seen"] is None for r in gp._pending_rows), gp._pending_rows


def test_a_bare_target_is_never_scanned_because_a_re_export_defeats_the_stem(tmp_path):
    """The limit that a measurement imposed, not a cautious guess.

    For a bare target `_discriminator` falls back to the stem of the defining file, and a caller
    reaching the symbol through a re-export never writes that stem. Measured on `corpus-ts`:
    `callerFacade.ts` imports `forwardReleasedItem` from `./facade`, contains no `proxy`, and the
    scan disproved a TRUE caller. `settleQueue` survived the identical shape only because `settle`
    is a substring of `settleFacade` — luck about a filename, not a fact about the code.

    A class qualifier names an entity a caller must get hold of; a file stem names a path it may
    never spell. So the scan runs for the first and not the second, and a bare target keeps the
    printed command it has had since `#34`.
    """
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget

    gp, _groups, _wanted = _guessed(n=4)
    src = tmp_path / "src"
    src.mkdir(parents=True)
    for i in range(4):
        (src / f"f{i}.ts").write_text("export const x = 1;\n")
    gp._answered_root = str(tmp_path)

    # A bare target whose BACKEND qualified name is dotted — the subtle half of the bug. The
    # discriminator falls back to `qn_raw` and yields `proxy` through its qualifier branch, so a
    # gate on "which branch fired" passes and a module path gets scanned as though it were a class.
    # None of these callers writes `proxy`; `callerFacade.ts` reaches the symbol via `./facade`.
    bare = [_EdgeGroup("forwardReleasedItem", "src.proxy.forwardReleasedItem", "src/proxy.ts", [
        {"a.name": f"c{i}", "a.qualified_name": f"pkg.c{i}", "a.file_path": f"src/f{i}.ts",
         "type(c)": "CALLS", "_bucket": "name-matched"} for i in range(4)])]
    body = _render(gp, bare, _SymbolTarget("forwardReleasedItem"), leaf="forwardReleasedItem")

    assert all(r["qualifier_seen"] is None for r in gp._pending_rows), gp._pending_rows
    assert "Checked:" not in body, body
    assert "_Settle it:" in body, body
