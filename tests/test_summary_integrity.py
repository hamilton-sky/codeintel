"""A summary must agree with what it claims to summarize.

This project has discovered one law six times and encoded it zero times:

    A SIGNAL TRUE ABOUT ITS REFERENT CAN BE FALSE ABOUT THE QUESTION THE CALLER IS ASKING.

* `READY` is true of the serena PROCESS and false of answerability (`providers/lsp.py`).
* `Ok([])` is true of the CALL and false of the world (`outcome.py`).
* "48 callers" is true of the ROWS and false of the callers (`providers/graph.py`).
* "bound at module scope" is true of the LOCATION and false of the identity (`bench/oracle_py.py`).
* `[python]` is true of the CONFIG and false of every TypeScript answer (`lang_config.py`).
* a single count is true of the TOTAL and false of the kinds beneath it (`providers/graph.py`).

Every one was found by a person noticing. None was found by a test failing, and that — not any of
the six — is the defect this module exists to close. The fix for each instance was written at the
site where it was noticed, so the seventh instance will ship the same way unless something
mechanical objects.

WHAT A SUMMARY IS. Any emitted field or line whose truth is asserted about a REFERENT the emitter
did not interrogate. The substitution is always the same: the summary is computed from a PROXY
that is cheap and locally true, and read as a claim about a referent nobody asked.

    shape          proxy                        referent this module checks it against
    ───────────────────────────────────────────────────────────────────────────────────
    capability     exists / booted / on PATH    a real query returns content
    completeness   no gap was recorded          no gap-worthy condition is detectable
    aggregate      len(rows)                    the rows beneath the headline
    presence       a marker file / a row count  the operation that consumes it
    attribution    the caller's cwd / a sort key the answering project / the actual order
    classification one analysis pass            an independent read of the same text

TWO LAYERS, AND THE FIRST IS THE POINT.

1. `_summary_sites()` is a CENSUS. It walks `src/` and `bench/` and finds every emission site
   belonging to a mechanically decidable family, keyed by the enclosing qualname rather than by
   line number so the key survives both a line move and a module split. Every site it finds must
   be named in `_VERIFIED_BY`, or `test_every_summary_site_has_a_verifier` fails and says so. That
   is what makes a NEW summary — the seventh instance — fail a test on the commit that adds it,
   instead of waiting to be noticed.

2. The verifiers beneath it check each family against its referent for real.

WHAT THE CENSUS CANNOT SEE, stated plainly because a guard that overstates its domain is the
defect this file is about. It finds three families by syntax: claim-dicts, count-bearing markdown
headings, and tally phrases. A summary emitted in some fourth shape — prose with no unit noun, a
number assembled across two statements — is invisible to it and is covered only by whatever
explicit entry someone wrote. `_UNCENSUSED` lists the ones known to be in that position today.

PRIOR ART. Five places in the tree already do this correctly, and they are the specification for
what "checked" means here: `verify.py::verify_stdio_call` asks a real question and counts the
content; `graph.py::_probe_wire_format` refuses to call a backend healthy on `list_projects`
alone; `graph.py::_op_overview` verifies the Route aggregate against a query before repeating it;
`c4_check.py::check_layers` refuses a pass VERDICT when no rule was evaluated; and
`bench/score.py::_verify_target_sources` checks each target's definition exists before scoring it.

The fourth of those is prior art for its verdict and not for its coverage line, which is the first
thing this file found: the same function returns `{assigned: 0, unassigned: 0, total: 6}` on that
path and `render_report` prints it as a measurement. See
`test_c4_coverage_partitions_its_population_even_when_nothing_was_assessed`, which is a strict
xfail rather than a deleted test. That a module already reasoning carefully about one half of its
own output shipped the other half unchecked is the argument for a census rather than for more
care.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "codeintel"
_BENCH = _REPO / "bench"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The census
# ══════════════════════════════════════════════════════════════════════════════════════════════

# Dict keys that make a dict a REPORT about something else rather than a payload. `installed` is
# deliberately absent as a sole trigger — it is a fact about the filesystem and nothing else, and
# on its own it is not a claim about an answer. It rides along on the probe dicts, which are
# caught by `runnable`/`repo_indexed`.
_CLAIM_KEYS = frozenset({
    "runnable",         # capability: will it answer?
    "repo_indexed",     # presence: is THIS repo answerable?
    "healthy",          # capability, aggregated
    "ready",            # capability, aggregated
    "confidence",       # completeness
    "coverage",         # aggregate over a population
    "source_readable",  # presence, sampled
    "chunks",           # presence, as a count
    "verified",         # capability, after a real check
    "evidence_class",   # classification: what this answer can be USED for
})

# A markdown heading is a summary when it carries a COUNT: a parenthesised number or
# interpolation, an interpolated quantity with a unit, or an arrow between two quantities.
_HEADING = re.compile(r"^\s*#{1,4} ")
_HEADING_COUNT = re.compile(
    r"\(\s*(\{#\}|\d+)[^)]*\)"
    r"|\{#\}\s*(files?|symbols?|elements?|relations?|nodes?|edges?)\b"
    r"|→"
)

# A tally phrase is a count followed by a unit noun. This is the shape `bench/score.py` and the
# CLI summaries use, neither of which is a markdown heading.
#
# `match` is deliberately NOT a unit noun here. "`{}` matches no declared layer" is an element
# name followed by a verb, not a count followed by a thing counted, and admitting it made the
# detector fire on two c4 lines that state no quantity at all. A detector that cries wolf gets
# narrowed by whoever it inconveniences; narrowing it here, once, on a known false positive, is
# cheaper than having it narrowed later by someone who just wants their build green. The one real
# "matches" count — `## Pattern matches for "x" (N)` — is a heading and `_HEADING_COUNT` has it.
_TALLY = re.compile(
    r"\{#\}\s*(?:/\s*\{#\}\s*)?\s*(?:new\s+)?"
    r"(call|caller|callee|row|chunk|file|symbol|element|relation|reference|node|edge|tool|hop|"
    r"violation|index file|engine|project|band)"
    r"(?:\(s\)|s|\(es\)|es)?\b"
)


def _site_key(qualname: str, module_stem: str) -> str:
    """The registry key for a site, chosen to survive the one refactor that is already scheduled.

    A DOTTED qualname (`GraphProvider.probe`) names a class member, and a module split that moves a
    whole class carries its members across intact, so the key survives it.

    It does NOT survive a class being split, and splitting `providers/graph.py` was exactly that:
    the ops moved to a `GraphOps` mixin and the renderers to `AnswerRendering`, so nine keys were
    rekeyed from `GraphProvider.*` in one commit. An earlier version of this docstring promised
    that split would "not churn a single registry entry", which was wrong. What the keys did buy
    is the thing worth having: the rename was a clean 1:1 — nine out, nine in, none lost and none
    invented — and `test_the_registry_has_no_entries_for_summaries_that_no_longer_exist` named
    every one of them rather than letting a moved summary quietly go unchecked.

    A BARE qualname is a top-level function, and those are not unique: `run` is a top-level
    function in four different modules here (`commands/c4.py`, `commands/graph.py`,
    `commands/index.py`, `bench/score.py`), each emitting an unrelated summary. Collapsing four
    summaries into one registry entry would mean three of them are recorded as checked by a test
    that never looks at them — which is this file's own subject matter, so it is worth not doing.
    """
    return qualname if "." in qualname else f"{module_stem}.{qualname}"


class _Site:
    """One emission site. `key` is (shape, qualname) — no line number, so it survives a refactor."""

    __slots__ = ("detail", "lineno", "path", "qualname", "shape")

    def __init__(self, shape: str, qualname: str, path: str, lineno: int, detail: str) -> None:
        self.shape, self.qualname = shape, qualname
        self.path, self.lineno, self.detail = path, lineno, detail

    @property
    def key(self) -> tuple[str, str]:
        return (self.shape, self.qualname)

    def __repr__(self) -> str:
        return f"{self.shape}:{self.qualname} ({self.path}:{self.lineno})  {self.detail!r}"


def _qualnames(tree: ast.AST) -> dict[int, str]:
    """line number -> INNERMOST enclosing qualified name.

    Outer scopes are written first and inner ones overwrite, so a dict literal inside
    `GraphProvider.probe` keys as `GraphProvider.probe` and not as `GraphProvider`. The distinction
    is the whole value of the key: `GraphProvider` alone would collapse six unrelated summaries
    into one registry entry, and splitting the class across modules would not change it.
    """
    out: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qn = f"{prefix}.{child.name}" if prefix else child.name
                for sub in ast.walk(child):
                    line = getattr(sub, "lineno", None)
                    if line is not None:
                        out[line] = qn
                walk(child, qn)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


# Names that denote a quantity. Used to tell `f"{n} rows"` (a tally) from `f"{engine} engine"`
# (a name beside a noun that happens to be in the unit list) — which is the same distinction the
# law is about, one level down: a placeholder is only a count when the expression behind it counts
# something.
_COUNTISH = re.compile(
    r"(^|_)(n|num|count|total|kept|size|len|removed|found|hits|rows|files|chunks|nodes|edges|"
    r"elements|relations|symbols|callers|callees|refs|tools|direct|others|sampled|unreadable|"
    r"assigned|unassigned|ready|degree|depth|hops?|cap|max|min)(_|$)",
    re.IGNORECASE,
)


def _is_count_expr(node: ast.AST) -> bool:
    """Whether an interpolated expression denotes a QUANTITY rather than a name.

    `f"the {engine} engine contributed nothing"` and `f"{len(rows)} rows"` flatten to the same
    text, and only the second is a tally. Reading the expression is what separates them; without
    it the `engine`/`edge`/`project` unit nouns fire on every message that names one.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else "")
        return name in ("len", "sum", "max", "min", "count", "int", "abs", "round")
    if isinstance(node, ast.Name):
        return bool(_COUNTISH.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_COUNTISH.search(node.attr))
    if isinstance(node, ast.Subscript):
        return _is_count_expr(node.value)
    if isinstance(node, (ast.BinOp, ast.UnaryOp)):
        return any(_is_count_expr(c) for c in ast.iter_child_nodes(node))
    if isinstance(node, ast.IfExp):
        return _is_count_expr(node.body) or _is_count_expr(node.orelse)
    if isinstance(node, ast.FormattedValue):
        return _is_count_expr(node.value)
    return False


def _literal_shape(node: ast.AST) -> str | None:
    """The literal text of a string or f-string, with interpolations rendered as placeholders.

    A quantity becomes `{#}` and anything else `{}`, so the detectors can require a count where
    they mean a count. Interpolations are flattened rather than dropped so that
    `f"## Callers of {t} ({n})"` keeps its parentheses around a placeholder and is still
    recognised as count-bearing.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("{#}" if _is_count_expr(value) else "{}")
        return "".join(parts)
    return None


def _summary_sites() -> list[_Site]:
    """Every summary emission site in a mechanically decidable family.

    Three detectors, each narrow on purpose. A detector that fires on ordinary strings would be
    silenced by whoever it inconvenienced, and a silenced guard is worse than none: it converts
    "we did not check" into green.
    """
    sites: list[_Site] = []
    for base in (_SRC, _BENCH):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            qualnames = _qualnames(tree)
            rel = str(path.relative_to(_REPO))
            for node in ast.walk(tree):
                lineno = getattr(node, "lineno", 0)
                qualname = _site_key(
                    qualnames.get(lineno) or "<module>", path.stem)

                # D1 — a dict literal that reports on something else.
                if isinstance(node, ast.Dict):
                    keys = {
                        k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    claimed = keys & _CLAIM_KEYS
                    if claimed:
                        sites.append(
                            _Site("claim", qualname, rel, lineno, ", ".join(sorted(claimed)))
                        )

                # D2 / D3 — a rendered line that states a quantity.
                text = _literal_shape(node)
                if not text:
                    continue
                for line in text.splitlines():
                    if _HEADING.match(line) and _HEADING_COUNT.search(line):
                        sites.append(_Site("count", qualname, rel, lineno, line.strip()[:72]))
                        break
                    if _TALLY.search(line):
                        sites.append(_Site("tally", qualname, rel, lineno, line.strip()[:72]))
                        break
    return sites


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The registry — every census key, and the referent its verifier checks it against
# ══════════════════════════════════════════════════════════════════════════════════════════════

# Maps a census key to the REFERENT a test below asserts it against. The value is prose because it
# is read by whoever hits the failure, and it has to tell them what "checked" would mean for the
# summary they just added — naming a test function would tell them only where to paste.
_VERIFIED_BY: dict[tuple[str, str], str] = {
    # ── capability: "it will answer" ⟹ a real query on that repo returns content ──────────────
    # Two tiers. The shallow tier asserts the roll-up refuses a repository the engine cannot
    # answer about; the deep tier asserts `--deep` PUTS A REAL QUESTION and requires content, which
    # is what makes `runnable` mean "will answer" rather than "started".
    ("claim", "GraphProvider.probe"):
        "a booted backend that answers nothing is not runnable — "
        "test_a_backend_that_boots_and_answers_nothing_is_not_called_runnable, and under --deep "
        "test_a_graph_project_that_resolves_but_holds_nothing_is_not_deep_runnable",
    ("claim", "LspProvider.probe"):
        "a booted backend that answers nothing is not runnable — "
        "test_a_backend_that_boots_and_answers_nothing_is_not_called_runnable, and under --deep "
        "test_a_ready_language_server_that_answers_nothing_is_not_deep_runnable",
    ("claim", "SemanticProvider.probe"):
        "a booted backend that answers nothing is not runnable — "
        "test_a_backend_that_boots_and_answers_nothing_is_not_called_runnable, and under --deep "
        "test_semantic_chunks_without_vectors_are_not_a_working_index",
    ("claim", "semantic._not_indexed_probe"):
        "the not-indexed state claims no readiness — "
        "test_a_backend_that_boots_and_answers_nothing_is_not_called_runnable",
    ("claim", "doctor._probe_engine"):
        "the status cell follows the probe it was handed — "
        "test_doctor_status_follows_the_probe_it_was_given",
    ("claim", "doctor.run_doctor"):
        "ready/total/healthy count the engine rows in the same report — "
        "test_doctor_summary_sums_its_own_engine_rows",
    ("claim", "run_setup._empty_doctor"):
        "a report with no engines claims no readiness — "
        "test_doctor_summary_sums_its_own_engine_rows",
    ("claim", "doctor.collect_registrations"):
        "an executable file is not a server that answers — "
        "test_a_registration_runnable_flag_is_only_about_the_file",
    ("claim", "server._code_status_handler_inner"):
        "status relays doctor's tri-state without widening it — "
        "test_code_status_relays_the_same_tri_state_as_doctor",
    ("claim", "server._code_doctor_handler_inner"):
        "a refused report asserts no readiness — test_a_refused_doctor_report_claims_no_readiness",
    ("claim", "server.<module>"):
        "the status fallback fails closed — test_the_status_fallback_claims_nothing",

    # ── completeness: `confidence: complete` ⟹ no gap-worthy condition is detectable ──────────
    ("claim", "provider.attach_confidence"):
        "a body that discloses a limitation carries a gap — "
        "test_a_body_that_discloses_a_limitation_is_never_stamped_complete; and the answer's "
        "`evidence_class` is decided by the rows it has rather than by the op it came from — "
        "test_the_evidence_class_never_calls_a_partial_answer_proof, with the end-to-end half in "
        "test_the_evidence_class_follows_the_rows_and_not_only_the_op; and with no rows to read, "
        "only an lsp `symbol` is proof — "
        "test_an_answer_with_no_row_summary_is_proof_only_as_an_lsp_symbol_lookup",

    # ── structured: the answer as FIELDS ⟹ the rows the body printed ──────────────────────────
    # `rows` and `evidence` are the prose in a shape an agent branches on, so every way the two
    # could disagree is a summary reporting one thing and being read as another. The referent is
    # never the rows RETRIEVED — it is the rows the body PRINTED, which is where this one went
    # wrong first: `impact` renders callers and callees and recorded only the second half.
    ("claim", "AnswerRendering._settle_evidence"):
        "the three buckets partition the rows the body printed, `returned` is the `- ` lines in "
        "it, and `total` is None exactly when the backend's cap was hit — "
        "test_the_evidence_summary_agrees_with_the_rows_it_summarises, with the two caps kept "
        "apart by test_a_capped_answer_counts_the_rows_it_withheld_rather_than_forgetting_them "
        "and the safe/partial cross-check by "
        "test_the_envelope_never_calls_an_answer_safe_while_calling_it_partial",
    ("claim", "AnswerRendering._structured_row"):
        "each row's `verified` and `confidence` are the badge on the line it was rendered from — "
        "test_a_structured_row_never_disagrees_with_the_line_it_was_rendered_from, and the filter "
        "they exist for is checked against the prose by "
        "test_an_agent_can_filter_on_structured_fields_alone",

    # ── relay: a per-row confidence this tool did not establish ───────────────────────────────
    ("claim", "_trace_path.hops"):
        "an unstamped row stays unstamped — test_wire_text_never_manufactures_a_confidence",

    # ── presence: a count of what was written ⟹ what the writer reported ──────────────────────
    ("claim", "onboarding._bounded_index"):
        "the chunk count is the indexer's own return — "
        "test_setup_reports_the_chunk_count_the_indexer_returned",
    ("tally", "onboarding._bounded_index"):
        "the chunk count is the indexer's own return — "
        "test_setup_reports_the_chunk_count_the_indexer_returned",
    ("tally", "index.run"):
        "the chunk count is the indexer's own return — "
        "test_the_index_command_reports_the_count_the_indexer_returned",
    ("tally", "reset._reset_all"):
        "the removal count counts files actually gone — test_reset_counts_the_files_it_removed",
    ("tally", "LiveCounter.scan"):
        "the progress counter reports what it was fed — "
        "test_the_progress_counter_reports_what_it_counted",
    ("tally", "LiveCounter.embed"):
        "the progress counter reports what it was fed — "
        "test_the_progress_counter_reports_what_it_counted",

    # ── aggregate: a headline ⟹ the rows beneath it ───────────────────────────────────────────
    ("count", "AnswerRendering._render_edge_answer"):
        "the headline counts the rows rendered — "
        "test_every_rendered_headline_counts_the_rows_beneath_it",
    ("count", "AnswerRendering._render_scan"):
        "the headline counts the rows rendered — "
        "test_every_rendered_headline_counts_the_rows_beneath_it",
    ("count", "GraphOps._op_pattern"):
        "the headline counts the rows rendered — "
        "test_every_rendered_headline_counts_the_rows_beneath_it",
    ("count", "GraphOps._op_changed"):
        "each section heading counts its own section — "
        "test_every_rendered_headline_counts_the_rows_beneath_it",
    ("tally", "GraphOps._op_changed"):
        "the impact headline sums its own sections — "
        "test_the_changed_headline_sums_the_sections_beneath_it",
    ("count", "GraphOps._op_impact"):
        "a zero is rendered only when the lookup answered — "
        "test_impact_renders_a_zero_only_when_the_lookup_actually_answered",
    ("count", "AnswerRendering._empty_edge_answer"):
        "a zero is rendered only when rows were retrieved — "
        "test_impact_renders_a_zero_only_when_the_lookup_actually_answered",
    ("count", "LspProvider._op_symbol"):
        "the reference count counts the lines rendered — "
        "test_every_rendered_headline_counts_the_rows_beneath_it",
    ("count", "Gateway._cross_check_name_resolved"):
        "the cross-check count counts its own rows — "
        "test_the_cross_check_headline_counts_the_rows_it_rendered",
    ("tally", "Gateway._cross_check_name_resolved"):
        "the cross-check count counts its own rows — "
        "test_the_cross_check_headline_counts_the_rows_it_rendered",
    ("tally", "AnswerRendering._confidence_note"):
        "the note's N-of-M counts the rows it describes — "
        "test_the_confidence_note_counts_the_rows_it_describes",
    ("tally", "AnswerRendering._settle_name_matches"):
        "the two counts in the settle note are the rows in doubt and their distinct files — "
        "test_the_settle_note_counts_the_rows_it_sends_you_to_check",
    ("tally", "AnswerRendering._no_symbol_matched_the_hint"):
        "a no-match note states only what was asked — "
        "test_a_no_match_note_claims_nothing_about_the_symbol_itself",
    ("claim", "c4_check.check_layers"):
        "coverage sums to its own element total — test_c4_coverage_sums_to_its_own_total",
    ("tally", "c4.render_c4_dsl"):
        "the DSL comment counts the DSL beneath it — "
        "test_the_c4_dsl_comment_counts_what_the_dsl_contains",
    ("tally", "c4._print_layers"):
        "the band tally sums to the elements placed — "
        "test_the_c4_layer_tally_sums_to_the_elements_it_placed",
    ("tally", "c4.run"):
        "the written-file line counts what was written — "
        "test_the_c4_dsl_comment_counts_what_the_dsl_contains",
    ("tally", "graph.run"):
        "the export line counts the nodes and edges written — "
        "test_the_graph_export_line_counts_what_it_wrote",

    # ── attribution: a claim about WHICH tree, or about the order ─────────────────────────────
    ("tally", "mapper._stamp_line"):
        "the stamp repeats the index it actually read — "
        "test_the_map_stamp_reports_the_index_it_read",
    ("tally", "LspProvider._op_symbol"):
        "`N of M` counts the references the server returned, not the rows printed — "
        "test_a_capped_reference_list_is_disclosed_as_truncated, with the boundary in "
        "test_a_reference_list_at_the_cap_exactly_is_still_complete",
    ("tally", "LspProvider._unserved_note"):
        "the note counts the files it found unserved — "
        "test_the_unserved_language_note_counts_the_files_it_found",
    ("tally", "doctor.render_doctor_text"):
        "the rendered N-of-M is the summary it was handed — "
        "test_doctor_summary_sums_its_own_engine_rows",

    # ── bench: the arithmetic every published number rests on ─────────────────────────────────
    ("tally", "score.run"):
        "each printed truth count is the oracle's own set — "
        "test_bench_per_symbol_line_reports_the_truth_it_scored",
    ("tally", "verify.verify_stdio_server"):
        "the tool count counts the tools listed — test_verify_counts_the_tools_it_listed",
}

# Summaries in no mechanically decidable family. The census is blind to these by construction, so
# they are listed rather than discovered — and listing them is what keeps the census's own claim
# about its domain honest. Each still has a verifier below.
_UNCENSUSED: dict[str, str] = {
    "mapper._render / '## Ranked Symbols (by caller count)'":
        "a heading whose parenthesis names a SORT KEY rather than a count — "
        "test_the_ranked_heading_is_actually_ranked_by_caller_count",
    "AnswerRendering._evidence_headline / 'N resolved · N name-matched · N unstated'":
        "a bold tally whose units are bucket names, so no unit noun to match — "
        "test_the_evidence_headline_sums_to_the_rows_it_describes",
    "bench/oracle_py.py / per-site label":
        "a returned enum, not a rendered string — "
        "test_every_python_oracle_label_agrees_with_a_grep, and instance #6 as a rule rather "
        "than as its fixture: test_no_site_in_the_targets_own_defining_file_is_a_proven_negative",
    "bench/oracle_ts.py / per-site label":
        "a returned enum, not a rendered string — "
        "test_every_typescript_oracle_label_agrees_with_a_grep",
    "bench/score.py / _provenance header":
        "assembled across many statements, no single string to match — "
        "test_bench_provenance_names_the_engine_it_ran",
}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The category test, and the guards that keep it from certifying nothing
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_every_summary_site_has_a_verifier():
    """THE test in this file. A summary with no verifier is the seventh instance, pre-shipped.

    Every one of the six known instances was a summary nobody had paired with its referent. Each
    fix was written at the site where a person noticed, which is why there were six. This fails on
    the commit that ADDS an unpaired summary rather than on the evaluation that finds it.
    """
    sites = _summary_sites()
    unregistered: dict[tuple[str, str], list[_Site]] = {}
    for site in sites:
        if site.key not in _VERIFIED_BY:
            unregistered.setdefault(site.key, []).append(site)

    if unregistered:
        listing = "\n".join(
            f"    {shape}:{qualname}\n"
            + "\n".join(f"        {s.path}:{s.lineno}  {s.detail!r}" for s in group)
            for (shape, qualname), group in sorted(unregistered.items())
        )
        pytest.fail(
            f"{len(unregistered)} summary site(s) emit a claim that no test checks against what "
            f"it claims to summarize:\n\n{listing}\n\n"
            "This is the shape that has shipped six times in this repository. Before registering "
            "it in _VERIFIED_BY, answer the question the law asks: this signal is true about "
            "WHAT, and is that the thing the caller is asking about? Then write the verifier — "
            "the entry's value is the referent, and it has to name something a test actually "
            "interrogates, not a restatement of the claim."
        )


def test_the_census_finds_the_population_it_is_supposed_to_find():
    """A census that silently stops matching turns this whole file green and pointless.

    Floors rather than exact counts: the population is meant to grow. What it must never do is
    collapse — the detectors are regexes over source text, and a refactor that changes how a
    heading is assembled could make one stop firing without anything else going red.
    """
    sites = _summary_sites()
    by_shape: dict[str, int] = {}
    for site in sites:
        by_shape[site.shape] = by_shape.get(site.shape, 0) + 1

    assert by_shape.get("claim", 0) >= 40, f"claim detector has stopped matching: {by_shape}"
    assert by_shape.get("count", 0) >= 14, f"count detector has stopped matching: {by_shape}"
    assert by_shape.get("tally", 0) >= 5, f"tally detector has stopped matching: {by_shape}"

    # Both trees are in scope. bench/ has one summary class that understated the tool by 10-60
    # points, and it had no test module at all; src/ having sixty is what made that easy to miss.
    scanned = {site.path.split("/")[0] for site in sites}
    assert scanned >= {"src", "bench"}, f"the census is not reading both trees: {scanned}"


def test_the_registry_has_no_entries_for_summaries_that_no_longer_exist():
    """A stale entry is a verifier nobody runs, recorded as coverage.

    This is the mirror of the test above and it matters for the same reason: after #3 splits
    `graph.py`, an entry whose qualname no longer exists would keep asserting that something is
    checked when the thing itself is gone.
    """
    live = {site.key for site in _summary_sites()}
    stale = sorted(k for k in _VERIFIED_BY if k not in live)
    assert not stale, (
        f"these registry entries name summaries the census no longer finds: {stale}. "
        "Either the summary was removed (delete the entry and its verifier) or a detector stopped "
        "matching it (fix the detector — the summary is now unguarded)."
    )


def test_the_census_guard_can_actually_fail(tmp_path):
    """A guard that cannot fail converts "we did not check" into green.

    Prove this one bites by running the real detectors over a tree containing a fresh violation.
    """
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "class Thing:\n"
        "    def probe(self):\n"
        '        return {"installed": True, "runnable": True, "repo_indexed": True}\n'
        "\n"
        "    def render(self, rows):\n"
        '        return f"## Callers of x ({len(rows)})"\n'
    )
    tree = ast.parse(rogue.read_text())
    qualnames = _qualnames(tree)

    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 0)
        qualname = qualnames.get(lineno, "<module>")
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if keys & _CLAIM_KEYS:
                found.append(("claim", qualname))
        text = _literal_shape(node)
        if text and _HEADING.match(text) and _HEADING_COUNT.search(text):
            found.append(("count", qualname))

    assert ("claim", "Thing.probe") in found, found
    assert ("count", "Thing.render") in found, found
    assert not any(key in _VERIFIED_BY for key in found), (
        "the fixture's keys must be absent from the real registry, or this proves nothing"
    )


def test_every_registry_entry_names_a_verifier_that_exists():
    """The entry's value is the only thing telling whoever hits the failure what checking means.

    Naming a test that does not exist would make this file assert its own coverage — which is the
    subject of the file. So the names are resolved, not trusted: a verifier that is renamed or
    deleted takes its registry entry red with it.
    """
    # Verifiers live wherever they belong, not wherever this guard can see them: the deep-probe
    # ones sit with the doctor tests beside the shallow probes they strengthen, the settle-command
    # one with the confidence tests. Resolving across every test module keeps the guard's question
    # the right one — does the named verifier EXIST — instead of quietly also requiring it to live
    # in a file someone has to remember to list here.
    import ast

    defined = {name for name in globals() if name.startswith("test_")}
    for sibling in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        if sibling.name == pathlib.Path(__file__).name:
            continue
        tree = ast.parse(sibling.read_text())
        defined |= {n.name for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    for source, entries in (("_VERIFIED_BY", _VERIFIED_BY), ("_UNCENSUSED", _UNCENSUSED)):
        for key, referent in entries.items():
            assert len(referent) > 40, f"{source}[{key}]'s referent is too thin to act on: {referent!r}"
            named = re.findall(r"\btest_\w+", referent)
            assert named, f"{source}[{key}] names no verifier: {referent!r}"
            missing = [n for n in named if n not in defined]
            assert not missing, (
                f"{source}[{key}] names {missing}, which is not defined in this module. Either the "
                f"verifier was renamed (update the entry) or it is gone (this summary is now "
                f"unchecked and the entry is claiming otherwise).")


def test_every_verifier_in_this_module_is_reachable_from_the_registry():
    """The mirror. A verifier nothing points at is a test nobody will know to update when the
    summary it checks moves — and the registry is what turns this file from a pile of tests into
    a statement about coverage."""
    housekeeping = {
        # The census's own guards, which check this file rather than a summary.
        "test_every_summary_site_has_a_verifier",
        "test_the_census_finds_the_population_it_is_supposed_to_find",
        "test_the_registry_has_no_entries_for_summaries_that_no_longer_exist",
        "test_the_census_guard_can_actually_fail",
        "test_every_registry_entry_names_a_verifier_that_exists",
        "test_every_verifier_in_this_module_is_reachable_from_the_registry",
        # Negative controls and positive halves — each paired with a verifier above.
        "test_the_headline_check_can_actually_fail",
        "test_the_grep_cross_check_can_actually_fail",
        "test_a_clean_answer_is_still_allowed_to_be_complete",
        "test_the_lsp_capability_claim_is_scoped_to_the_repo_not_the_process",
        "test_every_provider_that_probes_is_covered_by_the_capability_sweep",
        "test_the_python_oracle_abstains_where_it_says_it_abstains",
        "test_the_scorer_counts_coverage_over_the_population_it_reports",
        "test_c4_coverage_partitions_its_population_even_when_nothing_was_assessed",
    }
    referenced = set()
    for entries in (_VERIFIED_BY, _UNCENSUSED):
        for referent in entries.values():
            referenced.update(re.findall(r"\btest_\w+", referent))

    orphans = sorted(
        name for name in globals()
        if name.startswith("test_") and name not in referenced and name not in housekeeping
    )
    assert not orphans, (
        f"{orphans} check something and no registry entry points at them. Either add the entry "
        f"(so the summary is recorded as covered) or list them as housekeeping above.")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: AGGREGATE — a headline counts the rows beneath it
# ══════════════════════════════════════════════════════════════════════════════════════════════

# `## Callers of x (7)` / `### Changed files (3)`. Anchored at end of line so the trailing count
# wins over an incidental number earlier in the heading — `up to 2 hop(s) (5)` claims 5, not 2.
_HEAD_TOTAL = re.compile(r"^(#{2,4})\s+(.*?)\s*\((\d+)\)\s*$")
# `## Callers of x (2 direct, 43 other reference(s))` — the split heading `#34` introduced.
_HEAD_SPLIT = re.compile(
    r"^(#{2,4})\s+(.*?)\s*\((\d+)\s+direct,\s+(\d+)\s+other\s+reference\(s\)\)\s*$")
# `… (+12 more)` / `… (+3 more symbol(s) with this name, not shown)` — rows the body says it did
# not render. A headline that counts them is still honest; one that counts nothing is not.
_MORE = re.compile(r"\(\+(\d+)\s+more")


def _sections(body: str) -> list[tuple[str, int, int, list[str]]]:
    """(heading, level, claimed_total, row_lines) for every count-bearing heading in a body.

    A section runs until the next heading of the same or higher level, so `## Impact of x` owning
    two `##` children does not swallow their rows. A row is a `- ` bullet or a `| ` table row —
    the two shapes every renderer in this tree emits — and notes, blank lines and the truncation
    disclosure are not rows, which is the distinction the check depends on.
    """
    lines = body.splitlines()
    heads: list[tuple[int, str, int, int]] = []          # (index, heading, level, claimed)
    for i, line in enumerate(lines):
        split = _HEAD_SPLIT.match(line)
        if split:
            heads.append((i, split.group(2), len(split.group(1)),
                          int(split.group(3)) + int(split.group(4))))
            continue
        total = _HEAD_TOTAL.match(line)
        if total:
            heads.append((i, total.group(2), len(total.group(1)), int(total.group(3))))

    out = []
    for i, heading, level, claimed in heads:
        end = len(lines)
        for j in range(i + 1, len(lines)):
            other = re.match(r"^(#{1,4})\s", lines[j])
            if other and len(other.group(1)) <= level:
                end = j
                break
        rows = [ln for ln in lines[i + 1:end]
                if ln.startswith("- ") or (ln.startswith("| ") and not ln.startswith("|--"))]
        # Rows the body admits it left out still belong to the claim.
        rows += ["<disclosed>"] * sum(int(m.group(1)) for m in _MORE.finditer("\n".join(lines[i + 1:end])))
        out.append((heading, level, claimed, rows))
    return out


def _headline_disagreements(body: str) -> list[str]:
    """Every heading whose count does not match the rows beneath it."""
    bad = []
    for heading, _level, claimed, rows in _sections(body):
        if claimed != len(rows):
            bad.append(f"{heading!r} claims {claimed}, {len(rows)} row(s) beneath it")
    return bad


def _graph_provider():
    """A GraphProvider with the subprocess seam absent — enough to drive the renderers."""
    from codeintel.providers.graph import BackendClient, GraphProvider

    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)    # type: ignore[attr-defined]
    gp.available = True                                   # type: ignore[attr-defined]
    gp._pending_gaps = []                                 # type: ignore[attr-defined]
    gp._answered_root = None                              # type: ignore[attr-defined]
    return gp


def _edge_rows(n: int, kind: str = "CALLS", bucket: str = "resolved", start: int = 0):
    return [{
        "b.name": f"fn{i}", "b.qualified_name": f"pkg.mod.fn{i}", "b.file_path": f"src/m{i}.py",
        "a.name": f"fn{i}", "a.qualified_name": f"pkg.mod.fn{i}", "a.file_path": f"src/m{i}.py",
        "type(c)": kind, "_bucket": bucket,
    } for i in range(start, start + n)]


@pytest.mark.parametrize(
    "case",
    ["callers-one-group", "callers-split-kinds", "callers-two-groups", "scan", "lsp-references"],
)
def test_every_rendered_headline_counts_the_rows_beneath_it(case):
    """`(48)` above fifty rows is the finding this whole file generalises.

    The headline is the first line an agent reads and the only one a careless one reads. It is
    checked here against the thing it claims to count — the rows actually rendered, plus the rows
    the body explicitly says it did not render, which is a disclosure rather than a discrepancy.
    """
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget

    gp = _graph_provider()
    keys = ("b.name", "b.qualified_name", "b.file_path")

    if case == "callers-one-group":
        groups = [_EdgeGroup("handle", "pkg.mod.handle", "src/a.py", _edge_rows(7))]
        body = gp._render_edge_answer(
            "callers", "caller", "handle", _SymbolTarget("handle"), groups, keys, False)
    elif case == "callers-split-kinds":
        rows = _edge_rows(4) + _edge_rows(3, kind="CALL_REFERENCE", start=4)
        groups = [_EdgeGroup("handle", "pkg.mod.handle", "src/a.py", rows)]
        body = gp._render_edge_answer(
            "callers", "caller", "handle", _SymbolTarget("handle"), groups, keys, False)
    elif case == "callers-two-groups":
        groups = [
            _EdgeGroup("handle", "pkg.a.handle", "src/a.py", _edge_rows(3)),
            _EdgeGroup("handle", "pkg.b.handle", "src/b.py", _edge_rows(5, start=10)),
        ]
        body = gp._render_edge_answer(
            "callers", "caller", "handle", _SymbolTarget("handle"), groups, keys, False)
    elif case == "scan":
        rows = [{"name": f"s{i}", "qualified_name": f"pkg.s{i}", "file_path": f"src/s{i}.py"}
                for i in range(12)]
        body = gp._render_scan(rows, "Complexity / fan-in hotspots", 5, lambda r: [])
    else:
        from codeintel.providers.lsp import LspProvider

        lsp = LspProvider.__new__(LspProvider)
        lsp._last_backend_error = None                    # type: ignore[attr-defined]
        ref_lines = [f"- src/m{i}.py:{i + 1}" for i in range(6)]
        body = f"## References ({len(ref_lines)})\n" + "\n".join(ref_lines)

    assert _sections(body), f"no count-bearing heading was produced for {case}: {body!r}"
    assert not _headline_disagreements(body), (
        f"{case}: the heading counts something other than the rows it sits above\n{body}")


def test_the_headline_check_can_actually_fail():
    """The negative control for the check above. A body whose heading overstates its rows by an
    order of magnitude — the `StrategyChain.resolve` shape — must be reported, not tolerated."""
    body = "## Callers of StrategyChain.resolve (48)\n- a (x.py)\n- b (y.py)"
    assert _headline_disagreements(body) == [
        "'Callers of StrategyChain.resolve' claims 48, 2 row(s) beneath it"]
    # and a truncation the body DISCLOSES is not a discrepancy
    assert not _headline_disagreements(
        "## Callers of x (48)\n" + "\n".join(f"- r{i} (f.py)" for i in range(3))
        + "\n… (+45 more)")


def test_the_evidence_headline_sums_to_the_rows_it_describes():
    """`N resolved · N name-matched · N unstated` is the breakdown `#34` put above the rows.

    It is in no census family — it is a bold tally with no unit noun — so it is registered in
    `_UNCENSUSED` and checked here. Its own failure mode is the one it was built to fix, one level
    down: a tally that does not add up to the list beneath it.
    """
    from codeintel.graph_edges import _EdgeGroup

    gp = _graph_provider()
    rows = (_edge_rows(2, bucket="resolved")
            + _edge_rows(43, bucket="name-matched", start=2)
            + _edge_rows(5, bucket="unstated", start=45))
    groups = [_EdgeGroup("resolve", "pkg.StrategyChain.resolve", "src/chain.ts", rows)]

    head = gp._evidence_headline(groups, "caller")
    tallied = {int(n): bucket for n, bucket in re.findall(r"(\d+) ([a-z-]+)", head)}
    assert sum(tallied) == len(rows), f"{head!r} does not sum to {len(rows)} rows"
    assert set(tallied.values()) == {"resolved", "name-matched", "unstated"}
    # Silent when every row is one kind: a breakdown that only ever restates the heading is noise,
    # and noise is how a disclosure stops being read.
    assert gp._evidence_headline(
        [_EdgeGroup("x", "x", "f.py", _edge_rows(4, bucket="resolved"))], "caller") == ""


def test_the_confidence_note_counts_the_rows_it_describes():
    """"43 of 50 row(s) were resolved by name matching" is only useful if 50 is the rows."""
    from codeintel.graph_edges import _EdgeGroup

    gp = _graph_provider()
    rows = _edge_rows(2) + _edge_rows(43, start=2)
    for r in rows[:2]:
        r["strategy"] = "import_binding"          # resolved: followed a real import
    for r in rows[2:]:
        r["strategy"] = "suffix_match"            # name-guess: the population the note is about
    note = gp._confidence_note("callers", [_EdgeGroup("resolve", "q", "f.ts", rows)])

    pairs = re.findall(r"(\d+) of (\d+) row\(s\)", note)
    assert pairs, f"the note stated no N-of-M: {note!r}"
    for claimed, total in pairs:
        assert int(total) == len(rows), f"note says 'of {total}' over {len(rows)} rows: {note!r}"
        assert int(claimed) <= int(total), f"more rows qualified than exist: {note!r}"


def test_the_changed_headline_sums_the_sections_beneath_it():
    """`## Changes impact (3 files → 12 symbols, 4 callers elsewhere)` is a summary OF summaries:
    its three numbers are the three section headings beneath it, and nothing re-derives them."""
    gp = _graph_provider()
    files = [f"src/f{i}.py" for i in range(3)]
    syms = [(f"sym{i}", f"src/f{i % 3}.py", 1) for i in range(12)]
    ripple = [{"name": f"c{i}", "qualified_name": f"pkg.c{i}", "file_path": f"src/c{i}.py"}
              for i in range(4)]
    gp._query_rows = lambda *a, **k: []                   # type: ignore[method-assign]
    gp._changed_files = lambda *a, **k: files             # type: ignore[method-assign]

    head = (f"## Changes impact ({len(files)} files → {len(syms)} symbol(s), "
            f"{len(ripple)} callers elsewhere)")
    numbers = [int(n) for n in re.findall(r"(\d+)", head)]
    assert numbers == [len(files), len(syms), len(ripple)], (
        "the impact headline must restate the three sections it sits above, and nothing else")


def test_impact_renders_a_zero_only_when_the_lookup_actually_answered():
    """`## Callers of x (0)` is a claim about the world. `_op_impact` renders it for a half that
    returned NOTHING — which is a claim about the lookup — and those are different sentences.

    This is `outcome.py`'s rule at the renderer: `Ok([])` means "asked, and there is nothing" only
    when the backend was in a position to know.
    """
    gp = _graph_provider()
    gp._op_callers = lambda *a, **k: None                 # type: ignore[method-assign]
    gp._op_callees = lambda *a, **k: None                 # type: ignore[method-assign]

    body = gp._op_impact("handle", "proj", 1000)
    if body is None:
        return                                            # refused outright — the honest outcome
    for half in ("Callers", "Callees"):
        if f"## {half} of handle (0)" in body:
            pytest.fail(
                f"{half.lower()} were never retrieved, and the body states there are none:\n{body}")


def test_the_cross_check_headline_counts_the_rows_it_rendered():
    """The gateway's LSP cross-check states a reference count above its own list."""
    from codeintel.gateway import Gateway

    gw = Gateway()
    fn = getattr(gw, "_cross_check_name_resolved", None)
    if fn is None:
        pytest.skip("the cross-check seam has been renamed — the registry entry is stale")
    source = pathlib.Path(_SRC / "gateway.py").read_text()
    # The renderer builds its heading from the same list it then joins. Pin that structurally:
    # a heading built from one collection and a body built from another is the defect, and it is
    # visible in the source without booting an LSP.
    block = source[source.index("## Cross-check"):][:600]
    assert re.search(r"\{len\((\w+)\)\}", block), block
    counted = re.search(r"\{len\((\w+)\)\}", block).group(1)
    assert re.search(rf"\b{counted}\b", block[block.index("\n"):]), (
        f"the heading counts `{counted}` but the body beneath it is built from something else")


def test_a_no_match_note_claims_nothing_about_the_symbol_itself():
    """"No symbol matching X has callers in this index" must not read as "X has no callers".

    The note exists because the two sentences look alike and only one of them is supported: the
    index not matching a target says nothing about the code.
    """
    gp = _graph_provider()
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget

    others = [_EdgeGroup("handle", "pkg.b.handle", "src/b.py", _edge_rows(2))]
    note = gp._no_symbol_matched_the_hint(
        "callers", "handle@src/a.py", _SymbolTarget("handle", file_hint="src/a.py"), others)

    lowered = note.lower()
    assert "in this index" in lowered, note
    # The scoping clause is the whole point: without it this is "handle has no callers", which is
    # a claim about the code rather than about the lookup.
    assert "not evidence" in lowered or "says nothing" in lowered, (
        f"the note asserts absence without scoping it to the lookup: {note!r}")
    # And the population it names is the symbols this op returned, never "the index".
    gaps = [g for g in gp._pending_gaps if g.get("kind") == "target-hint-unmatched"]
    assert gaps, "a hint that matched nothing must raise a gap, not only a note"
    assert str(len(others)) in gaps[0]["detail"], gaps[0]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: CAPABILITY — "it will answer" ⟹ a real query on THIS repo returns content
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _providers_with_a_probe() -> list[str]:
    """Every provider module on disk whose class exposes a `probe`.

    Read from the filesystem rather than typed here, for the reason `test_incompleteness.py` gives
    about its own provider sweep: a guard whose domain is hand-typed certifies the sites the last
    bug was found at, and a fourth engine would inherit the claim without ever being checked.
    """
    import importlib
    import inspect

    import codeintel.providers as pkg

    out = []
    for path in sorted(pathlib.Path(pkg.__file__).parent.glob("*.py")):
        if path.stem == "__init__":
            continue
        module = importlib.import_module(f"codeintel.providers.{path.stem}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module.__name__ and hasattr(obj, "probe"):
                out.append(path.stem)
                break
    return out


def test_every_provider_that_probes_is_covered_by_the_capability_sweep():
    """The sweep below is parametrized by hand; this is what makes that safe.

    A fourth engine landing with a `probe` and no case here would otherwise ship a `runnable`
    nobody ever checked against an answer — which is exactly how instance #2 shipped.
    """
    covered = {"graph", "lsp", "semantic"}
    found = set(_providers_with_a_probe())
    assert found <= covered, (
        f"{sorted(found - covered)} exposes a probe with no capability case in this file. Its "
        "`runnable` is a claim that it will answer; add the case that drives it with a backend "
        "that boots and answers nothing.")


@pytest.mark.parametrize("engine", ["graph", "lsp", "semantic"])
def test_a_backend_that_boots_and_answers_nothing_is_not_called_runnable(engine, tmp_path):
    """`runnable` is read as "will answer". This asserts it means that.

    Each case puts the engine in the state its own worst bug came from — a process that starts,
    reports health, and cannot answer a question about THIS repository:

    * graph — `list_projects` answers, every real query returns nothing (the 0.10.x wire break).
    * lsp   — the session reaches READY over a TypeScript tree with no `tsconfig.json`, so every
              cross-file reference query comes back empty and correct. Instance #2 exactly.
    * semantic — the engine works and this repository has no chunks.

    ASSERTED ON THE ROLL-UP, not on `runnable`, and the distinction is the contract rather than a
    convenience. The tri-state splits the question deliberately: `runnable` is about the ENGINE and
    `repo_indexed` about this repo, so semantic answers `runnable: True, repo_indexed: False` for
    an un-indexed tree and is right to. LSP has no `repo_indexed` at all — serena keeps no
    persistent index — so its repo-level answerability has nowhere to ride except `runnable`, which
    is why the tsconfig check lives there. `_status_for` is where the three become one word, and
    "N engines ready for this repo" is the sentence a reader actually consumes.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    if engine == "graph":
        gp = _graph_provider()
        gp._run = lambda method, payload, timeout_ms: (          # type: ignore[method-assign]
            {"projects": [{"name": "proj", "root_path": str(repo)}]}
            if method == "list_projects" else None)
        gp._probe_wire_format = lambda name: False               # type: ignore[method-assign]
        report = gp.probe(str(repo))

    elif engine == "lsp":
        from codeintel.providers.lsp import LspProvider, _State

        # Enough TypeScript to clear `_UNSERVED_FILE_FLOOR`, a config that SERVES it, and no
        # tsconfig.json. That is `bench/fixtures/corpus_ts` — where `doctor --deep` reported
        # `3 / 3 engines ready` while every reference query returned an empty list.
        for i in range(6):
            (repo / f"f{i}.ts").write_text(
                "import { forward } from './f0'\nexport function forward() { return 1 }\n")
        (repo / ".serena").mkdir()
        (repo / ".serena" / "project.yml").write_text(
            "language_servers:\n  - typescript\n", encoding="utf-8")

        lsp = LspProvider.__new__(LspProvider)
        lsp.available = True                                     # type: ignore[attr-defined]
        lsp._cmd = "uvx"                                         # type: ignore[attr-defined]

        class _Ready:
            state = _State.READY

            class _Lock:
                def __enter__(self): return self
                def __exit__(self, *a): return False

            _lock = _Lock()

        lsp._sessions = {str(repo): _Ready()}                    # type: ignore[attr-defined]
        report = lsp.probe(str(repo), deep=False)

    else:
        from codeintel.providers.semantic import SemanticProvider

        sp = SemanticProvider()
        report = sp.probe(str(repo))

    from codeintel.doctor import _status_for

    assert _status_for(report) == "fail", (
        f"{engine} rolls up as ready for a repository it cannot answer about — "
        f"{report!r}")
    # And it has to say what to do, or the honest answer is unactionable and gets ignored.
    assert report.get("detail"), f"{engine} claims nothing runnable and explains nothing: {report}"
    assert report.get("remediation"), (
        f"{engine} reports a repository it cannot answer about and names no next action: {report}")


def test_the_lsp_capability_claim_is_scoped_to_the_repo_not_the_process(tmp_path):
    """The positive half, without which the test above is satisfiable by always saying no.

    A READY session over a TypeScript tree WITH a tsconfig must still report runnable — a check
    that fires on correct repositories is one nobody reads, and `bench/run.py daycap` being
    byte-identical before and after the `unresolvable` fix is the measured version of this.
    """
    from codeintel.providers.lsp import LspProvider, _State

    repo = tmp_path / "ok"
    repo.mkdir()
    (repo / "a.ts").write_text("export const x = 1\n")
    (repo / "tsconfig.json").write_text('{"include": ["*.ts"]}\n')

    lsp = LspProvider.__new__(LspProvider)
    lsp.available = True                                         # type: ignore[attr-defined]
    lsp._cmd = "uvx"                                             # type: ignore[attr-defined]

    class _Ready:
        state = _State.READY

        class _Lock:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        _lock = _Lock()

    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text(
        "language_servers:\n  - typescript\n", encoding="utf-8")
    lsp._sessions = {str(repo): _Ready()}                        # type: ignore[attr-defined]

    from codeintel.doctor import _status_for

    report = lsp.probe(str(repo), deep=False)
    assert report["runnable"] is True, report
    assert _status_for(report) == "ok", report


def test_doctor_status_follows_the_probe_it_was_given():
    """`status` is a roll-up of the three questions beneath it and must not widen any of them."""
    from codeintel.doctor import _status_for

    assert _status_for({"installed": True, "runnable": False, "repo_indexed": True}) == "fail"
    assert _status_for({"installed": False, "runnable": False, "repo_indexed": None}) == "fail"
    # An unverified boot is not a failure and not a pass — the tri-state has to survive the roll-up.
    assert _status_for({"installed": True, "runnable": None, "repo_indexed": None}) != "fail"
    assert _status_for({"installed": True, "runnable": True, "repo_indexed": True}) == "ok"


def test_doctor_summary_sums_its_own_engine_rows():
    """`N / M engines ready` is a headline over the rows printed directly beneath it.

    Same shape as a caller count, and it is the line a person reads before deciding whether to
    trust anything else the tool says.
    """
    from codeintel.doctor import render_doctor_text

    engines = {
        "graph": {"status": "ok", "installed": True, "runnable": True, "repo_indexed": True,
                  "detail": "resolved", "remediation": None},
        "lsp": {"status": "fail", "installed": True, "runnable": False, "repo_indexed": None,
                "detail": "no tsconfig", "remediation": "add one"},
        "semantic": {"status": "ok", "installed": True, "runnable": True, "repo_indexed": True,
                     "detail": "1 chunk", "remediation": None},
    }
    ready = sum(1 for e in engines.values() if e["status"] != "fail")
    report = {"ok": True, "project_root": "/repo", "deep": True, "engines": engines,
              "summary": {"ready": ready, "total": len(engines), "healthy": False}}

    assert report["summary"]["ready"] == ready
    assert report["summary"]["total"] == len(report["engines"])

    text = render_doctor_text(report)
    stated = re.search(r"(\d+)\s*/\s*(\d+)\s+engines ready", _strip_ansi(text))
    assert stated, f"the rendered report states no N / M: {text}"
    assert (int(stated.group(1)), int(stated.group(2))) == (ready, len(engines)), (
        f"the rendered headline disagrees with the summary it was handed: {stated.group(0)}")


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_a_registration_runnable_flag_is_only_about_the_file():
    """`collect_registrations` reports `runnable` from `os.access(X_OK)`.

    That is true about the FILE and says nothing about whether the server answers — the same
    substitution as READY. It is allowed to stay a file-level check, but it must not be the only
    thing standing behind the word: `verify.py` exists precisely to ask the stronger question, and
    this pins that the two are different checks rather than one word doing both jobs.
    """
    import inspect

    from codeintel import doctor, verify

    source = inspect.getsource(doctor.collect_registrations)
    assert "X_OK" in source or "which" in source, source
    assert "verify_stdio" not in source, (
        "collect_registrations now claims to have verified the server; if it really does, this "
        "test should assert the handshake instead of the file mode")
    # The stronger check exists and asks a real question of the process.
    stronger = inspect.getsource(verify.verify_stdio_call)
    assert "content" in stronger, "verify_stdio_call no longer inspects what came back"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: COMPLETENESS — `confidence: complete` ⟹ no gap-worthy condition is detectable
# ══════════════════════════════════════════════════════════════════════════════════════════════

# Phrases a renderer uses when it is telling the reader something is missing, unproven or capped.
# A body containing one of these is disclosing a limitation IN PROSE; `gaps` is the same fact in
# the field an integration branches on, and the two must not be able to disagree. This is the
# `attach_confidence` docstring's own argument — "the caller is expected to have said the same
# thing in the body text" — turned into something that fails.
_DISCLOSURES = (
    "not retrieved",
    "name matching",
    "likely spurious",
    "unverified",
    "truncated",
    "unknown rather than none",
    "dropped as name collisions",
    "not shown",
    "distinct symbols",
    "comes from the indexed project that contains",
    "not evidence that",
)


def _discloses(body: str) -> list[str]:
    lowered = body.lower()
    return [phrase for phrase in _DISCLOSURES if phrase in lowered]


@pytest.mark.parametrize(
    "scenario",
    ["name-matched-rows", "ambiguous-target", "row-cap", "lsp-unresolvable-references"],
)
def test_a_body_that_discloses_a_limitation_is_never_stamped_complete(scenario, tmp_path):
    """Prose and `gaps` are the same fact in two fields, and only one of them is machine-readable.

    A renderer that writes the caveat and forgets `_add_gap` ships a body that reads "43 of 50 rows
    were resolved by name matching" under `confidence: complete` — which is the 2026-08-17 bug's
    exact grammar: the honest sentence is present, and the field an agent branches on says the
    answer is whole.
    """
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget
    from codeintel.provider import attach_confidence

    gp = _graph_provider()
    keys = ("b.name", "b.qualified_name", "b.file_path")

    if scenario == "name-matched-rows":
        rows = _edge_rows(2) + _edge_rows(43, start=2)
        for r in rows[:2]:
            r["strategy"] = "import_binding"
        for r in rows[2:]:
            r["strategy"] = "suffix_match"
        groups = [_EdgeGroup("resolve", "q", "f.ts", rows)]
        body = gp._render_edge_answer(
            "callers", "caller", "resolve", _SymbolTarget("resolve"), groups, keys, False,
            gp._confidence_note("callers", groups))

    elif scenario == "ambiguous-target":
        groups = [
            _EdgeGroup("handle", "pkg.a.handle", "src/a.py", _edge_rows(3)),
            _EdgeGroup("handle", "pkg.b.handle", "src/b.py", _edge_rows(2, start=9)),
        ]
        body = gp._render_edge_answer(
            "callers", "caller", "handle", _SymbolTarget("handle"), groups, keys, False)

    elif scenario == "row-cap":
        groups = [_EdgeGroup("handle", "q", "f.py", _edge_rows(4))]
        body = gp._render_edge_answer(
            "callers", "caller", "handle", _SymbolTarget("handle"), groups, keys, True,
            gp._row_cap_note("callers", "handle"))

    else:
        from codeintel.providers.lsp import LspProvider

        repo = tmp_path / "ts"
        repo.mkdir()
        for i in range(6):
            (repo / f"f{i}.ts").write_text("export const x = 1\n")
        (repo / ".serena").mkdir()
        (repo / ".serena" / "project.yml").write_text(
            "language_servers:\n  - typescript\n", encoding="utf-8")
        lsp = LspProvider.__new__(LspProvider)
        lsp._pending_gaps = ()                                # type: ignore[attr-defined]
        unsound = lsp._empty_references_unsound(str(repo), "f0.ts")
        assert unsound is not None, "the corpus_ts shape no longer produces an unsound emptiness"
        lsp._add_gap("references", unsound)
        body = f"## References — not retrieved\n> {unsound.describe()}."
        gp = lsp                                              # gaps live on the lsp provider here

    disclosed = _discloses(body)
    assert disclosed, f"{scenario} produced no disclosure to check:\n{body}"

    gaps = list(gp._pending_gaps or ())
    envelope = attach_confidence(
        {"ok": True, "op": "callers", "target": "x", "result": body,
         "engine": "graph", "cached": False},
        gaps,
    )
    assert envelope["confidence"] == "partial", (
        f"{scenario}: the body discloses {disclosed} and the envelope says the answer is "
        f"complete. gaps={gaps!r}\n{body}")


def test_a_clean_answer_is_still_allowed_to_be_complete():
    """Without this, the test above is satisfiable by stamping everything `partial` — and a
    `partial` that appears on correct answers is one nobody reads, which costs the disclosure its
    only value. `bench/run.py daycap` being byte-identical across the `unresolvable` fix is the
    measured form of this same guard."""
    from codeintel.graph_edges import _EdgeGroup
    from codeintel.graph_targets import _SymbolTarget
    from codeintel.provider import attach_confidence

    gp = _graph_provider()
    rows = _edge_rows(3)
    for r in rows:
        r["strategy"] = "import_binding"
    groups = [_EdgeGroup("handle", "pkg.a.handle", "src/a.py", rows)]
    body = gp._render_edge_answer(
        "callers", "caller", "handle", _SymbolTarget("handle"),
        groups, ("b.name", "b.qualified_name", "b.file_path"), False,
        gp._confidence_note("callers", groups))

    assert not _discloses(body), f"a fully resolved answer disclosed {_discloses(body)}:\n{body}"
    envelope = attach_confidence(
        {"ok": True, "op": "callers", "target": "handle", "result": body,
         "engine": "graph", "cached": False},
        list(gp._pending_gaps or ()),
    )
    assert envelope["confidence"] == "complete", envelope


def test_wire_text_never_manufactures_a_confidence():
    """`_trace_path` relays a per-row confidence the backend supplied. A row the backend never
    scored must stay unscored: defaulting it would invent evidence, which is `_edge_confidence`'s
    own rule ("an unstamped edge is not a confident one") one layer out."""
    from codeintel.wire_text import parse

    # The backend names its columns, and this reply carries no `strategy` and no `confidence` —
    # the 0.9.x shape, and any edge the newer backend leaves unlabelled.
    parsed = parse(
        "trace_path",
        "function: handle\n"
        "direction: callers\n"
        "mode: calls\n"
        "callers_total: 2\n"
        "callers: 2  (cols: qn hop)\n"
        "  pkg.a.caller_one 1\n"
        "  pkg.b.caller_two 2\n",
    )
    hops = (parsed or {}).get("callers") or []
    assert len(hops) == 2, f"the fixture no longer parses as a trace: {parsed!r}"
    for hop in hops:
        assert hop["confidence"] is None, (
            f"a row the backend never scored came back scored: {hop!r}")
        assert hop["strategy"] is None, hop
        assert hop["risk"] is None, hop

    # And when the backend DOES say, the value is relayed rather than re-derived.
    scored = parse(
        "trace_path",
        "function: handle\ndirection: callers\nmode: calls\ncallers_total: 1\n"
        "callers: 1  (cols: qn hop strategy confidence)\n"
        "  pkg.a.caller_one 1 suffix_match 0.28\n",
    )
    row = (scored or {})["callers"][0]
    assert row["strategy"] == "suffix_match", row
    assert str(row["confidence"]) == "0.28", row


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: PRESENCE — a stated quantity ⟹ what was actually written or removed
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_setup_reports_the_chunk_count_the_indexer_returned(tmp_path, monkeypatch):
    """`indexed N new chunk(s)` is a claim about what is now in the database."""
    import codeintel.indexer as indexer_mod
    from codeintel import onboarding

    monkeypatch.setattr(indexer_mod, "Indexer", lambda *a, **k: _FakeIndexer(17))
    result = onboarding._bounded_index(str(tmp_path), timeout_s=30, out=lambda *a, **k: None)
    assert result["chunks"] == 17, result
    assert "17" in result["detail"], result

    # A failing pass must not be reported as a count. `-1` formatted into "indexed N new chunk(s)"
    # would read as a successful index of minus one — the presence-claim version of rendering a
    # failure as an empty answer.
    monkeypatch.setattr(indexer_mod, "Indexer", lambda *a, **k: _FakeIndexer(-1))
    failed = onboarding._bounded_index(str(tmp_path), timeout_s=30, out=lambda *a, **k: None)
    assert failed["status"] != "ok", failed
    assert failed["chunks"] == 0, failed
    assert "-1" not in failed["detail"], failed


class _FakeIndexer:
    def __init__(self, count: int) -> None:
        self._count, self.last_error = count, "index failed" if count < 0 else None

    def index(self, project_root: str) -> int:
        return self._count


def test_the_index_command_reports_the_count_the_indexer_returned():
    """`Indexed N chunks` is printed from the indexer's own return, and a failure must not print a
    count at all — `-1` formatted into that sentence would read as a successful index of -1."""
    source = (_SRC / "commands" / "index.py").read_text()
    printed = re.search(r'print\(f"Indexed \{(\w+)\} chunks"\)', source)
    assert printed, "the `Indexed N chunks` line has changed shape — re-verify what N is"
    counted = printed.group(1)
    # The same name must be what `index()` returned, and the failure branch must be reached first.
    assert re.search(rf"{counted}\s*=\s*\w*\.?index\(", source) or re.search(
        rf"{counted}\s*=\s*indexer\.index\(", source), source
    assert source.index("index failed") < source.index('f"Indexed {'), (
        "the failure branch no longer precedes the count, so a failed pass can print a chunk count")


def test_reset_counts_the_files_it_removed(tmp_path, monkeypatch):
    """`removed N index file(s)` must count files that are actually gone, and the dry run must
    count files that are actually there."""
    from codeintel import reset

    base = tmp_path / "home"
    base.mkdir()
    db = base / "semantic.db"
    made = [db, base / "semantic.db-wal", base / "semantic.db-shm"]
    for f in made:
        f.write_text("x")

    dry = reset._reset_all(str(db), apply=False)
    assert dry["count"] == len(made), dry
    assert all(f.exists() for f in made), "a dry run removed something"
    assert "would be removed" in dry["detail"], dry

    applied = reset._reset_all(str(db), apply=True)
    assert applied["count"] == len(made), applied
    assert not any(f.exists() for f in made), "the count says removed and the files are still here"
    assert str(len(made)) in applied["detail"], applied


def test_the_progress_counter_reports_what_it_counted():
    """`N files, M chunks` on the indexing progress line is what the caller fed it."""
    import inspect

    from codeintel.term import LiveCounter

    # The progress line states two quantities. Both must be the arguments it was handed — a
    # counter that renders one of them from its own accumulated state would drift from the pass
    # it claims to be reporting. The terminal plumbing is `test_term.py`'s subject; this is only
    # about what the numbers refer to.
    source = inspect.getsource(LiveCounter.scan)
    stated = re.search(r'f"\{(\w+):,\} files, \{(\w+):,\} chunks"', source)
    assert stated, f"the scan progress line has changed shape — re-verify what it counts:\n{source}"
    params = list(inspect.signature(LiveCounter.scan).parameters)
    for name in stated.groups():
        assert name in params, (
            f"`{name}` is printed as this pass's count but is not one of its arguments {params}")

    embed_src = inspect.getsource(LiveCounter.embed)
    progress = re.search(r'f"\{(\w+):,\}/\{(\w+):,\} chunks', embed_src)
    assert progress, f"the embed progress line has changed shape:\n{embed_src}"
    embed_params = list(inspect.signature(LiveCounter.embed).parameters)
    for name in progress.groups():
        assert name in embed_params, f"`{name}` is not an argument of embed {embed_params}"


def test_verify_counts_the_tools_it_listed():
    """`{name} {version} — N tools (a, b, c)` must count the very list it then prints."""
    source = (_SRC / "verify.py").read_text()
    line = re.search(r'f"\{name\} \{version\} — \{len\((\w+)\)\} tools \(\{\', \'\.join\((\w+)\)\}\)"',
                     source)
    assert line, "the verify detail line has changed shape — re-verify what N counts"
    assert line.group(1) == line.group(2), (
        f"the count is over `{line.group(1)}` and the listing is of `{line.group(2)}`")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: ATTRIBUTION — a claim about WHICH tree, or about what the order means
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_the_ranked_heading_is_actually_ranked_by_caller_count():
    """`## Ranked Symbols (by caller count)` names its own sort key, and CODE_INTEL.md is committed.

    In no census family — a heading with a parenthesised phrase rather than a count — so it is
    registered in `_UNCENSUSED` and checked here. Its live failure mode is `_as_degree`: 0.10.x
    returns aggregate counts as STRINGS, and `"9" > "138"` lexically, so a heading claiming a
    caller-count ranking would have published a lexical one. That is the law exactly — the sort is
    true about the VALUES and false about the quantity they denote.
    """
    from codeintel.mapper import MapGenerator, _as_degree

    assert _as_degree("138") == 138, "string counts are no longer coerced — the ranking is lexical"
    assert _as_degree("9") < _as_degree("138"), "the ranking compares strings, not quantities"

    # The real 0.10.x wire shape, counts as STRINGS — which is the defect's own habitat.
    wire = {
        "columns": ["fn.name", "fn.file_path", "in_degree"],
        "rows": [["small", "src/a.py", "9"],
                 ["big", "src/b.py", "138"],
                 ["mid", "src/c.py", "40"]],
    }
    rows = wire["rows"]

    from codeintel.graph_resolution import ProjectResolution

    class _Provider:
        available = True

        def _resolve_project(self, project_root):
            return ProjectResolution(name="proj", matched_root="/repo", scope="exact")

        def build_result(self, op, target, files, budget, project_root):
            return {"ok": True, "result": "## Architecture: repo\n1577 nodes, 3440 edges"}

        def _run(self, method, payload, timeout_ms):
            return wire if method == "query_graph" else None

    text = MapGenerator(_Provider()).generate("/repo")
    assert "## Ranked Symbols (by caller count)" in text, (
        f"the fake produced no ranked section, so nothing was checked:\n{text}")

    table = [ln for ln in text.splitlines() if ln.startswith("| `")]
    assert len(table) == len(rows), f"{len(rows)} ranked rows produced {len(table)} table rows"
    counts = [int(ln.rsplit("|", 2)[1].strip()) for ln in table]
    assert counts == sorted(counts, reverse=True), (
        f"the heading claims a caller-count ranking and the rows are in {counts}")


def test_the_map_stamp_reports_the_index_it_read():
    """`from an index of N nodes / M edges` dates the map against the index it was built from.

    Both numbers are parsed out of the architecture text the same run received, so the stamp and
    the body cannot describe different indexes — and when there is no such text the stamp says
    nothing rather than saying zero.
    """
    from codeintel.mapper import _parse_index_counts, _stamp_line

    nodes, edges = _parse_index_counts("## Architecture: repo\n1577 nodes, 3440 edges\n")
    assert (nodes, edges) == (1577, 3440)
    stamp = _stamp_line("2026-09-17", nodes, edges)
    assert "1577 nodes / 3440 edges" in stamp, stamp

    # No counts available ⇒ no claim. `0 nodes / 0 edges` would be a statement about the index.
    assert _parse_index_counts(None) == (None, None)
    bare = _stamp_line("2026-09-17", None, None)
    assert "index of" not in bare and "0 nodes" not in bare, bare


def test_the_unserved_language_note_counts_the_files_it_found(tmp_path):
    """`typescript (771 files)` is the evidence for the whole unserved-language claim — instance
    #1's own disclosure. A count that did not come from the census would make it unfalsifiable."""
    from codeintel.providers.lsp import LspProvider

    repo = tmp_path / "poly"
    repo.mkdir()
    for i in range(7):
        (repo / f"m{i}.ts").write_text("export const x = 1\n")
    for i in range(3):
        (repo / f"m{i}.py").write_text("x = 1\n")
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text(
        "language_servers:\n  - python\n", encoding="utf-8")

    provider = LspProvider.__new__(LspProvider)
    configured, census = provider._language_coverage(str(repo))
    assert configured == ["python"], configured
    assert census.get("typescript") == 7, census

    note = provider._unserved_note(str(repo))
    assert note is not None, "seven unserved TypeScript files produced no note"
    stated = re.search(r"typescript \((\d+) files\)", note[0])
    assert stated, note
    assert int(stated.group(1)) == census["typescript"], (
        f"the note states {stated.group(1)} files and the census found {census['typescript']}")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: CAPABILITY, at the MCP surface — what an agent reads instead of the doctor table
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_code_status_relays_the_same_tri_state_as_doctor(monkeypatch):
    """`code.status` used to build throwaway providers and report one flat boolean per engine, so
    `lsp: true` could mean "uvx is on PATH" for an engine that never boots. It relays doctor's
    tri-state now, and relaying is only honest if nothing widens on the way through."""
    from codeintel import doctor, server

    report = {
        "ok": True, "project_root": "/repo", "deep": False,
        "engines": {
            "graph": {"status": "ok", "installed": True, "runnable": True, "repo_indexed": True,
                      "detail": "resolved", "remediation": None},
            "lsp": {"status": "fail", "installed": True, "runnable": False, "repo_indexed": None,
                    "detail": "no tsconfig", "remediation": "add one"},
            "semantic": {"status": "fail", "installed": True, "runnable": True,
                         "repo_indexed": False, "detail": "0 chunks", "remediation": "index"},
        },
        "summary": {"ready": 1, "total": 3, "healthy": False},
        "versions": {}, "version_skew": None,
    }
    monkeypatch.setattr(doctor, "run_doctor", lambda *a, **k: report)

    out = server._code_status_handler_inner({"project_root": "/repo"})
    for name, probe in report["engines"].items():
        for field in ("installed", "runnable", "repo_indexed", "status"):
            assert out["readiness"][name][field] == probe[field], (
                f"{name}.{field} changed value between doctor and code.status")
    assert out["healthy"] is False, out
    assert out["indexed"] is False, "semantic has no chunks for this repo and `indexed` says it does"


def test_a_refused_doctor_report_claims_no_readiness():
    """An RBAC refusal must not be reported as a health verdict. `ready: 0, healthy: false` on a
    report that ran no probe is a claim about permissions, so it carries `reason` to say so —
    otherwise "0 / 3 engines ready" reads as three broken engines."""
    from codeintel import server

    gw = server._get_gateway()
    gw.allows = lambda role, op: False              # type: ignore[method-assign]
    out = server._code_doctor_handler_inner({"project_root": "/repo", "role": "restricted"})

    assert out["reason"] == "op-not-allowed-for-role", out
    assert out["summary"] == {"ready": 0, "total": 3, "healthy": False}, out
    assert out["engines"] == {}, (
        "a refused report names engine rows, so its 0 / 3 reads as a measurement of them")

    # The root gate is a separate refusal and must be reported as separately.
    gw.allows = lambda role, op: True               # type: ignore[method-assign]
    gw.allows_root = lambda role, root: False       # type: ignore[method-assign]
    scoped = server._code_doctor_handler_inner({"project_root": "/elsewhere", "role": "restricted"})
    assert scoped["reason"] == "root-not-allowed-for-role", scoped
    assert scoped["engines"] == {}, scoped


def test_the_status_fallback_claims_nothing():
    """The fallback is returned when the handler could not run at all. Every capability field in it
    must be false or empty: a fallback that defaulted `healthy` to true would assert readiness from
    the one state that establishes least."""
    from codeintel.server import _STATUS_FALLBACK

    assert _STATUS_FALLBACK["healthy"] is False
    assert _STATUS_FALLBACK["indexed"] is False
    for engine in ("graph", "lsp", "semantic"):
        assert _STATUS_FALLBACK[engine] is False, engine
    assert _STATUS_FALLBACK["readiness"] == {}
    assert _STATUS_FALLBACK["engines"] == ["none"]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: AGGREGATE, in the c4 surface
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _c4_payload(n: int = 6) -> dict:
    return {
        "elements": [{"id": f"e{i}", "name": f"e{i}", "paths": [f"src/e{i}.py"]} for i in range(n)],
        "relations": [], "project": "demo", "fit": {"depth": 2, "how": "auto"},
    }


def test_c4_coverage_sums_to_its_own_total():
    """`coverage: {assigned, unassigned, total}` is three numbers about one population."""
    from codeintel.c4_check import check_layers

    payload = _c4_payload()
    parsed = {"order": ["top", "bottom"],
              "layers": {"members": {"top": ["src/e0*", "src/e1*"], "bottom": ["src/e2*"]}}}

    coverage = check_layers(payload, parsed)["coverage"]
    assert coverage["total"] == len(payload["elements"]), coverage
    assert coverage["assigned"] + coverage["unassigned"] == coverage["total"], (
        f"coverage does not partition its own population: {coverage}")


@pytest.mark.xfail(
    strict=True,
    reason="FINDING, found by this file on its first run — not a known-broken test left to rot. "
           "`check_layers` returns early when no layer order is declared (or the config failed to "
           "parse) with coverage {assigned: 0, unassigned: 0, total: N}, and `render_report` "
           "prints it unconditionally as `coverage: 0 of 6 elements assigned; 0 unassigned`. Six "
           "elements, none assigned, none unassigned: the three numbers do not partition, and "
           "nothing was assessed. `assigned: 0` is true about the assignment step, which never "
           "ran, and false about the elements. Strict, so whoever fixes it is told to delete this "
           "marker rather than leaving a passing test recorded as expected-to-fail.",
)
def test_c4_coverage_partitions_its_population_even_when_nothing_was_assessed():
    """The same invariant on the path where no layering was declared.

    `check_layers` already refuses to call an unlayered config a pass — "layering yields zero
    violations by construction. Reporting that as 'no problems' would dress..." — and the coverage
    line beside that verdict is the half that was not carried through.
    """
    from codeintel.c4_check import check_layers

    for parsed in ({}, {"problem": "bad toml"}):
        coverage = check_layers(_c4_payload(), parsed)["coverage"]
        assert coverage["assigned"] + coverage["unassigned"] == coverage["total"], (
            f"nothing was assessed and coverage still states a partition: {coverage}")


def test_the_c4_dsl_comment_counts_what_the_dsl_contains():
    """`// N elements, M relations, K source files` sits at the top of a file people read as fact."""
    from codeintel.c4 import render_c4_dsl

    elements = [{"id": f"e{i}", "path": f"src/e{i}.py", "title": f"e{i}", "kind": "module",
                 "tech": "Python", "files": 1, "churn": 0, "fan_in": 0, "fan_out": 0,
                 "internal_imports": 0, "import_fan_in": 0, "hotspot": False} for i in range(4)]
    relations = [{"from": "e0", "to": "e1", "kind": "IMPORTS", "weight": 1, "both": False, "n": 1},
                 {"from": "e1", "to": "e2", "kind": "IMPORTS", "weight": 1, "both": False, "n": 2}]
    dsl = render_c4_dsl({
        "elements": elements, "relations": relations, "project": "demo",
        "fit": {"depth": 2, "how": "auto", "cap": 200, "table": {2: 4}},
        "stats": {"hotspot_count": 0, "files_kept": 4},
    })
    stated = re.search(r"//\s*(\d+) elements, (\d+) relations, (\d+) source files", dsl)
    assert stated, f"the DSL header has changed shape — re-verify what it counts:\n{dsl[:400]}"
    assert int(stated.group(1)) == len(elements), dsl[:400]
    assert int(stated.group(2)) == len(relations), dsl[:400]
    assert int(stated.group(3)) == 4, dsl[:400]

    # The header counts the model that follows it, so the DSL body must hold that many elements.
    declared = len(re.findall(r"^\s{2,}\w[\w.]* = \w+ ", dsl, re.M))
    assert declared == len(elements) or declared == 0, (
        f"the header claims {len(elements)} elements and the DSL declares {declared}")


def test_the_c4_layer_tally_sums_to_the_elements_it_placed():
    """`N band(s) over M of K element(s)` — the middle number is the placed ones, not the total."""
    source = (_SRC / "commands" / "c4.py").read_text()
    line = re.search(r"\{(\w+\(?\w*\)?)\} band\(s\) over \{(\w+)\} of \{(\w+)\} element",
                     source)
    assert line, "the inferred-layers line has changed shape — re-verify what it counts"
    bands, placed, total = line.groups()
    assert placed != total, "the line prints the same quantity twice, so `of` states nothing"
    assert bands.startswith("len("), f"`{bands}` is stated as a band count: {bands}"

    # Both of `placed`/`total` must come from the stats the layering produced, not from anything
    # else in scope — "M of K elements" is a claim about the pass that just ran.
    for name in (placed, total):
        bound = re.search(rf"{name}\s*=\s*int\(stats\.get\(", source)
        assert bound, f"`{name}` is printed as a count of that pass and is not read from its stats"


def test_the_graph_export_line_counts_what_it_wrote():
    """`Wrote path (N nodes, M edges)` is a claim about the file just written."""
    source = (_SRC / "commands" / "graph.py").read_text()
    line = re.search(r'Wrote \{[^}]+\}\s*\(\{(\w+)\} nodes, \{(\w+)\} edges\)', source)
    assert line, "the export line has changed shape — re-verify what it counts"
    nodes, edges = line.groups()
    assert nodes != edges, "the export line prints one quantity twice"

    # Both names must be measured off the payload that is then written to the file, not off
    # anything else in scope — the whole content of the claim is that they describe THAT file.
    bound = re.search(rf"{nodes},\s*{edges}\s*=\s*(.+)", source)
    assert bound, f"`{nodes}`/`{edges}` are printed as counts and never bound together: {source[:400]}"
    assert bound.group(1).count("len(") == 2, bound.group(1)
    assert "payload" in bound.group(1), bound.group(1)
    written = re.search(r"f\.write\((\w+)\.render_html\((\w+)\)\)", source)
    assert written and written.group(2) == "payload", (
        "the file is rendered from something other than the payload the counts were taken from")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: CLASSIFICATION — every bench oracle label, checked against a grep over the same file
# ══════════════════════════════════════════════════════════════════════════════════════════════
#
# `bench/` is where this whole class cost the most. The oracle understated the measured engine by
# 10-60 points for a fortnight — `snitch-simulator`'s graph arm read 38% direct precision where the
# truth was 100% — and it was in the one label that exists to charge an engine for being WRONG. It
# failed in the flattering direction for the argument `bench/README.md` makes, which is the bias
# least likely to prompt anyone to look, and two test modules covered the oracles while `score.py`
# and `run.py` had none.
#
# The check below is deliberately NOT another analysis. It re-reads the same file with a plain
# regex and asks whether the label is consistent with the text — the same independence the oracle
# itself claims over the engines it scores. A second AST pass would be a third resolver agreeing
# with the first two, which is the circularity `bench/README.md` opens by rejecting.

_BENCH = _REPO / "bench"
_CORPUS = _BENCH / "fixtures" / "corpus"
_CORPUS_PKG = "src/corpuspkg"


def _bench_on_path():
    if str(_BENCH) not in sys.path:
        sys.path.insert(0, str(_BENCH))


def _mentions(path: pathlib.Path, name: str) -> list[int]:
    """1-based line numbers where *name* appears as a whole word. Plain text, no parsing."""
    pattern = re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])")
    return [i for i, line in enumerate(path.read_text().splitlines(), 1)
            if pattern.search(line.split("#")[0])]


@pytest.mark.parametrize(
    "def_file,symbol",
    [(f"{_CORPUS_PKG}/self_call.py", "relay_self"),
     (f"{_CORPUS_PKG}/sse.py", "_broadcast"),
     (f"{_CORPUS_PKG}/sse.py", "filter")],
)
def test_every_python_oracle_label_agrees_with_a_grep(def_file, symbol):
    """A label is a claim about a site. This re-reads the site and checks the claim.

    Three rules, each one a way the label can be true about its own analysis and false about the
    text it describes:

    * a judged site's file must actually mention the name — a label on a file that never names it
      is a key built from the wrong module path, which is how a `src/` source root silently zeroed
      a whole target;
    * a CALL site's file must contain the name followed by `(` somewhere;
    * an IMPORT site's file must contain an import line naming it.
    """
    _bench_on_path()
    from oracle_py import target_from_definition, truth_for

    if not (_CORPUS / def_file).exists():
        pytest.fail(f"the corpus no longer holds {def_file} — this check is scoring nothing")

    qualified = target_from_definition(str(_CORPUS), def_file, symbol)
    truth = truth_for(str(_CORPUS), qualified)
    judged = {"call": truth.calls, "reference": truth.references,
              "import": truth.imports, "not-target": truth.negatives}
    assert any(judged.values()), f"the oracle judged nothing for {qualified}"

    for label, keys in judged.items():
        for rel_file, enclosing in sorted(keys):
            path = _CORPUS / rel_file
            assert path.exists(), f"{label} labelled a file that is not there: {rel_file}"
            text = path.read_text()
            hits = _mentions(path, symbol)
            assert hits, (
                f"{label} at {rel_file}:{enclosing} — a grep for `{symbol}` in that file "
                f"finds nothing, so the label describes a site that does not exist")
            if label == "call":
                assert re.search(rf"(?<![\w.]){re.escape(symbol)}\s*\(", text), (
                    f"call labelled at {rel_file}:{enclosing} and `{symbol}(` appears nowhere")
            if label == "import":
                assert re.search(rf"^\s*(from|import)\b.*\b{re.escape(symbol)}\b", text, re.M), (
                    f"import labelled at {rel_file}:{enclosing} with no import line naming it")


def test_no_site_in_the_targets_own_defining_file_is_a_proven_negative():
    """Instance #6, stated as a rule instead of as the one fixture that reproduces it.

    `_accounted_by` answers WHERE a name is bound. In the target's own defining module the binding
    it finds is the target's own `def`, and the caller read that as "some other symbol this module
    binds" — so every call a function made to itself from its own file was scored as a FABRICATED
    caller. `snitch-simulator` read 38% direct precision as a result; corrected, 100%.

    `bench/fixtures/corpus/src/corpuspkg/self_call.py` pins the instance. This pins the rule, over
    every target in the list, so the next symbol whose home module calls it is covered without
    anyone remembering to add a fixture.
    """
    _bench_on_path()
    from oracle_py import target_from_definition, truth_for

    for def_file, symbol in [(f"{_CORPUS_PKG}/self_call.py", "relay_self"),
                             (f"{_CORPUS_PKG}/sse.py", "_broadcast"),
                             (f"{_CORPUS_PKG}/sse.py", "filter")]:
        qualified = target_from_definition(str(_CORPUS), def_file, symbol)
        truth = truth_for(str(_CORPUS), qualified)
        home_negatives = [
            (f, enclosing) for f, enclosing in truth.negatives
            if f == def_file
            # `shadowed(relay_self)` really is a different binding: a parameter is nearer than the
            # module-scope `def`, and calling THAT a caller would trade one wrong label for its
            # mirror. Only sites with no nearer binding are the ones this rule is about.
            and not _binds_locally(_CORPUS / f, enclosing, symbol)
        ]
        assert not home_negatives, (
            f"{qualified}: {home_negatives} are in the file that DEFINES {symbol} and nothing "
            f"nearer binds the name there, so calling them proven non-callers asserts the "
            f"opposite of the truth — the label that charges an engine for being wrong")


def _binds_locally(path: pathlib.Path, enclosing: str, name: str) -> bool:
    """Whether *enclosing* itself binds *name* — a parameter, an assignment, or a nested def.

    Textual on purpose: the point of this file is to check the oracle's answer against the source
    rather than against a second implementation of the oracle's reasoning.
    """
    lines = path.read_text().splitlines()
    leaf = enclosing.rsplit(".", 1)[-1]
    for i, line in enumerate(lines):
        if not re.match(rf"^\s*(async\s+)?def\s+{re.escape(leaf)}\s*\(", line):
            continue
        signature = line
        for extra in lines[i + 1:]:
            if signature.count("(") <= signature.count(")"):
                break
            signature += extra
        if re.search(rf"(?<![\w.]){re.escape(name)}\s*[,:=)]", signature.split("(", 1)[-1]):
            return True
        body = "\n".join(lines[i + 1:])
        if re.search(rf"^\s+{re.escape(name)}\s*=", body, re.M):
            return True
    return False


def test_the_python_oracle_abstains_where_it_says_it_abstains():
    """The abstention is a claim too, and it is the one that makes the rest trustworthy.

    `describe` in the Python corpus is judged NOTHING — 0% coverage, one undecidable site. That is
    not a gap in the oracle; it is the rule `bench/README.md` states: a bare name nothing in the
    file accounts for is a global another module could have installed, so Python must abstain
    where TypeScript can prove a negative. Asserting it here stops a future "improvement" from
    quietly deciding it — which would put a proven negative on the exact shape that produced 32
    fabricated callers, and score it as a win.
    """
    _bench_on_path()
    from oracle_py import target_from_definition, truth_for

    qualified = target_from_definition(str(_CORPUS), f"{_CORPUS_PKG}/sse.py", "describe")
    truth = truth_for(str(_CORPUS), qualified)

    judged = truth.calls | truth.references | truth.imports | truth.negatives
    assert truth.decided == 0, (
        f"the oracle now judges `describe` in Python: {judged}. Python cannot prove that "
        f"negative — see bench/README.md, 'the case Python must abstain on'.")
    assert truth.undecidable, "the site is not even recorded as undecidable, so it vanished"
    assert truth.coverage == 0.0, truth.coverage

    # And the site it abstained on is a real mention, not a phantom.
    for rel_file, _enclosing in truth.undecidable:
        assert _mentions(_CORPUS / rel_file, "describe"), rel_file


def test_the_grep_cross_check_can_actually_fail(tmp_path):
    """The negative control. A label pointing at a file that never names the symbol has to be
    caught, or this whole section certifies nothing."""
    target = tmp_path / "nothing.py"
    target.write_text("def unrelated():\n    return 1\n")
    assert _mentions(target, "relay_self") == []
    assert _mentions(target, "unrelated") == [1]


def test_every_typescript_oracle_label_agrees_with_a_grep():
    """The same rule for the TypeScript oracle, which scores the arm that found the worst failure
    this project has seen (`describe`, 32 fabricated callers)."""
    _bench_on_path()
    corpus_ts = _BENCH / "fixtures" / "corpus_ts"
    if not corpus_ts.exists():
        pytest.fail("bench/fixtures/corpus_ts is gone — the TypeScript arm is scoring nothing")

    from oracle_ts import target_from_definition, truth_for

    sources = sorted(p for p in corpus_ts.rglob("*.ts") if p.is_file())
    assert sources, f"no TypeScript sources under {corpus_ts}"

    definition = next((p for p in sources if "forwardReleasedItem" in p.read_text()
                       and "export" in p.read_text()), None)
    assert definition is not None, "the corpus no longer defines forwardReleasedItem"

    rel = str(definition.relative_to(corpus_ts))
    qualified = target_from_definition(str(corpus_ts), rel, "forwardReleasedItem")
    truth = truth_for(str(corpus_ts), qualified)

    judged = {"call": truth.calls, "reference": truth.references,
              "import": truth.imports, "not-target": truth.negatives}
    assert any(judged.values()), f"the TypeScript oracle judged nothing for {qualified}"

    for label, keys in judged.items():
        for rel_file, enclosing in sorted(keys):
            path = corpus_ts / rel_file
            assert path.exists(), f"{label} labelled a missing file: {rel_file}"
            # An aliased import renames the symbol, so the ORIGINAL name need not appear — which
            # is itself a thing the oracle claims to handle. Require the file to name either.
            text = path.read_text()
            named = bool(_mentions(path, "forwardReleasedItem")) or "import" in text
            assert named, (
                f"{label} at {rel_file}:{enclosing} — the file neither names the symbol nor "
                f"imports anything, so the label describes a site that does not exist")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shape: AGGREGATE, in the scorer — the arithmetic every published number rests on
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_bench_per_symbol_line_reports_the_truth_it_scored():
    """`truth: 4 call(s), 2 ref(s), 5 proven non-caller(s), coverage 79%` is printed above every
    arm's result and is what a reader diffs a re-run against. Each number must be the set the
    scorer went on to use — `bench/README.md` asks for exactly that diff ("a re-run that disagrees
    should be diffed PER SYMBOL against the per-symbol counts recorded above")."""
    source = (_BENCH / "score.py").read_text()
    line = re.search(
        r'f"\s*\{symbol[^}]*\} truth: \{len\((\w+)\)\} call\(s\), "\s*\n?\s*'
        r'f"\{len\(([\w.]+)\)\} ref\(s\), \{len\(([\w.]+)\)\} proven non-caller\(s\), "',
        source)
    assert line, "the per-symbol truth line has changed shape — re-verify what it counts"
    calls, refs, negatives = line.groups()
    assert calls == "true_calls", calls
    assert refs == "t.references", refs
    assert negatives == "t.negatives", negatives

    # And `true_calls` must be the same set the direct-caller score is computed against, or the
    # printed truth and the scored truth are two different populations.
    assert re.search(r"true_calls\s*=\s*t\.calls", source), source[:400]
    assert re.search(r"direct\[a\]\.add\(\s*got\[a\]\.callers,\s*true_calls", source), (
        "the per-symbol line prints one truth set and the scorer scores another")


def test_the_scorer_counts_coverage_over_the_population_it_reports():
    """`coverage` is judged-over-mentioned, and the scorer prints its mean across symbols. A mean
    over a list that is not one-per-symbol is the headline-over-rows shape in the benchmark."""
    _bench_on_path()
    from oracle_py import Truth

    truth = Truth(target="pkg.mod.x")
    truth.calls.add(("a.py", "f"))
    truth.references.add(("b.py", "g"))
    truth.negatives.add(("c.py", "h"))
    truth.undecidable.add(("d.py", "i"))

    assert truth.decided == 3, truth.decided
    assert truth.coverage == pytest.approx(3 / 4), truth.coverage

    source = (_BENCH / "score.py").read_text()
    assert re.search(r"covered\.append\(t\.coverage\)", source), (
        "coverage is no longer collected per symbol, so its mean is over something else")
    assert re.search(r"sum\(covered\)\s*/\s*len\(covered\)", source), source[-900:]


def test_bench_provenance_names_the_engine_it_ran():
    """The provenance header exists because a run recorded neither half of its own provenance and
    reported numbers nine points off the published table with no way to say why.

    So the binary it NAMES has to be the binary it INVOKES. A header that printed `codeintel` from
    PATH while `_run_codeintel` shelled out to `CODEINTEL_BENCH_EXE` would be the same defect the
    header was added to fix, one level up — and `CODEINTEL_BENCH_EXE` is the documented way to
    score a working tree, so that path is the common one, not the exotic one.
    """
    source = (_BENCH / "score.py").read_text()

    invoked = re.search(r"def _run_codeintel\([^)]*\bexe: str\b[^)]*\)", source)
    assert invoked, "_run_codeintel no longer takes the exe it runs — re-verify what it invokes"
    assert re.search(r"def _provenance\(root: str, exe: str\)", source), (
        "_provenance no longer receives the exe, so the header cannot be naming what was run")
    assert re.search(r"_provenance\(root, exe\)", source), (
        "run() no longer passes its exe to the provenance header")

    # Both halves are stated: which tree was scored, and which engine scored it.
    for claim in ("scored tree", "engine", "backends"):
        assert claim in source, f"the provenance header no longer states {claim!r}"
    # And the skew warning — the line that says the numbers describe the INSTALLED build.
    assert "declares" in source and "on PATH" in source, (
        "the version-skew warning is gone; it is what tells a reader the run measured a different "
        "tree from the checkout they are reading")


def test_the_evidence_class_never_calls_a_partial_answer_proof():
    """`evidence_class` is a claim about what an answer can be USED for, and its referent is the
    answer — not the op, which is the cheap proxy sitting right beside it.

    An op-keyed constant is locally true and reads as a verdict: every `callers` result would come
    back `evidence`, including the 48-row one in which two rows were callers. So the op supplies a
    CEILING and the answer supplies the verdict, and this is the half of that which can be checked
    without a backend — no path through the stamp may reach `evidence` while the same envelope is
    saying a named part of it is missing.
    """
    from codeintel.provider import _OP_CEILING, attach_confidence

    gap = [{"section": "callers", "kind": "row-cap-reached", "detail": "capped"}]
    clean = {"verified": 4, "possible": 0, "unstated": 0, "returned": 4, "total": 4,
             "truncated": False, "safe_for_destructive": True}
    dirty = {**clean, "possible": 2, "returned": 6, "total": 6, "safe_for_destructive": False}

    for op in _OP_CEILING:
        for gaps in ([], gap):
            for evidence in (None, clean, dirty):
                env = {"ok": True, "op": op, "target": "t", "result": "## x", "engine": "graph"}
                if evidence is not None:
                    env["evidence"] = evidence
                out = attach_confidence(env, gaps)               # type: ignore[arg-type]
                where = (op, bool(gaps), evidence and evidence["safe_for_destructive"])

                assert out["evidence_class"] in ("evidence", "discovery", "advisory"), where
                if out["evidence_class"] == "evidence":
                    assert not gaps, f"{where}: proof, and partial in the same envelope"
                    if evidence is not None:
                        assert evidence["safe_for_destructive"], (
                            f"{where}: proof, over rows that did not all follow a binding")
                # The ceiling is a ceiling: nothing is promoted above what its op can support.
                if _OP_CEILING[op] == "advisory":
                    assert out["evidence_class"] == "advisory", where
                if _OP_CEILING[op] == "discovery":
                    assert out["evidence_class"] == "discovery", where

    # A null result carries no body to classify, and stamping one would imply it has an answer.
    assert "evidence_class" not in attach_confidence(
        {"ok": True, "op": "callers", "target": "t", "result": None, "engine": "graph"})  # type: ignore[arg-type]


def test_an_answer_with_no_row_summary_is_proof_only_as_an_lsp_symbol_lookup():
    """Absent a row summary, `evidence` is earned by what answered and not granted by default.

    The default used to be proof: any engine answering an EVIDENCE-ceiling op in prose — a fan-out,
    a graph answer whose summary was withheld, a future backend — was stamped `evidence`. Only a
    language server's `symbol` resolves a real binding by construction, so only it keeps that."""
    from codeintel.provider import attach_confidence

    def stamp(op, engine):
        return attach_confidence(
            {"ok": True, "op": op, "target": "t", "result": "## x", "engine": engine})["evidence_class"]  # type: ignore[arg-type]

    assert stamp("symbol", "lsp") == "evidence"
    for op, engine in [("callers", "graph"), ("callees", "graph"), ("callers", "graph+lsp"),
                       ("callers", "lsp"), ("symbol", "semantic"), ("symbol", "graph+lsp"),
                       ("callers", "some-new-backend")]:
        assert stamp(op, engine) == "advisory", (op, engine)
