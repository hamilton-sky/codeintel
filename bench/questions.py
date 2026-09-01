"""Ground truth for the agent-cost benchmark, one question set per repository.

Every `must_include` below was verified by reading the repository in question, NOT recalled and NOT
taken from a prior document. That distinction is the whole point: this benchmark exists to replace an
unmeasured claim in the README, and a benchmark whose truth is itself unverified would only move the
claim rather than settle it. Each `Question` carries a `provenance` line naming the command that
established it, so a reader can re-derive the answer instead of believing this file.

The questions are STRATIFIED the way `run.py`'s symbol list is, and for the same reason. A random
draw of "questions about a codebase" is dominated by ones a single `grep` answers, every arm scores
the same, and the measurement says nothing. These are questions where the *structure* is the answer:
who calls a thing, where a value actually comes from, which of several same-named definitions is the
one being asked about. That biases absolute token counts UPWARD for every arm and narrows the spread
between them — the honest direction, because the flattering alternative is to pick questions only a
graph can answer and call the result a comparison.

`must_forbid` is the half that makes a cheap wrong answer cost something. Without it an arm that
gives up early, or that confidently names a docstring as a call site, scores as well as one that did
the work — the same failure mode `bench/README.md` describes proven negatives fixing for the
call-edge benchmark.

TWO REPOSITORIES, deliberately. The `codeintel` set is self-referential: useful, because ground truth
was cheap to establish, but this project's whole defect history says unfamiliar repositories are
where reality lives. `pathly-adapters` is the counterweight — 3,284 files, polyglot (419 Python, 398
TSX, 343 TS), not written for this benchmark, and the same tree `bench/run.py` already scores. A
spread that appears on one repository and not the other is a fact about the repository, not about
the tool, and having two sets is what makes that visible.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    key: str
    prompt: str
    # Regexes (case-insensitive, searched against the agent's final answer text). ALL must match.
    must_include: tuple[str, ...]
    # Regexes that must NOT match. A hit means the answer contains a fabrication or a known trap.
    must_forbid: tuple[str, ...] = ()
    # How the truth was established, so a reader can re-derive it rather than trust this file.
    provenance: str = ""
    # A realistic CORRECT answer. Used by `--dry-run` as a positive control: if this does not score
    # correct, the question's regexes are unsatisfiable and every arm would be marked wrong forever
    # — a harness bug that looks exactly like a product finding. Cheap to write, and it is the only
    # thing that proves `must_include` can actually be matched by prose a model would produce.
    canned_answer: str = ""


@dataclass(frozen=True)
class RepoTarget:
    """A repository to ask questions about, and the name its index is filed under.

    `project` is the indexed project name the `raw_backend` arm needs. It is recorded here rather
    than passed on the command line because getting it wrong does not fail loudly — the backend
    answers about a *different* repository, and the arm would be scored on answers to questions
    nobody asked.
    """
    key: str
    path: str
    project: str


def _env_path(var: str, default: str) -> str:
    return os.path.expanduser(os.environ.get(var, default))


_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REPOS: dict[str, RepoTarget] = {
    "codeintel": RepoTarget(
        key="codeintel",
        path=_env_path("CODEINTEL_AGENTBENCH_REPO", _CHECKOUT),
        project="Users-shammaihamilton-Documents-project-codeintel",
    ),
    "pathly-adapters": RepoTarget(
        key="pathly-adapters",
        # Same environment variable `bench/run.py` already uses for this tree, so a machine that can
        # run one benchmark can run the other without learning a second knob.
        path=_env_path("CODEINTEL_BENCH_PATHLY",
                       "/Users/shammaihamilton/Documents/project/pathly-adapters"),
        project="Users-shammaihamilton-Documents-project-pathly-adapters",
    ),
}


# =================================================================================================
# codeintel — this checkout.
# =================================================================================================

_CODEINTEL_QUESTIONS: tuple[Question, ...] = (
    Question(
        key="q_callers_gateway",
        prompt=(
            "In this repository, which NON-TEST source files under src/ contain code that actually "
            "calls `Gateway.query`? List each file path and the line number of the call. Do not "
            "include test files, and do not include mentions inside strings or docstrings — only "
            "real call sites."
        ),
        must_include=(
            r"src/codeintel/server\.py",
            r"src/codeintel/commands/query\.py",
        ),
        # The docstring trap. injector.py contains `code.query(op="changed")` as prose.
        must_forbid=(r"injector\.py",),
        provenance=(
            "rg -n '\\.query\\(' src/codeintel/ -> server.py:167 and commands/query.py:101 are the "
            "only real call sites; injector.py:74 is the same text inside a docstring."
        ),
        canned_answer=(
            "There are two real call sites: src/codeintel/server.py:167 (inside the MCP handler) and "
            "src/codeintel/commands/query.py:101 (the CLI path). Both call gw.query(...)."
        ),
    ),
    Question(
        key="q_confidence_floor",
        prompt=(
            "This project applies a numeric confidence floor to graph call edges, below which an "
            "edge is treated as low-confidence and disclosed to the caller. What is the numeric "
            "value of that floor, what is the constant called, and which file defines it?"
        ),
        must_include=(
            r"0\.85",
            r"_EDGE_CONFIDENCE_FLOOR",
            r"providers/graph\.py",
        ),
        provenance="rg -n '_EDGE_CONFIDENCE_FLOOR' -> src/codeintel/providers/graph.py:398 = 0.85",
        canned_answer=(
            "The floor is 0.85, held in the constant _EDGE_CONFIDENCE_FLOOR, defined in "
            "src/codeintel/providers/graph.py at line 398."
        ),
    ),
    Question(
        key="q_rank_labels",
        prompt=(
            "When `codeintel map` ranks symbols by caller count, it constrains the query to a fixed "
            "set of node labels so that non-callable things cannot be ranked. Which labels are in "
            "that set, and what is the constant called?"
        ),
        must_include=(
            r"_RANK_LABELS", r"Function", r"Method", r"Class", r"Interface", r"Route",
        ),
        provenance=(
            "src/codeintel/mapper.py:55 -> "
            "_RANK_LABELS = ('Function','Method','Class','Interface','Route')"
        ),
        canned_answer=(
            "The constant is _RANK_LABELS in src/codeintel/mapper.py:55, and it contains Function, Method, Class, "
            "Interface and Route."
        ),
    ),
    Question(
        key="q_semantic_reasons",
        prompt=(
            "The semantic provider distinguishes two different 'no results' conditions with two "
            "different reason strings: one for a repository that has never been indexed, and one "
            "for an index pass that ran and failed. What are the two exact reason strings, and "
            "which one means the pass ran and failed?"
        ),
        must_include=(r"index-failed", r"no-index"),
        provenance="src/codeintel/providers/semantic.py:297 'index-failed'; :304 'no-index'",
        canned_answer=(
            "The two strings are \"no-index\" (nothing was ever indexed) and \"index-failed\" (a pass ran and failed). "
            "index-failed is the one meaning the pass ran and failed."
        ),
    ),
    Question(
        key="q_safe_null",
        prompt=(
            "The never-raise contract means a caller never receives an exception — a failure comes "
            "back as a null result with a reason. Which function constructs that null envelope, and "
            "which file defines it?"
        ),
        must_include=(r"safe_null_result", r"provider\.py"),
        provenance="rg -n 'def safe_null_result' -> src/codeintel/provider.py:67",
        canned_answer=(
            "The function is safe_null_result, defined in src/codeintel/provider.py at line 67."
        ),
    ),
)


# =================================================================================================
# pathly-adapters — a polyglot tree this benchmark did not write.
#
# Chosen for the failure modes they probe, not for coverage:
#   * a cross-language seam (TypeScript calls a Python route) whose only shared token is a URL that
#     also appears in two docs and one comment;
#   * a value that does not live in code at all, but in a JSON schema the module reads at import;
#   * a re-export chain, which is what defeated `run.py`'s own oracle in its first version;
#   * an entry point whose target function is named `main`, one of FOURTEEN in the tree;
#   * a bare name with three separate definitions, where "which one" is the whole question.
# =================================================================================================

_PATHLY_QUESTIONS: tuple[Question, ...] = (
    Question(
        key="q_pa_route_handler",
        prompt=(
            "The Electron studio POSTs to the HTTP path `/runner/terminal/result` on the local "
            "orchestrator. Which Python function HANDLES that route, and which file defines it? "
            "Give the function name and the file path. Only the actual route handler counts — not "
            "documentation, comments, or code that merely mentions the path."
        ),
        must_include=(
            r"runner_terminal_result",
            r"http_server/blueprints/runner/api_lifecycle\.py",
        ),
        # Three decoys mention the path: two markdown docs and a hook module's comments. An arm that
        # greps the string and reports the first or the most-mentioned hit lands on one of these.
        must_forbid=(r"stop_telemetry", r"CLAUDE\.md"),
        provenance=(
            "rg -n 'runner/terminal/result' src/ -> the only @bp.route is "
            "src/pathly_orchestrator/http_server/blueprints/runner/api_lifecycle.py:235, handler "
            "`runner_terminal_result` at :236. Decoys: src/pathly_orchestrator/CLAUDE.md:79 and "
            ":429, src/pathly_hooks/stop_telemetry.py:7 and :115."
        ),
        canned_answer=(
            "The handler is runner_terminal_result, defined in "
            "src/pathly_orchestrator/http_server/blueprints/runner/api_lifecycle.py at line 236, decorated with "
            "@bp.route(\"/runner/terminal/result\", methods=[\"POST\"]) on line 235."
        ),
    ),
    Question(
        key="q_pa_fsm_transitions",
        prompt=(
            "In the pathly_orchestrator FSM, the set of legal state transitions is NOT written in "
            "Python — the module loads it at import time from a data file. Which file holds the "
            "transition table, which key inside that file holds it, and how many states does it "
            "define? Also name the Python module that reads it."
        ),
        must_include=(
            r"state\.schema\.json",
            r"transitions",
            r"\b13\b",
            r"fsm/state\.py",
        ),
        provenance=(
            "src/pathly_orchestrator/fsm/state.py:38-46 reads "
            "src/pathly_data/schemas/state.schema.json and assigns STATES = _SCHEMA['transitions']; "
            "json.load of that file shows len(transitions) == 13."
        ),
        canned_answer=(
            "The transition table lives in src/pathly_data/schemas/state.schema.json, under the top-level key "
            "\"transitions\", and it defines 13 states. It is read at import time by "
            "src/pathly_orchestrator/fsm/state.py, which assigns STATES = _SCHEMA[\"transitions\"]."
        ),
    ),
    Question(
        key="q_pa_reexport",
        prompt=(
            "Where is the function `replace_flow_graph` DEFINED (file and line), and which other "
            "module imports it and actually calls it? Give the calling file and the line of the "
            "call."
        ),
        must_include=(
            r"flow_graph_ops\.py",
            r"flow_defs\.py",
        ),
        provenance=(
            "def replace_flow_graph at src/pathly_orchestrator/db/queries/flow_graph_ops.py:326; "
            "imported at db/queries/flow_defs.py:24 and called at flow_defs.py:90."
        ),
        canned_answer=(
            "replace_flow_graph is defined at src/pathly_orchestrator/db/queries/flow_graph_ops.py:326. It is "
            "imported at src/pathly_orchestrator/db/queries/flow_defs.py:24 and called there at line 90."
        ),
    ),
    Question(
        key="q_pa_entrypoint",
        prompt=(
            "This package installs a console command called `pathly-ff`. Which Python module and "
            "which function does it invoke, and what is the path of the file that defines that "
            "function?"
        ),
        must_include=(
            r"pathly_orchestrator[./]cli[./]ff",
            r"\bmain\b",
        ),
        provenance=(
            "pyproject.toml [project.scripts]: pathly-ff = 'pathly_orchestrator.cli.ff:main'; "
            "def main at src/pathly_orchestrator/cli/ff.py:37. Note `main` is defined 14 times "
            "across src/, so 'rg \"def main\"' alone cannot answer this."
        ),
        canned_answer=(
            "pathly-ff maps to pathly_orchestrator.cli.ff:main, so it invokes the function main defined in "
            "src/pathly_orchestrator/cli/ff.py at line 37."
        ),
    ),
    Question(
        key="q_pa_collision",
        prompt=(
            "A function named `append_event` is defined in THREE different modules in this "
            "repository. Name all three defining file paths. Only definitions count, not calls or "
            "imports."
        ),
        must_include=(
            r"engine_actions\.py",
            r"eventlog\.py",
            r"fsm_events\.py",
        ),
        provenance=(
            "rg -n '^def append_event' src/ -g '*.py' -> fsm/engine_actions.py:469, "
            "eventlog.py:98, db/queries/fsm_events.py:42. Exactly three."
        ),
        canned_answer=(
            "append_event is defined in three files: src/pathly_orchestrator/fsm/engine_actions.py:469, "
            "src/pathly_orchestrator/eventlog.py:98, and src/pathly_orchestrator/db/queries/fsm_events.py:42."
        ),
    ),
)


QUESTION_SETS: dict[str, tuple[Question, ...]] = {
    "codeintel": _CODEINTEL_QUESTIONS,
    "pathly-adapters": _PATHLY_QUESTIONS,
}


def by_key(repo_key: str, keys: list[str] | None = None) -> tuple[Question, ...]:
    """Select questions for one repository, preserving declaration order.

    Keyed by repository because a question set is only meaningful against the tree it was verified
    on. Running the codeintel questions against pathly-adapters would score every arm zero and look
    like a product finding.
    """
    if repo_key not in QUESTION_SETS:
        raise SystemExit(
            f"unknown repo key {repo_key!r}; expected one of {', '.join(QUESTION_SETS)}")
    questions = QUESTION_SETS[repo_key]
    if not keys:
        return questions
    wanted = set(keys)
    picked = tuple(q for q in questions if q.key in wanted)
    missing = wanted - {q.key for q in picked}
    if missing:
        raise SystemExit(
            f"unknown question key(s) for {repo_key}: {', '.join(sorted(missing))}\n"
            f"available: {', '.join(q.key for q in questions)}")
    return picked
