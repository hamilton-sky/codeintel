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
import pathlib

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
    for line0 in range(200):
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

def _provider_modules():
    """Every provider module ON DISK — not a hardcoded list.

    The previous version of this guard iterated the literal tuple ("graph", "lsp", "semantic")
    while its own docstring claimed "a fourth engine cannot skip it". A fourth engine could, and
    `none.py` already was one. A guard whose domain is typed by hand certifies the sites the last
    bug was found at, which is exactly the habit that let the same defect reappear three times.
    """
    import importlib

    import codeintel.providers as pkg

    d = pathlib.Path(pkg.__file__).parent
    names = sorted(f.stem for f in d.glob("*.py") if f.stem != "__init__")
    return [(n, importlib.import_module(f"codeintel.providers.{n}")) for n in names]


def test_every_provider_module_is_discovered_from_disk():
    """The guard's domain must grow when the package does."""
    names = [n for n, _ in _provider_modules()]
    assert set(names) >= {"graph", "lsp", "semantic", "none"}, names


def _non_null_envelope_constructors():
    """Every dict literal in the tree that builds an envelope with a NON-NULL `result`.

    This is the census the fixes kept being written without. `confidence` was invented as an
    invariant over "answers that have a body", and the population of places that build such an
    answer is mechanically decidable — so decide it here rather than trusting a hand-typed list
    that was wrong twice (it missed `gateway._merge`, and it could not see a fourth provider).

    Returns (file, lineno, wrapped_in_attach_confidence) per construction site.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "codeintel"
    sites = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        wrapped_lines = set()
        wrapped_names = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "attach_confidence"):
                continue
            for sub in ast.walk(node):
                wrapped_lines.add(getattr(sub, "lineno", -1))
            # Both shapes count: the literal passed inline, and the literal built into a local
            # that is then wrapped on a later line.
            if node.args and isinstance(node.args[0], ast.Name):
                wrapped_names.add(node.args[0].id)
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            if targets and any(t in wrapped_names for t in targets) and node.value is not None:
                for sub in ast.walk(node.value):
                    wrapped_lines.add(getattr(sub, "lineno", -1))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "result" not in keys or "engine" not in keys:
                continue
            value = node.values[keys.index("result")]
            if isinstance(value, ast.Constant) and value.value is None:
                continue  # a null-result envelope has nothing to be partial about
            sites.append((path.name, node.lineno, node.lineno in wrapped_lines))
    return sites


def test_every_non_null_envelope_constructor_stamps_confidence():
    """The invariant, enforced over its whole population instead of over the last bug's location.

    The previous guard asserted the STRING "attach_confidence" appeared in three hand-named modules
    — satisfied by an import line, blind to `gateway._merge`, and blind to any fourth provider. It
    passed while two construction sites shipped unqualified answers."""
    sites = _non_null_envelope_constructors()
    assert len(sites) >= 4, f"census looks broken, found only {sites}"
    unwrapped = [(f, ln) for f, ln, ok in sites if not ok]
    assert not unwrapped, (
        "these build a non-null envelope without attach_confidence, so their answers claim "
        f"completeness they never established: {unwrapped}"
    )


def test_the_census_guard_can_actually_fail(tmp_path, monkeypatch):
    """A guard that cannot fail is worse than no guard: it converts 'we did not check' into 'green'.

    Prove this one fails by pointing the census at a tree containing a violation."""
    import ast

    src = tmp_path / "src" / "codeintel"
    src.mkdir(parents=True)
    (src / "rogue.py").write_text(
        'def build():\n'
        '    return {"ok": True, "op": "x", "target": "y", "result": "BODY",\n'
        '            "engine": "rogue", "cached": False}\n'
    )
    tree = ast.parse((src / "rogue.py").read_text())
    found = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)
             and "result" in [k.value for k in n.keys if isinstance(k, ast.Constant)]]
    assert found, "the fixture itself must contain a construction site"
    # and it is not wrapped — which is precisely what the real census reports as a failure
    assert "attach_confidence" not in (src / "rogue.py").read_text()


def test_confidence_helper_semantics():
    from codeintel.provider import attach_confidence

    answered = {"ok": True, "op": "x", "target": "y", "result": "BODY",
                "engine": "e", "cached": False}
    assert attach_confidence(answered)["confidence"] == "complete"
    partial = attach_confidence(answered, [{"section": "s", "kind": "k", "detail": "d"}])
    assert partial["confidence"] == "partial" and partial["gaps"]
    # A null result keeps `reason` as its whole story; stamping it would imply a body exists.
    assert "confidence" not in attach_confidence({**answered, "result": None})


def test_a_fanned_out_answer_reports_the_engine_that_could_not_be_asked():
    """`context` fans out by default. When one engine answers and another cannot be reached, the
    merged body simply omits the second — which reads as "it had nothing to add", not "it was never
    asked". The merge used to hand-build a six-key envelope and discard both `confidence` and the
    constituents' `gaps`."""
    from codeintel.gateway import Gateway

    gw = Gateway()
    merged = gw._merge(
        {
            "lsp": {"ok": True, "op": "context", "target": "x", "result": "## found it",
                    "engine": "lsp", "cached": False, "confidence": "complete"},
            "graph": {"ok": True, "op": "context", "target": "x", "result": None,
                      "engine": "graph", "cached": False, "reason": "engine-unavailable"},
        },
        "context", "x",
    )
    assert merged["result"] is not None
    assert merged["confidence"] == "partial", "a half-answered fan-out is not complete"
    kinds = [g.get("kind") for g in merged["gaps"]]
    assert "engine-unavailable" in kinds, kinds


def test_a_constituent_gap_survives_the_merge():
    from codeintel.gateway import Gateway

    gw = Gateway()
    merged = gw._merge(
        {
            "lsp": {"ok": True, "op": "context", "target": "x", "result": "## partial answer",
                    "engine": "lsp", "cached": False, "confidence": "partial",
                    "gaps": [{"section": "references", "kind": "timeout", "detail": "still loading"}]},
        },
        "context", "x",
    )
    assert merged["confidence"] == "partial"
    assert any(g.get("kind") == "timeout" for g in merged["gaps"])


def test_no_mcp_handler_bypasses_redaction():
    """`redact` was placed on one seam and three sibling handlers reached callers around it —
    `code.doctor` was measured emitting nine absolute home paths on a single call. Enumerate the
    exported handlers rather than naming the one that was caught."""
    from codeintel import server

    handlers = [n for n in dir(server) if n.startswith("code_") and n.endswith("_handler")]
    assert len(handlers) >= 4, handlers
    for name in handlers:
        src = inspect.getsource(getattr(server, name))
        assert "redact" in src or "gw.query" in src, (
            f"{name} returns to a caller without passing through redact()"
        )


def test_hotspots_ranks_across_languages():
    """The gate on `hotspots` being reinstated.

    It was withdrawn because both evaluated repositories ranked 100% `.tsx` — every Python and
    backend-TypeScript function was absent, which reads exactly like a ranking that considered them
    and found them simpler. Two request bugs caused it: only `Function` nodes were requested (a
    class method is a `Method` node) and the candidate set was capped at 200 rows returned in NAME
    order, so the client-side sort ranked an alphabetical slice. This pins both.
    """
    from codeintel.graph_backend import BackendClient
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
    gp._backend = BackendClient.__new__(BackendClient)
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


def test_withdrawn_ops_are_still_measured_by_the_corpus_oracle():
    """Withdrawal must not disarm the only harness that could earn the op back.

    `deadcode` was withdrawn from the default surface pending a precision measurement — and the
    nightly corpus job, which runs the `git grep` ground-truth oracle for exactly that op, does not
    set the opt-in flag. So the op became unmeasurable at the same moment it was made conditional on
    measurement. That is a deadlock, not a plan."""
    wf = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "corpus.yml"
    if not wf.exists():
        pytest.skip("no corpus workflow in this checkout")
    # What the job EXPORTS, not what it mentions: a comment explaining why a flag was removed names
    # the flag, and must not read as the flag still being set.
    body = "\n".join(line for line in wf.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    from codeintel.providers.graph import _WITHDRAWN_OPS, GraphProvider

    # Only an op that still HAS an implementation can be deadlocked this way. `deadcode` was, and
    # the deadlock is how it stayed unmeasured for two releases; it has since been measured and
    # retired, so there is nothing left to enable — and a corpus job still exporting the flag would
    # imply otherwise.
    runnable_but_withdrawn = [op for op in _WITHDRAWN_OPS
                              if hasattr(GraphProvider, f"_op_{op}")]
    if runnable_but_withdrawn:
        assert "CODEINTEL_ENABLE_UNVERIFIED_OPS" in body, (
            f"ops {sorted(runnable_but_withdrawn)} are withdrawn pending measurement, but the "
            "corpus job does not enable them, so they are never measured"
        )
    else:
        assert "CODEINTEL_ENABLE_UNVERIFIED_OPS" not in body, (
            "the corpus job exports an opt-in flag that no longer enables any op"
        )


# --------------------------------------------------------------------------- corpus split (B5)

def test_prose_is_classified_without_swallowing_code():
    """Conservative by design: an unknown extension is CODE. The bug being fixed is prose crowding
    code out, and an over-eager rule would simply invert it."""
    from codeintel.source_kind import is_prose

    for p in ("docs/x.md", "README.md", "tests/snapshots/t.claude.md", "a/.archive/s.json"):
        assert is_prose(p), p
    for p in ("src/a.py", "src/ui/W.tsx", "main.go", "lib/thing.unknownext"):
        assert not is_prose(p), p


def test_code_hits_outrank_prose_and_the_mix_is_reported():
    """Retrieval, not just rendering. Partitioning the final ten was not enough — on a doc-heavy
    repository all ten candidates were prose, so re-ordering had nothing to promote."""
    from codeintel.source_kind import partition_by_corpus

    hits = [{"path": f"docs/d{i}.md"} for i in range(8)] + [{"path": "src/impl.py"}]
    code, prose = partition_by_corpus(hits)
    assert [m["path"] for m in code] == ["src/impl.py"]
    assert len(prose) == 8


def test_an_all_prose_answer_says_no_code_matched():
    """A caller who asked 'where is X done' and got only prose must be told no source matched —
    otherwise an empty code corpus reads as 'the implementation does not exist'."""
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "codeintel" / "providers" / "semantic.py"
    body = src.read_text()
    assert "no-code-matches" in body
    assert "NOT evidence" in body


def test_the_home_path_is_redacted_in_its_flattened_form_too():
    """The graph backend's project id for a path-slug registration IS the flattened absolute path —
    separators turned into dashes. Redaction matched only the slash form, so it passed straight
    through, and on a Go repository (flat qualified names, so the project prefix was not stripped
    either) every result row carried the username mid-identifier. Two independent defences both
    missed the same leak, which is the argument for keeping both."""
    import os

    from codeintel.redact import contains_home_path, redact_text

    home = os.path.expanduser("~")
    flat = home.strip("/").replace("/", "-")
    leaky = f"private-tmp-{flat}-scratch-cobra.Execute"
    assert contains_home_path(leaky), "the detector cannot see the form that leaked"
    assert not contains_home_path(redact_text(leaky))


def test_a_flat_namespace_qualified_name_still_gets_its_project_prefix_stripped():
    """Go, Java, C# and Ruby produce `project.Symbol` — ONE segment after the project id. The
    stripper required a dotted remainder, so on those languages it stripped nothing at all. Two
    languages could not surface this; both Python and TypeScript happen to produce dotted
    remainders."""
    from codeintel.providers.graph import _strip_project_prefix

    assert _strip_project_prefix("my-repo.Execute") == "Execute"
    assert _strip_project_prefix("Users-alice-work-cobra.ExecuteC") == "ExecuteC"
    # ...without eating the kebab-case FILENAMES the dotted-remainder rule was added to protect.
    assert _strip_project_prefix("use-toast.ts") == "use-toast.ts"
    assert _strip_project_prefix("1731900000000-CreateInitialTables.ts") == (
        "1731900000000-CreateInitialTables.ts")


@pytest.mark.parametrize("home,expected_flat", [
    ("/Users/alice", "Users-alice"),
    ("/home/bob", "home-bob"),
    ("C:\\Users\\alice", "C-Users-alice"),      # Windows: backslashes AND a drive colon
])
def test_the_flattened_home_is_detected_on_every_platform(home, expected_flat, monkeypatch):
    """The flattened-home redaction was written POSIX-only — `strip("/").replace("/", "-")` — so on
    Windows it returned the home path unchanged and the detector reported False for a leak that
    plainly contained the username. CI is ubuntu-only, so nothing would have caught it.

    The macOS/Linux form of this same defect was found by evaluating a third LANGUAGE; this one is
    the same defect one PLATFORM over. Parametrised rather than fixed to the host, so the platform
    this happens to run on stops deciding what gets verified."""
    import codeintel.redact as r

    monkeypatch.setattr(r, "_home", lambda: home)
    assert r._flattened_home() == expected_flat
    leak = f"private-tmp-{expected_flat}-proj.Execute"
    assert r.contains_home_path(leak), "a leak the detector cannot see is a leak that ships"
    assert not r.contains_home_path(r.redact_text(leak))
