"""A partial answer must never be renderable as an empty one.

These are the tests that would have caught the worst bug found in the 2026-08-17 evaluation: a cold
language server timed out, the reference lookup returned nothing, and the renderer emitted

    ## References
    (none)

with `ok: true`, no `reason` and no `hint` — a confident false statement, byte-identical to a true
one, answering the single question an agent asks before deleting code.

Two tiers here:

* the *starvation* test enumerates every op on every provider and drives it with a backend that
  answers nothing. It asserts the class rather than the instance, so an op added next year is
  covered without anyone remembering to add a case — the same reasoning the graph provider's own
  renderer sweep already uses.
* the coordinate tests pin the 0-based → 1-based conversion that two separate renderers each got
  wrong independently.
"""

from __future__ import annotations

import inspect

import pytest

from codeintel.loc import line1, loc, span
from codeintel.outcome import Missing, Ok, is_missing

# Strings that assert "there is nothing here". None of them may appear in a body that was produced
# without the backend actually answering.
_EMPTINESS_CLAIMS = ("(none)", "(none found)", "(no matches)", "(not found)", "(0)")


# --------------------------------------------------------------------------- outcome basics

def test_ok_of_empty_is_not_missing():
    """`Ok([])` is a real answer — asked, and there is nothing. Conflating it with a failure is the
    bug in the other direction, and it would make every true negative unusable."""
    assert not is_missing(Ok([]))
    assert not is_missing(Ok({}))
    assert is_missing(Missing("timeout"))


def test_every_missing_describes_itself():
    """A gap with no explanation is only marginally better than no gap at all."""
    for kind in ("not-asked", "timeout", "backend-error", "unparsable", "unsupported"):
        assert Missing(kind).describe().strip()


# --------------------------------------------------------------------------- coordinates (B10)

@pytest.mark.parametrize("line0,expected", [(0, 1), (1, 2), (193, 194), (5681, 5682)])
def test_line_conversion_is_one_based(line0, expected):
    assert line1(line0) == expected


def test_no_rendered_location_is_ever_line_zero():
    """`path:0` is the tell. It is not a valid line number in any editor, and it was shipping from
    two different engines — LSP body locations and semantic chunk starts."""
    for line0 in range(0, 200):
        assert not loc("a.py", line0).endswith(":0")
        assert not span("a.py", line0, line0 + 3).split("-")[0].endswith(":0")


def test_location_without_a_usable_line_renders_a_bare_path():
    """Better to point at the file than to invent a position."""
    assert loc("a.py", None) == "a.py"
    assert loc("a.py", "not-a-number") == "a.py"
    assert loc("a.py", -1) == "a.py"


def test_span_renders_one_based_range():
    # serena reports the `def` at 117 for a symbol that a human sees on line 118.
    assert span("board_mirror.py", 117, 158) == "board_mirror.py:118-159"


# --------------------------------------------------------------------------- starvation (B1)

class _StarvedLspSession:
    """Stands in for a language server that accepted the connection and then answers nothing —
    exactly the cold-start state, where serena is up but the workspace has not finished loading."""

    _mcp_session = None
    _loop = None
    _lock = __import__("threading").Lock()
    state = None


def _lsp_provider():
    from codeintel.providers.lsp import LspProvider

    p = LspProvider.__new__(LspProvider)  # bypass backend detection
    p._last_backend_error = None
    p._pending_gaps = ()
    return p


def test_starved_symbol_never_claims_zero_references():
    """The regression test for B1, at the renderer.

    `find_symbol` succeeds and `find_referencing_symbols` fails. That exact combination had no test
    — there was one for every call failing, and one for a genuinely empty reference map, but not for
    the half-and-half case that actually occurs on a cold server."""
    p = _lsp_provider()
    calls = {"n": 0}

    def _fake_call_tool(session, tool, args, timeout_s):
        calls["n"] += 1
        if tool == "find_symbol":
            import json as _json

            return Ok(_json.dumps([{
                "kind": "Function", "relative_path": "src/factory.ts",
                "name_path": "makeWidgetFactory", "body": "export function makeWidgetFactory() {}",
                "body_location": {"start_line": 0, "end_line": 2},
            }]))
        return Missing("timeout", "the language server had not finished loading this workspace")

    p._call_tool = _fake_call_tool  # type: ignore[method-assign]
    body = p._op_symbol(_StarvedLspSession(), "makeWidgetFactory", "/repo", 5.0)

    assert body is not None, "a resolvable definition must still be returned"
    assert "## Symbol: makeWidgetFactory" in body
    # The definition half is intact and useful — a gate that discarded it would be its own bug.
    assert "src/factory.ts:1-3" in body, "definition location must be 1-based"
    # The references half must not assert emptiness.
    for claim in _EMPTINESS_CLAIMS:
        assert claim not in body, f"starved reference lookup rendered {claim!r}"
    assert "not retrieved" in body
    assert p._pending_gaps, "a missing section must record a machine-readable gap"
    assert p._pending_gaps[0]["section"] == "references"
    assert p._pending_gaps[0]["kind"] == "timeout"


def test_genuinely_empty_references_are_still_expressible():
    """The counterpart. Asked, answered, nothing there — that must remain sayable, and must NOT be
    reported as a gap, or every true negative becomes noise."""
    p = _lsp_provider()

    def _fake_call_tool(session, tool, args, timeout_s):
        import json as _json

        if tool == "find_symbol":
            return Ok(_json.dumps([{
                "kind": "Function", "relative_path": "src/factory.ts",
                "name_path": "orphan", "body": "function orphan() {}",
                "body_location": {"start_line": 0, "end_line": 1},
            }]))
        return Ok(_json.dumps({}))

    p._call_tool = _fake_call_tool  # type: ignore[method-assign]
    body = p._op_symbol(_StarvedLspSession(), "orphan", "/repo", 5.0)

    assert body is not None
    assert "## References (0)" in body
    assert "not retrieved" not in body
    assert not p._pending_gaps, "a true negative is not a gap"


def test_the_two_answers_are_different_bytes():
    """The whole contract, stated as one assertion: 'could not ask' and 'nothing there' must not
    render identically. This is what made the original bug invisible to every caller."""
    import json as _json

    def _provider(ref_outcome):
        p = _lsp_provider()

        def _call(session, tool, args, timeout_s):
            if tool == "find_symbol":
                return Ok(_json.dumps([{
                    "kind": "Function", "relative_path": "f.ts", "name_path": "x",
                    "body": "x", "body_location": {"start_line": 0, "end_line": 0},
                }]))
            return ref_outcome

        p._call_tool = _call  # type: ignore[method-assign]
        return p._op_symbol(_StarvedLspSession(), "x", "/repo", 5.0)

    failed = _provider(Missing("timeout", "still loading"))
    empty = _provider(Ok(_json.dumps({})))
    assert failed != empty


def test_every_lsp_op_survives_a_backend_that_answers_nothing():
    """Enumerate rather than list: any op added later is covered without anyone remembering.

    A starved backend may legitimately produce no answer at all (None → a safe-null carrying a
    reason). What it may never produce is a body that asserts emptiness."""
    p = _lsp_provider()
    p._call_tool = lambda *a, **k: Missing("timeout", "nothing is answering")  # type: ignore[method-assign]

    op_methods = [
        name for name, fn in inspect.getmembers(type(p), inspect.isfunction)
        if name.startswith("_op_")
    ]
    assert op_methods, "no ops discovered — the reflection has broken, not the code"

    for name in op_methods:
        p._pending_gaps = ()
        body = getattr(p, name)(_StarvedLspSession(), "anything", "/repo", 5.0)
        if body is None:
            continue  # routed to a safe-null with a reason — the honest outcome
        for claim in _EMPTINESS_CLAIMS:
            assert claim not in body, f"{name} rendered {claim!r} from a backend that said nothing"


# --------------------------------------------------------------------------- redaction (B9)

def test_home_paths_are_redacted_from_every_envelope_field():
    """The leak was in `hint` AND inside `result` (a scope note a renderer built), which is why
    this is asserted over the whole envelope rather than over the fields anyone remembered."""
    import os

    from codeintel.redact import contains_home_path, redact

    home = os.path.expanduser("~")
    env = {
        "ok": True, "op": "overview", "target": "",
        "result": f"> Scope: `{home}/work/app` is not indexed on its own",
        "hint": f"Index it standalone with: codeintel index {home}/work/app",
        "gaps": [{"section": "refs", "detail": f"looked in {home}/work/app"}],
    }
    out = redact(env)
    blob = f"{out['result']} {out['hint']} {out['gaps']}"
    assert not contains_home_path(blob), blob
    # Still actionable: a shell expands `~` back to the right directory.
    assert "codeintel index ~/work/app" in out["hint"]


def test_redaction_survives_the_private_prefix():
    """macOS reports realpaths under /private, so a leak can arrive with a prefix that a plain
    home-prefix comparison misses."""
    import os

    from codeintel.redact import contains_home_path, redact_text

    home = os.path.expanduser("~")
    assert not contains_home_path(redact_text(f"/private{home}/x/y"))


# --------------------------------------------------------------------------- confidence contract

def test_every_engine_stamps_confidence_on_an_answered_envelope():
    """`confidence` was introduced on the LSP provider alone, which reproduced in miniature the
    defect it exists to fix: the MCP instructions tell callers to check a field that two of three
    engines never set, and an ABSENT field is ambiguous — "complete" and "this engine does not
    report" look identical. Enumerate the providers so a fourth engine cannot quietly skip it."""
    import inspect

    from codeintel import providers
    from codeintel.provider import attach_confidence

    answered = {"ok": True, "op": "x", "target": "y", "result": "BODY",
                "engine": "e", "cached": False}
    assert attach_confidence(answered)["confidence"] == "complete"
    assert attach_confidence(answered, [{"section": "s", "kind": "k", "detail": "d"}])["confidence"] == "partial"
    # A null result keeps `reason` as its whole story; stamping it would imply a body exists.
    assert "confidence" not in attach_confidence({**answered, "result": None})

    # Each provider module must route its answered envelope through the shared helper rather than
    # stamping the field by hand — hand-stamping is how they drifted apart the first time.
    for name in ("graph", "lsp", "semantic"):
        mod = __import__(f"codeintel.providers.{name}", fromlist=["x"])
        src = inspect.getsource(mod)
        assert "attach_confidence" in src, f"{name} provider does not stamp confidence"
        assert '"confidence"] =' not in src, f"{name} provider hand-stamps confidence"
    assert providers is not None


def test_hotspots_ranks_across_languages():
    """The gate on `hotspots` being reinstated.

    It was withdrawn because both evaluated repositories ranked 100% `.tsx` — every Python and
    backend-TypeScript function was absent, which reads exactly like a ranking that considered them
    and found them simpler. Two request bugs caused it: only `Function` nodes were requested (a
    class method is a `Method` node) and the candidate set was capped at 200 rows returned in NAME
    order, so the client-side sort ranked an alphabetical slice. This pins both.
    """
    from codeintel.providers.graph import GraphProvider

    rows = [
        {"name": "handler", "qualified_name": "svc.handler", "file_path": "src/svc.py",
         "complexity": 40, "in_degree": 3, "out_degree": 9, "lines": 200},
        {"name": "Widget", "qualified_name": "ui.Widget", "file_path": "src/ui/Widget.tsx",
         "complexity": 30, "in_degree": 2, "out_degree": 5, "lines": 150},
        {"name": "buildArgv", "qualified_name": "runner.buildArgv", "file_path": "src/runner.ts",
         "complexity": 20, "in_degree": 1, "out_degree": 4, "lines": 90},
    ]
    gp = GraphProvider.__new__(GraphProvider)
    gp._pending_gaps = ()
    labels: list[str] = []
    limits: list[int] = []

    def _search(payload, project, timeout_ms):
        labels.append(payload.get("label"))
        limits.append(int(payload.get("limit") or 0))
        # The backend returns each label's own rows; split so neither label is a superset.
        return [r for r in rows if (payload.get("label") == "Method") == r["name"][0].isupper()]

    gp._search_symbols = _search              # type: ignore[method-assign]
    gp._is_noise = lambda r: False            # type: ignore[method-assign]

    body = gp._op_hotspots("proj", 5000)
    assert body is not None
    assert set(labels) == {"Function", "Method"}, f"labels requested: {labels}"
    assert min(limits) >= 1000, "candidate set too small to rank rather than sample"
    for path in ("src/svc.py", "src/ui/Widget.tsx", "src/runner.ts"):
        assert path in body, f"{path} missing from a mixed-language ranking"
    # Highest complexity first, regardless of which label it arrived under.
    assert body.index("src/svc.py") < body.index("src/ui/Widget.tsx")


def test_a_single_language_ranking_says_so():
    """When a ranking really is one file type, that must be stated rather than left to look like a
    considered result — the failure mode that hid the bug above for two whole repositories."""
    from codeintel.providers.graph import _language_coverage_note

    only_tsx = [{"file_path": f"src/C{i}.tsx"} for i in range(10)]
    assert "tsx" in _language_coverage_note(only_tsx)
    mixed = [{"file_path": f"src/m{i}.py"} for i in range(5)] + only_tsx[:5]
    assert _language_coverage_note(mixed) == ""
