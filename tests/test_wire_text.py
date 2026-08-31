"""The 0.10.x text dialect, against replies captured verbatim from a real backend.

Same discipline as `test_graph_real.py`, and for the same reason that file exists: the old suite
mocked `_run` with a shape the backend never emits, so every op silently discarded real rows while
the tests stayed green. Fixtures here are byte-for-byte `codebase-memory-mcp 0.10.8` output — piped
stdin, stdout only — so a format change breaks a test instead of an answer.

The assertions are about the ANSWER, not the parse. A translator that returns a well-formed dict
full of wrong rows is worse than the honest "backend-incompatible" refusal it replaced, because the
refusal was the only thing that made the 0.9→0.10 break diagnosable rather than indistinguishable
from an unindexed repository.
"""
from __future__ import annotations

import pathlib

import pytest

from codeintel.wire_text import is_text_dialect, parse, split_row

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "wire_0_10"


def fx(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the row tokenizer
# --------------------------------------------------------------------------- #

def test_bare_values_split_on_whitespace():
    assert split_row("runAlerts src/app/alert.ts CALLS") == [
        "runAlerts", "src/app/alert.ts", "CALLS"]


def test_a_quoted_value_keeps_its_spaces():
    """The backend quotes exactly when it must, so a Section name — a markdown heading — is one
    value and not five. Splitting naively on whitespace shifted every later column by four."""
    assert split_row('"Prompts — all six are done" .prompts/README.md') == [
        "Prompts — all six are done", ".prompts/README.md"]


def test_escaped_quotes_inside_a_quoted_value_survive():
    """`labels(a)` arrives as a JSON array inside a quoted cell: `"[\\"Function\\"]"`."""
    assert split_row('bar "[\\"Function\\"]" CALLS') == ["bar", '["Function"]', "CALLS"]


def test_an_apostrophe_does_not_open_a_quote():
    """Why this is not `shlex`: POSIX mode treats `'` as an opener and would swallow the rest of
    the line — and prose cells (docstrings, headings) contain apostrophes constantly."""
    assert split_row("don't stop") == ["don't", "stop"]


def test_the_null_marker_becomes_none_in_a_row():
    rows = parse("query_graph", 'rows: 1  (cols: a b)\n  x -\ntotal: 1\n')["rows"]
    assert rows == [["x", None]]


# --------------------------------------------------------------------------- #
# every method codeintel calls
# --------------------------------------------------------------------------- #

def test_query_graph_rebuilds_columns_and_rows():
    out = parse("query_graph", fx("query_graph"))
    # Cypher column names carry their own parens. A regex that stopped at the first `)` produced
    # `labels(a` and dropped every column after it — silently, since the rows still parsed.
    assert out["columns"] == ["a.name", "a.qualified_name", "a.file_path",
                              "labels(a)", "type(c)", "c.confidence"]
    assert len(out["rows"]) == 2
    first = dict(zip(out["columns"], out["rows"][0], strict=True))
    assert first["a.name"] == "runAlerts"
    assert first["a.file_path"] == "src/app/alert.ts"
    assert first["c.confidence"] == "0.95"      # the column whose loss started all of this


def test_an_empty_result_is_an_answer_not_a_failure():
    """`rows: 0` is "nothing matched", which is a fact about the repository. Returning None would
    turn it back into "backend unreadable" — the exact confusion this module exists to end."""
    out = parse("query_graph", "rows: 0  (cols: n.name)\ntotal: 0\n")
    assert out is not None
    assert out["rows"] == []
    assert out["columns"] == ["n.name"]


def test_search_graph_flattens_groups_into_qualified_names():
    """Rows sit under a group line that owns the qualified-name prefix and the file path; the
    header states the rule (`qn = group prefix + "." + name`). Losing it leaves every hotspot
    labelled by its bare name with no file."""
    out = parse("search_graph", fx("search_graph"))
    first = out["results"][0]
    assert first["name"] == "CONFIG"
    assert first["qualified_name"].endswith("src.bin.guard.CONFIG")
    assert first["file_path"] == "src/bin/guard.js"
    assert first["in_degree"] == 1 and first["out_degree"] == 1
    assert out["total"] == 259 and out["has_more"] is True


def test_search_graph_reads_the_metrics_that_fields_adds():
    """`complexity`/`cognitive`/`is_test` are core columns in 0.9.x and opt-in in 0.10.x. Without
    them `hotspots` ranks on a column that is uniformly zero — a list sorted by nothing."""
    out = parse("search_graph", fx("search_graph_fields"))
    assert {"complexity", "cognitive", "is_test"} <= set(out["results"][0])
    assert any(r["is_test"] for r in out["results"]), "the spec-file row should be flagged"


def test_search_code_splits_spans_and_match_lines():
    out = parse("search_code", fx("search_code"))
    hit = next(r for r in out["results"] if r["node"] == "runAlerts")
    assert hit["file"] == "src/app/alert.ts"
    assert hit["start_line"] == 55 and hit["end_line"] == 126
    # `59;85` unquoted and `"75"` quoted are the same column — the backend quotes inconsistently.
    assert hit["match_lines"] == [59, 85]
    assert all(isinstance(n, int) for r in out["results"] for n in r["match_lines"])


def test_trace_path_keeps_hops_and_risk_labels():
    out = parse("trace_path", fx("trace_path"))
    assert out["function"] == "runAlerts"
    assert out["callees"], out
    hop1 = [c for c in out["callees"] if c["hop"] == 1]
    assert hop1 and all(c["risk"] for c in hop1)
    assert any(c["name"] == "evaluateLatch" for c in out["callees"])


def test_get_architecture_recovers_counts_and_breakdowns():
    out = parse("get_architecture", fx("get_architecture"))
    assert out["total_nodes"] == 1577 and out["total_edges"] == 3440
    labels = {d["label"]: d["count"] for d in out["node_labels"]}
    assert labels["Function"] == 259
    assert out["languages"], "the language census drives the overview's language block"
    assert all("file_count" in d for d in out["languages"])


def test_detect_changes_reads_the_file_list_and_the_impacted_walk():
    out = parse("detect_changes", fx("detect_changes_rich"))
    assert "src/domain/budget.ts" in out["changed_files"]
    assert out["changed_count"] == len(out["changed_files"])
    names = {s["name"] for s in out["impacted_symbols"]}
    # 0.10.x's `impacted` is already transitive — `runAlerts` calls into the changed file from
    # another one, and `runRefresh` reaches it at a second hop.
    assert {"runAlerts", "runRefresh"} <= names, out["impacted_symbols"]
    ra = next(s for s in out["impacted_symbols"] if s["name"] == "runAlerts")
    assert ra["file_path"] == "src/app/alert.ts"
    assert ra["qualified_name"].endswith("src.app.alert.runAlerts")
    assert ra["hop"] == 1


def test_a_clean_tree_is_reported_as_clean_not_as_unreadable():
    out = parse("detect_changes", fx("detect_changes"))
    assert out is not None
    assert out["impacted_symbols"] == []


# --------------------------------------------------------------------------- #
# refusing, which is the half that keeps the safe-null contract honest
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "",
    "<html><body>502 Bad Gateway</body></html>",
    "Usage: codebase-memory-mcp cli [--progress] [--json] <tool_name>",
    "panic: runtime error: invalid memory address",
    "function: x\ndirection: both\n",
])
def test_a_reply_that_is_not_a_usable_answer_is_refused(text):
    """`parse` is where the decision lives, and it is the one that must hold.

    Note the last two cases: `panic: …` and a hop-less `trace_path` reply BOTH satisfy
    `is_text_dialect`, because a Go panic line is `key: value` and nothing cheap tells it apart
    from `function: runAlerts`. That pre-filter is deliberately weak; what makes the refusal
    reliable is that every per-method adapter requires the keys its answer is built from and
    returns None when they are absent. Testing the pre-filter instead of the decision would pass
    while the decision rotted."""
    assert parse("query_graph", text) is None
    assert parse("get_architecture", text) is None
    assert parse("detect_changes", text) is None


@pytest.mark.parametrize("text", ["", "<html><body>502</body></html>", "   \n\n"])
def test_the_cheap_prefilter_rejects_what_it_can(text):
    """It catches the common shapes — an empty stream, an HTML error page — before any adapter
    runs. It is an optimisation, not the guarantee; see the test above for the guarantee."""
    assert is_text_dialect(text) is False


def test_an_unknown_method_is_refused_rather_than_guessed():
    assert parse("some_future_tool", "rows: 1  (cols: a)\n  x\ntotal: 1\n") is None


def test_a_method_answering_in_the_wrong_shape_is_refused():
    """Right dialect, wrong content — a `trace_path` reply that carries no hop sections at all.
    Returning `{"callees": [], "callers": []}` would assert the symbol reaches nothing."""
    assert parse("trace_path", "function: x\ndirection: both\n") is None


def test_the_parser_never_raises_on_malformed_input():
    """Everything above this line runs inside a never-raise transport. A parser that threw would
    surface as a generic swallowed error, losing the reason the caller needs."""
    for junk in ("rows: notanumber  (cols:)\n  \n", "rows: 1  (cols: a b\n  unterminated \"quote",
                 "\x00\xff binary", "key:\n  \n  \n"):
        parse("query_graph", junk)          # must not raise
        parse("detect_changes", junk)


def test_a_group_line_with_no_file_path_still_opens_its_rows():
    """`trace_path` groups its rows under a bare qualified name with NO parenthesised path — and so
    does any node outside a file (`builtins.*`). Requiring the path made the row reader treat that
    line as foreign and abandon the section, so a `trace_path` reply parsed its header, read ZERO
    rows, and `chain` reported a symbol with 17 hops as having none. Latent in the 0.10.x support
    until `--include-evidence` changed the column set to the grouped form."""
    text = ("function: f\ndirection: both\ncallees_total: 2\n"
            "callees: 2  (rows: name hop strategy confidence; qn = group prefix + \".\" + name)\n"
            "proj.faults.delay:\n"
            "  apply 1 lsp 0.88\n"
            "builtins:\n"
            "  str 1 heuristic 0.38\n")
    out = parse("trace_path", text)
    assert out is not None
    assert len(out["callees"]) == 2, out
    first = out["callees"][0]
    assert first["qualified_name"] == "proj.faults.delay.apply"
    assert first["strategy"] == "lsp" and first["confidence"] == "0.88"


def test_a_sibling_section_ends_the_previous_one():
    """With the group pattern loosened, a following section header must still terminate the rows
    above it rather than being swallowed as a group of theirs."""
    text = ("callees: 1  (cols: qn hop)\n  a.b 1\n"
            "callers: 1  (cols: qn hop)\n  c.d 2\n")
    out = parse("trace_path", text)
    assert [c["qualified_name"] for c in out["callees"]] == ["a.b"], out
    assert [c["qualified_name"] for c in out["callers"]] == ["c.d"], out
