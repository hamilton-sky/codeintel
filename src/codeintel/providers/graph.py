from __future__ import annotations

import difflib

# `shutil`/`subprocess` are no longer called from this module — `BackendClient` (graph_backend.py)
# owns the transport now — but many tests monkeypatch `codeintel.providers.graph.shutil.which` /
# `.subprocess.run` by dotted string path, which pytest resolves by walking attributes off THIS
# module. Since `shutil`/`subprocess` are process-wide singletons, patching either through this
# module's reference patches the same object `graph_backend.py` calls through, so the imports stay
# here purely as a resolution anchor for those tests.
import shutil  # noqa: F401
import subprocess  # noqa: F401
import threading
from typing import Any

from codeintel.graph_backend import BackendClient, _parse_query_rows, _parse_search_results
from codeintel.graph_ops import GraphOps
from codeintel.graph_render import (
    _cypher_literal,
    _strip_project_prefix,
)
from codeintel.graph_resolution import (
    _RESOLVE_TIMEOUT_MS,
    ProjectLookup,
    ProjectResolution,
    ProjectResolver,
)
from codeintel.graph_targets import (
    _has_own_git_dir,
    _parse_symbol_target,
    _same_path,
)
from codeintel.outcome import Missing
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result

# Every op _dispatch recognizes. Kept beside it so "unsupported op" and "op found nothing" stay
# distinguishable — they were the same `None` before, and the resulting `unsupported-op` on a
# perfectly supported `callers` was the most misleading string the never-raise envelope produced.
_GRAPH_OPS = frozenset({
    "impact", "context", "callers", "callees", "chain", "pattern",
    "overview", "changed", "changes", "deadcode", "hotspots",
})


# Ops whose answer is DEFINED BY the repository boundary rather than by a symbol inside it. When
# resolution lands on a containing project instead of the repo that was asked about, these must
# refuse rather than answer: "the monorepo's hotspots" is not a lower-confidence answer to "this
# repo's hotspots", it is the answer to a different question. `deadcode` was the dangerous one — a
# symbol that is dead within one repo is routinely live in its sibling, so an ancestor-scoped answer
# told an agent to delete working code — and it is kept in this set although it is now retired and
# refuses earlier, for the same reason `docs/deploy.md` keeps it in the RBAC example: removing it
# would silently un-scope it the day something takes its name. The symbol-scoped ops are not listed:
# for a genuine subdirectory of a monorepo, an ancestor index is the CORRECT place to find a
# symbol's callers, so those answer and carry a caveat instead.
_ROOT_SCOPED_OPS = frozenset({"overview", "changed", "changes", "deadcode", "hotspots"})

# Ops withdrawn from the product because they were measured wrong, not merely imprecise.
#
# `deadcode` is RETIRED: the implementation is gone, and this entry remains so the op name still
# explains itself to anyone who asks for it. It was withdrawn pending "a labelled corpus measures
# its precision and recall"; that corpus exists now
# (`tests/test_corpus.py::test_deadcode_precision_and_recall_are_measured_not_assumed`) and the
# measurement is what retired it:
#
#   Two pinned real Python repositories, every function and method labelled from the AST — 2,425
#   definitions — with liveness decided by an oracle that errs toward LIVE and records the reference
#   behind each label. Precision AS SHIPPED: 6/24 = 25%. Restricted to real code, with the planted
#   canaries removed, it named 18 candidates on those two repositories and every single one was
#   live. Recall was 60%, and every dead symbol in that denominator was planted: in 2,425 real
#   definitions across two maintained repositories there was NOT ONE dead private symbol to find.
#
# Both directions of the repair were measured too, and neither rescues it. Applying the two fixes
# this codebase already contains elsewhere — requesting `Method` nodes as `hotspots` learned to, and
# restricting candidates to code files as `changed` learned to in 0.15.4 — reaches 89% precision and
# 80% recall on the planted set, but on real code it then names exactly one candidate, and that one
# is `MockRequest.get_type` in requests: a method `http.cookiejar` calls by duck-typed convention,
# whose name appears once in the source.
#
# That last false positive is the whole story, and it is why no further repair was attempted. The
# verification is a name-frequency scan, so it fails on exactly one condition — a symbol whose name
# appears once in the source and is called by a convention outside it. Two repositories produced
# three distinct instances of that condition (non-code nodes the backend labels `Function`,
# interpreter-called dunders, stdlib duck-typed protocol methods), the recorded TypeScript evidence
# adds a fourth and fifth (a rollup plugin hook, object-literal properties), and the set is not
# enumerable: no specification lists `get_type`. An op whose measured yield on real code is zero
# true positives has no benefit to weigh against that.
#
# `callers` on a specific symbol answers the same underlying question — "does anything call this?" —
# and is accurate. That is the substitute, and it is what the docs point at.
#
# `hotspots` was withdrawn alongside `deadcode` and has since been REINSTATED. Its rankings were 100%
# `.tsx` on two repositories that are two-thirds Python and backend TypeScript, caused by two request
# bugs rather than by a missing metric: it asked only for `Function` nodes (so every class method was
# invisible — 2,381 of them on one repo) and capped candidates at 200 rows returned in NAME order,
# making the client-side sort rank an alphabetical 4% slice. Both are fixed, and the fix is measured:
# `test_hotspots_ranks_across_languages` pins the mixed-language behaviour, and re-running the two
# evaluation repositories now yields 11 `.py` / 12 `.tsx` / 2 `.ts` and 18 `.ts` / 7 `.tsx` with the
# gnarliest Python and backend functions at the top. A ranking that cannot see a language now says
# so via `_language_coverage_note` instead of reading like a result.
_WITHDRAWN_OPS: dict[str, str] = {
    "deadcode": (
        "`deadcode` is retired, not merely disabled: a labelled corpus measured its precision at "
        "25%, and on real code with nothing planted it named 18 candidates across two repositories "
        "of which every one was live. There is no implementation left to enable. Use `callers` on a "
        "specific symbol instead — that answers the same question and is verified accurate."
    ),
}


# The full op vocabulary this MCP surface advertises (`server.py`'s `code.query` schema), not just
# the ones THIS engine implements. `_AUTO_ENGINE.get(op, "graph")` (gateway.py) routes any op string
# it doesn't recognize here as its fallback, so a typo of `symbol` or `search` — LSP/semantic ops,
# not graph ops — still lands on `unsupported-op` below and deserves the same "did you mean"
# treatment as a typo of a graph op, not a hint that only lists half the real vocabulary.
_NON_GRAPH_OPS: frozenset[str] = frozenset({"symbol", "search"})


def _suggest_op(unknown: str) -> list[str]:
    """Ops a typo probably meant. Close matches first, then prefix matches — mirrors
    `__main__.py`'s `_suggest` (same approach, not imported: that module is CLI-only and importing
    from it would pull argparse/CLI wiring into the query path). Retired ops are excluded on
    purpose: suggesting `deadcode` for a typo would recommend a feature that fails by design."""
    candidates = sorted((_GRAPH_OPS | _NON_GRAPH_OPS) - set(_WITHDRAWN_OPS))
    close = difflib.get_close_matches(unknown, candidates, n=3, cutoff=0.5)
    prefix = [op for op in candidates if op.startswith(unknown) and op not in close]
    return (close + prefix)[:3]














# The supported backend range. `codebase-memory-mcp` 0.9.x answers `query_graph`/`search_graph`
# with `{"columns": [...], "rows": [...]}`, which every renderer here parses. 0.10.x replaced that
# with a compact human-readable text format; `list_projects` stayed JSON, so project resolution and
# `doctor` still work while EVERY other op silently returns nothing. That combination is the worst
# possible: the tool looks healthy and answers "not in the graph index" about a fully indexed repo.
_SUPPORTED_BACKEND = "0.9.x and 0.10.x"
_INCOMPATIBLE_HINT = (
    "the graph backend returned a response this release cannot parse — codebase-memory-mcp "
    f"{_SUPPORTED_BACKEND} are both understood (0.9.x answers in JSON rows, 0.10.x in a text "
    "layout this release reads), so this is a THIRD shape: most likely a backend newer than this "
    "codeintel. Check for a newer codeintel, or pin a known-good backend (pip/uv: "
    "`pip install 'codebase-memory-mcp==0.10.*'`; standalone binary: re-install a 0.10.x build). "
    "This is NOT a statement about whether your repository is indexed."
)

# The "backend is not installed" remediation, stated ONCE. `probe` hands it to `doctor` as a
# structured row; the query path puts it in the envelope's `hint`. Both readers need it and they
# used to have only one supplier: an agent making its first `code.query` — the exact call the
# README tells a new user to run — received a bare `engine-unavailable` with nothing to act on,
# while `doctor` three lines away knew the fix. A second copy of the text would drift, so the
# envelope reads this one.
_UNAVAILABLE_DETAIL = "codebase-memory-mcp not found on PATH"
_UNAVAILABLE_REMEDIATION = (
    "put the codebase-memory-mcp binary on PATH — it's an external native backend (see "
    "docs/graph.md); once present it self-updates via `codebase-memory-mcp update`"
)
_UNAVAILABLE_HINT = (
    f"{_UNAVAILABLE_DETAIL}; {_UNAVAILABLE_REMEDIATION}. The graph engine is optional: "
    "`--op search` answers with no backend at all. This is NOT evidence the symbol has no callers."
)





































class GraphProvider(GraphOps):
    """Wraps the codebase-memory-mcp CLI. Never raises.

    Backend contract (verified against codebase-memory-mcp 0.9.0 by dogfooding, not assumed):
      * ``list_projects``  → ``{"projects": [{name, root_path, ...}]}``
      * ``query_graph``    → ``{"columns": [...], "rows": [[...], ...], "total": N}``  — rows are
                             value-arrays aligned to ``columns``, NOT a list of dicts.
      * ``trace_path``     → ``{function, callees: [{name, qualified_name, hop}], callers: [...]}``
                             or ``{"status": "ambiguous", "suggestions": [...]}``.
      * ``search_code``    → ``{"results": [{node, qualified_name, label, file, match_lines}]}``.
      * ``get_architecture`` → ``{project, total_nodes, total_edges, node_labels, edge_types, languages}``.
      * ``search_graph``   → ``{"total": N, "results": [{name, qualified_name, file_path, in_degree,
                             out_degree, complexity, cognitive, lines, is_test, is_entry_point}, ...]}``
                             — degree filters (max_degree/min_degree/exclude_entry_points) + metrics.
      * ``detect_changes`` → ``{"changed_files": [path, ...], "impacted_symbols": [{qualified_name,
                             name, file_path}, ...], "changed_count": N, "depth": D}``. changed_files
                             come DUPLICATED (staged+unstaged); impacted_symbols interleaves bare file
                             markers (label == file_path) with real symbols.

    Call graph: module-level function calls are recorded as ``USAGE`` edges from the calling
    ``Module`` node; method/function-to-method calls are ``CALLS`` edges. "Who calls X" therefore
    needs BOTH edge types (``[:CALLS|USAGE]``) — ``CALLS`` alone misses every module-level callee
    (that is why the old ``(caller)-[:CALLS]->(fn)`` query returned zero rows for real symbols).
    """

    def __init__(self) -> None:
        self._backend = BackendClient()
        self._resolver = ProjectResolver(self._backend)

    # `available`/`_cmd`/`_saw_unparsable`/`_last_failure` live on `self._backend` now (see
    # graph_backend.py) — exposed here as properties so the ~27 internal `self.available` /
    # `self._cmd` / `self._saw_unparsable` / `self._last_failure` references below, the external
    # consumers (grapher.py, mapper.py, reindexer.py, server.py), and the tests that do
    # `gp.available = True` on a `GraphProvider.__new__(GraphProvider)` instance all keep working
    # unchanged, as long as `gp._backend` exists.
    @property
    def available(self) -> bool:
        return self._backend.available

    @available.setter
    def available(self, value: bool) -> None:
        self._backend.available = value

    # Read by `Gateway._dispatch_single`, which short-circuits on `available is False` and so never
    # reaches this provider's own `build_result`. Without it the gateway had no way to ask WHY an
    # engine was unavailable and emitted a hintless envelope — the defect this attribute closes.
    # A plain attribute rather than a method so a stub provider in a test can set it in one line.
    unavailable_hint = _UNAVAILABLE_HINT

    @property
    def _cmd(self) -> str | None:
        return self._backend._cmd

    @_cmd.setter
    def _cmd(self, value: str | None) -> None:
        self._backend._cmd = value

    @property
    def _saw_unparsable(self) -> bool:
        return self._backend._saw_unparsable

    @_saw_unparsable.setter
    def _saw_unparsable(self, value: bool) -> None:
        self._backend._saw_unparsable = value

    @property
    def _last_failure(self) -> Missing | None:
        return self._backend._last_failure

    @_last_failure.setter
    def _last_failure(self, value: Missing | None) -> None:
        self._backend._last_failure = value

    # `_project_cache`/`_negative_until`/`_project_cache_lock` live on `self._resolver` now (see
    # graph_resolution.py) — exposed here as properties for the same reason the four backend
    # attributes above are: tests build a provider with `GraphProvider.__new__(GraphProvider)` and
    # set these directly, which requires `gp._resolver` to exist first.
    @property
    def _project_cache(self) -> dict[str, ProjectResolution]:
        return self._resolver._project_cache

    @_project_cache.setter
    def _project_cache(self, value: dict[str, ProjectResolution]) -> None:
        self._resolver._project_cache = value

    @property
    def _negative_until(self) -> dict[str, float]:
        return self._resolver._negative_until

    @_negative_until.setter
    def _negative_until(self, value: dict[str, float]) -> None:
        self._resolver._negative_until = value

    @property
    def _project_cache_lock(self) -> threading.Lock:
        return self._resolver._project_cache_lock

    @_project_cache_lock.setter
    def _project_cache_lock(self, value: threading.Lock) -> None:
        self._resolver._project_cache_lock = value

    # Sentinel: distinguishes "the subprocess call failed" from "it succeeded and returned JSON
    # null". Overloading None for both would make a legit null result wrongly trigger the fallback.
    _FAIL = BackendClient._FAIL
    # Sentinel: the backend ran and exited 0, but did not speak JSON — a protocol/version
    # mismatch rather than a failure. Kept separate from _FAIL so it survives to the caller.
    _UNPARSABLE = BackendClient._UNPARSABLE

    # Thin delegators: ~a dozen tests stub these on a provider instance (`gp._run = ...`), and the
    # ops call `self._run(...)` — keeping them as overridable methods here is what lets a stub still
    # intercept while the real implementation lives on `BackendClient`.
    def _run(self, method: str, payload: dict, timeout_ms: int) -> Any | None:
        return self._backend._run(method, payload, timeout_ms)

    def _run_stdin(self, method: str, body: str, timeout_ms: int) -> Any:
        return self._backend._run_stdin(method, body, timeout_ms)

    def _run_rawjson(self, method: str, body: str, timeout_ms: int) -> Any:
        return self._backend._run_rawjson(method, body, timeout_ms)

    # `_match_project` is pure (no backend call), so it can be a genuinely thin delegator with no
    # stub-seam consequence.
    @staticmethod
    def _match_project(raw: Any, project_root: str) -> ProjectResolution | None:
        return ProjectResolver._match_project(raw, project_root)

    # `_lookup_project` fetches through `self._run` (its own overridable delegator) rather than
    # `self._resolver._lookup_project(...)` wholesale — the same deviation `_query_rows` makes in
    # graph_backend.py's module docstring, and for the same reason: a large population of tests
    # stub the transport at `_run` alone (`monkeypatch.setattr(p, "_run", ...)`) and expect
    # `_resolve_project`/`build_result` (which read through here) to honour that stub. Routing the
    # fetch through `self._resolver`'s own `self._backend._run(...)` would silently bypass it. The
    # caching state (`_project_cache`/`_negative_until`/`_project_cache_lock`) and the matching logic
    # (`_match_project`) still come from `self._resolver`/its delegator, so there is exactly one
    # cache and one matcher — only the fetch call is duplicated, matching `_query_rows`'s shape.
    def _lookup_project(self, project_root: str) -> ProjectLookup:
        """Resolve a root to a backend project — delegated to `ProjectResolver`, which owns the one
        copy of the lookup body (its docstring carries the ~5.8s-allocator story that shaped the
        timeout). Passes this provider's OWN `self._run` so a `gp._run` stub gates resolution the way
        a large population of tests expect. Kept as an overridable method because 6 tests stub
        `gp._lookup_project` directly."""
        return self._resolver._lookup_project(project_root, self._run)

    def _resolve_project(self, project_root: str) -> ProjectResolution | None:
        """The resolution alone, for callers that only branch on found/not-found.

        Calls `self._lookup_project` — this provider's OWN overridable delegator — rather than
        `self._resolver._resolve_project(...)`: 6 tests stub `gp._lookup_project` directly, and
        routing through the resolver's own `_resolve_project` (which calls the resolver's OWN
        `_lookup_project`) would silently bypass that stub."""
        return self._lookup_project(project_root).resolution

    def _deep_answer(self, project: str, timeout_ms: int) -> bool | None:
        """Does a real query against THIS project come back with a row? ``None`` if it could not ask.

        `_probe_wire_format` already sends a genuine `query_graph`, and it is not this check: it
        asks whether the backend's REPLY is in a dialect this release can read, and an empty result
        set answers that perfectly well. So a project registered with zero nodes — an index that
        failed, was reset, or was registered against an unreadable tree — passes it, and the probe
        then reports `runnable: true, repo_indexed: true` about an engine that will answer every
        question with nothing.

        True about the wire, false about the repository. This is the second half of the question,
        and the only one a reader means by "ready".
        """
        rows = self._query_rows("MATCH (a) RETURN a.name LIMIT 1", project, timeout_ms)
        if self._last_failure is not None:
            return None                     # could not ask — never reported as an empty answer
        return bool(rows)

    def probe(self, project_root: str, timeout_ms: int = _RESOLVE_TIMEOUT_MS,
              *, deep: bool = False) -> dict:
        """Never-raise health check for the doctor.

        Returns ``{installed, runnable, repo_indexed, project, detail, remediation}``. Shallow is
        one ``list_projects`` call, bounded by ``timeout_ms``. ``deep`` adds one real query against
        the resolved project and requires it to return a row — see `_deep_answer`, and
        `docs/doctor.md` for why booting is not answering."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": False, "project": None,
                "detail": _UNAVAILABLE_DETAIL,
                "remediation": _UNAVAILABLE_REMEDIATION,
            }
        raw = self._run("list_projects", {}, timeout_ms)
        if raw is None:
            return {
                "installed": True, "runnable": False, "repo_indexed": False, "project": None,
                "detail": "codebase-memory-mcp is installed but list_projects failed/timed out",
                "remediation": "check `codebase-memory-mcp cli list_projects '{}'` works",
            }
        # `list_projects` is the ONE call 0.10.x still answers in JSON, so a probe that stopped
        # here would report a fully healthy graph engine on a backend where every actual query
        # returns nothing. Ask a real query the way a query would, and report the mismatch.
        if self._probe_wire_format(self._any_project_name(raw)) is False:
            return {
                "installed": True, "runnable": False, "repo_indexed": False, "project": None,
                "detail": f"incompatible codebase-memory-mcp — this release speaks "
                          f"{_SUPPORTED_BACKEND}, and the installed backend answers in neither, "
                          f"so every graph op except project resolution returns nothing",
                # Two install shapes exist and only one takes a pip command: the PyPI launcher, and
                # a standalone native binary that self-manages. Naming only pip left the binary
                # users — including this project's own maintainer — with an instruction they could
                # not run, which is the failure mode this whole check exists to avoid.
                "remediation": "upgrade codeintel first — a backend newer than this release is "
                               "the usual cause. If that does not resolve it, pin a known-good "
                               "backend: pip/uv installs `pip install "
                               "'codebase-memory-mcp==0.10.*'`; standalone binary: re-install a "
                               "0.10.x build for your platform.",
            }
        resolution = self._match_project(raw, project_root)
        if resolution is None:
            return {
                "installed": True, "runnable": True, "repo_indexed": False, "project": None,
                "detail": "backend OK but this repo is not indexed in the graph",
                "remediation": f"codeintel index {project_root}",
            }
        # Resolution falls back to the nearest indexed ANCESTOR, which is right for a subdirectory
        # of an indexed repo and badly wrong for a repo that merely sits inside one. Asking about
        # `~/projects/my-app` when only `~/projects` is indexed reported "ready" and then answered
        # from a graph spanning every repo on the machine — the top two refactor hotspots for one
        # project came from another project's build output. Ready, but not for what was asked.
        # `build_result` now consults the SAME resolution record, so what the doctor reports and
        # what a query actually does can no longer disagree.
        if resolution.is_ancestor:
            own_repo = _has_own_git_dir(project_root)
            # Derived, not typed: a retired op named in this list would send the reader looking
            # for something that refuses for an unrelated reason.
            runnable_scoped = ", ".join(sorted(_ROOT_SCOPED_OPS - set(_WITHDRAWN_OPS)))
            scoped = f"the repo-wide ops ({runnable_scoped}) will refuse"
            answered = self._deep_answer(resolution.name, timeout_ms) if deep else True
            return {
                "installed": True, "runnable": True if answered else answered,
                "repo_indexed": True,
                "project": resolution.name,
                "detail": (f"this repo is NOT indexed on its own — answers would come from "
                           f"'{resolution.name}' ({resolution.matched_root}), which contains it; "
                           f"{scoped}"
                           + (" (this directory is its own git repository, so it is a nested repo "
                              "rather than a subdirectory of that project)" if own_repo else "")),
                "remediation": f"codeintel index {project_root}",
            }
        if deep:
            answered = self._deep_answer(resolution.name, timeout_ms)
            if answered is False:
                return {
                    "installed": True, "runnable": False, "repo_indexed": True,
                    "project": resolution.name,
                    "detail": (f"project '{resolution.name}' resolves, and a real query against it "
                               f"returns no rows at all — the registration exists but the index "
                               f"behind it is empty, so every question will answer nothing"),
                    "remediation": f"codeintel index {project_root}",
                }
            if answered is None:
                return {
                    "installed": True, "runnable": None, "repo_indexed": True,
                    "project": resolution.name,
                    "detail": (f"project '{resolution.name}' resolves, but the verification query "
                               f"did not complete, so whether it will answer is unknown"),
                    "remediation": "re-run `codeintel doctor --deep`; if it persists, check "
                                   "`codebase-memory-mcp cli query_graph`",
                }
        return {
            "installed": True, "runnable": True, "repo_indexed": True, "project": resolution.name,
            "detail": (f"resolved project '{resolution.name}' in codebase-memory-mcp"
                       + (" and it answered a real query" if deep else "")),
            "remediation": None,
        }

    # The root the ANSWERING project is registered under, recorded per query so a renderer can
    # check what it is about to attribute. Class-level default for the same __new__ reason.
    _answered_root: str | None = None
    # Parts of this answer known to be short of an answer. Graph has two real cases: a symbol-scoped
    # answer served from a CONTAINING project, and callee rows dropped as name collisions.
    _pending_gaps: tuple[dict[str, Any], ...] = ()
    # The structured form of the rows an edge op rendered, and the three facts needed to summarise
    # them honestly once the answer is whole. Same mechanism as `_pending_gaps` above and the same
    # caveat: `refactor-graph-provider.md`'s open phase 4 would have the renderer return these
    # instead of the provider carrying them. Class-level defaults because five test modules build a
    # provider with `__new__` and never run `__init__`.
    _pending_rows: tuple[dict[str, Any], ...] = ()
    # The backend's own row cap was hit, so the total is unknown rather than large.
    _pending_row_cap: bool = False
    # Rows retrieved and deliberately not printed (the candidate cap). Known, so counted, not None.
    _pending_withheld: int = 0
    # The body carries `- ` lines that are not result rows, so no row summary of it can be true.
    _pending_nonrow_lines: bool = False
    # The token a name match did not use, and which of the files in doubt name it. `None` for both
    # means nobody looked — never "nobody names it".
    _qualifier_token: str | None = None
    _qualifier_files: dict[str, bool] | None = None

    def _clear_failure(self) -> None:
        self._backend._clear_failure()

    def _add_gap(self, section: str, kind: str, detail: str) -> None:
        self._pending_gaps = (*self._pending_gaps, {
            "section": section, "kind": kind, "detail": detail,
        })

    def _answered_root_mismatch(self, asked_root: str) -> bool:
        """Whether this answer is about a tree other than the one the caller asked about.

        A check on the resolved DATA rather than on the registry's claim. Resolution can legitimately
        report an exact match while the backend answers from elsewhere — removing a project's index
        file does not deregister it — so a renderer that wants to name the repo has to ask this
        first. Returns False when the answering root is unknown, because an unverifiable mismatch is
        not evidence of one."""
        if not self._answered_root or not asked_root:
            return False
        return not _same_path(self._answered_root, asked_root)

    @classmethod
    def _reset_wire_format_cache(cls) -> None:
        BackendClient._reset_wire_format_cache()

    def _probe_wire_format(self, project: str) -> bool | None:
        return self._backend._probe_wire_format(project)

    @staticmethod
    def _any_project_name(raw: Any) -> str:
        return BackendClient._any_project_name(raw)

    @staticmethod
    def _project_root_of(raw: Any, name: str | None) -> str | None:
        return ProjectResolver._project_root_of(raw, name)

    # ------------------------------------------------------------------ helpers

    # `_query_rows` fetches through `self._run` (its own overridable delegator) rather than
    # `self._backend._query_rows(...)` wholesale, then reuses the same parser — see
    # graph_backend.py's module docstring for why: tests stub the transport at `_run` alone and
    # expect `callers`/`callees` (which read through here) to honour that stub.
    _EDGE_OPS_WITH_A_SYMBOL_TARGET = ("callers", "callees", "impact", "chain", "context")

    # Structural edges say where a symbol LIVES, not what depends on it. Naming them in a
    # "nothing references this" message would answer a question nobody asked.
    _STRUCTURAL_EDGES = frozenset({
        "DEFINES", "DEFINES_METHOD", "CONTAINS_FILE", "CONTAINS_FOLDER", "HAS_BRANCH",
        "FILE_CHANGES_WITH", "SIMILAR_TO", "SEMANTICALLY_RELATED",
    })

    def _dependency_kinds(self, target: str, project: str, timeout_ms: int) -> dict[str, int]:
        """Every non-structural relationship touching *target*, by kind.

        The point is to stop "no callers" from being the end of the sentence. A symbol reached only
        by `INHERITS`, `DECORATES`, `HANDLES` or `TESTS` is not unreferenced — it is referenced in a
        way this op does not cover, and saying which way is the difference between a usable answer
        and one that reads as "unused"."""
        wanted = _parse_symbol_target(target)
        if not wanted.name:
            return {}
        # ALIASED. Unaliased, the backend names the column `COUNT(*)` — uppercased — while the
        # query says `count(*)`, so a lookup by the written name silently missed and every kind
        # rendered as "0 x KIND". An alias makes the column name ours rather than a formatting
        # detail of whichever backend answered.
        cypher = (
            f'MATCH (a)-[c]->(b) WHERE b.name="{_cypher_literal(wanted.name)}" '
            "RETURN type(c) AS kind, count(*) AS n LIMIT 30"
        )
        out: dict[str, int] = {}
        for r in self._query_rows(cypher, project, timeout_ms):
            kind = str(r.get("kind") or "").strip()
            if not kind or kind in self._STRUCTURAL_EDGES:
                continue
            try:
                count = int(str(r.get("n") or 0))
            except (TypeError, ValueError):
                continue
            if count > 0:            # a zero is a parse miss, never a fact worth printing
                out[kind] = count
        return out

    def _node_locations(self, target: str, project: str, timeout_ms: int) -> list[str]:
        """Where a symbol with this bare name is DEFINED, independent of whether it has edges.

        The distinction this exists to draw is the one that decides whether deleting a symbol is
        safe. `not-in-graph` used to be returned for two situations that are opposites:

          * the symbol genuinely is not indexed — a stale index, a typo, a rename; and
          * the symbol is indexed perfectly well and simply has no incoming CALLS edge.

        The second is the normal state of every framework-dispatched handler (a Flask route, an
        ASGI entrypoint) and of every method passed as a value rather than called — on one evaluated
        repository `forward_released_item` is defined at proxy.py:392, is registered through
        `set_forward_fn(app.forward_released_item)`, and `pattern` finds it immediately, yet
        `callers` reported it "not in the graph index" and advised a re-index that cannot change the
        answer. An agent that reads that and concludes the method is unused deletes a live one.

        The sibling checks above this one already refuse to let a backend outage masquerade as a
        fact about the repository. This closes the remaining path to the same misreading, which is
        the one that arrives through a perfectly healthy backend."""
        wanted = _parse_symbol_target(target)
        if not wanted.name:
            return []
        cypher = (
            f'MATCH (n) WHERE n.name="{_cypher_literal(wanted.name)}" '
            "RETURN n.qualified_name, n.file_path LIMIT 10"
        )
        out: list[str] = []
        for r in self._query_rows(cypher, project, timeout_ms):
            qn = _strip_project_prefix(str(r.get("n.qualified_name") or ""), may_be_filename=False)
            fp = str(r.get("n.file_path") or "")
            if wanted.narrowed and not wanted.matches(str(r.get("n.qualified_name") or ""), fp):
                continue
            label = f"{qn} ({fp})" if qn and fp and qn != fp else (qn or fp)
            if label and label not in out:
                out.append(label)
        return out

    def _query_rows(self, cypher: str, project: str, timeout_ms: int) -> list[dict]:
        raw = self._run("query_graph", {"project": project, "query": cypher}, timeout_ms)
        return _parse_query_rows(raw)




    # Same reasoning as `_query_rows` above: fetch via `self._run`, parse via the shared helper.
    def _search_symbols(self, extra: dict, project: str, timeout_ms: int) -> list[dict] | None:
        raw = self._run("search_graph", {"project": project, **extra}, timeout_ms)
        return _parse_search_results(raw)


























    def build_result(
        self,
        op: Any,
        target: Any,
        files: Any,
        budget: Any,
        project_root: Any,
    ) -> Result:
        try:
            op_str = str(op or "")
            target_str = str(target or "")
            root_str = str(project_root or "")

            if not self.available:
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="engine-unavailable",
                    hint=_UNAVAILABLE_HINT,
                )

            try:
                budget_ms = int(budget) if budget else 0
            except Exception:
                budget_ms = 0
            timeout_ms = budget_ms if budget_ms > 0 else 5000

            lookup = self._lookup_project(root_str)
            if lookup.reason == "backend-unreachable":
                # Do NOT say "not indexed" here. That claim is about the repository, this failure
                # is about the backend, and the remedy it implies (`codeintel index`) cannot fix a
                # backend that is not answering — it just runs the same timeout again.
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="backend-unreachable",
                    hint="the graph backend did not respond in time — check "
                         "`codebase-memory-mcp cli list_projects '{}'` runs, and raise "
                         "CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS if it is simply slow on this machine",
                )
            resolution = lookup.resolution
            if resolution is None:
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="project-not-indexed",
                    hint=f"run: codeintel index {root_str}  (or: codeintel doctor)",
                )

            if op_str not in _GRAPH_OPS:
                # The only safe-null in this file that used to carry no hint and no way forward —
                # a wrong op guess is easily misread as "found nothing". `deadcode` and other WITHDRAWN ops are
                # handled separately below with their own `op-withdrawn` reason and rationale; this
                # branch is reached only by a genuinely unrecognized op string.
                matches = _suggest_op(op_str)
                known = sorted((_GRAPH_OPS | _NON_GRAPH_OPS) - set(_WITHDRAWN_OPS))
                hint = f"ops: {', '.join(known)}"
                if matches:
                    hint = f"did you mean {' or '.join(matches)}? {hint}"
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="unsupported-op", hint=hint,
                )

            # The repo asked about is not indexed on its own; this answer would come from a project
            # that merely CONTAINS it. For a root-scoped op that is not a weaker answer to the
            # question, it is a confident answer to a different one — `hotspots` over a parent
            # directory ranks another repository's build output above this repo's own code. Refuse,
            # and say
            # which project the answer would have come from so the caller can tell this apart from
            # "nothing indexed at all". Symbol-scoped ops fall through: for a real subdirectory of a
            # monorepo the containing index is exactly where a symbol's callers live.
            if resolution.is_ancestor and op_str in _ROOT_SCOPED_OPS:
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="project-not-indexed-standalone",
                    hint=f"`{root_str}` is not indexed on its own — it resolves to the project "
                         f"containing it, whose {op_str} would describe a different tree. "
                         f"Index it standalone with: codeintel index {root_str}",
                )

            # Checked AFTER the scope gate on purpose: "this repo is not indexed on its own" is the
            # more specific and more actionable answer, and it stays the one the caller gets.
            # Unconditional. There used to be a `CODEINTEL_ENABLE_UNVERIFIED_OPS=1` opt-in here,
            # which made sense while a withdrawn op still had an implementation behind it. Retiring
            # `deadcode` removed the thing the flag enabled, and a flag that enables nothing is a
            # promise the code cannot keep — worse than no flag, because a reader sets it and
            # believes something changed.
            if op_str in _WITHDRAWN_OPS:
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="op-withdrawn",
                    hint=_WITHDRAWN_OPS[op_str],
                )

            project = resolution.name
            # Record what the answer will actually be about, so renderers can check before they
            # attribute it to the caller's repo.
            self._answered_root = resolution.matched_root
            self._pending_gaps = ()
            self._pending_rows = ()
            self._pending_row_cap = False
            self._pending_withheld = 0
            self._pending_nonrow_lines = False
            self._qualifier_token = None
            self._qualifier_files = None
            self._clear_failure()
            result_text = self._dispatch(op_str, target_str, project, timeout_ms, root_str)
            if result_text is not None and self._last_failure is not None:
                # A backend call failed somewhere inside this op, yet it still produced a body. That
                # body may therefore contain a count or an emptiness claim resting on data that was
                # never retrieved — `_op_impact` renders "## Callees of X (0)\n(none found)" when one
                # of its two independent queries times out. Say so once, here, rather than in each of
                # nine ops: this is the check whose ABSENCE let B1 reappear in this engine.
                miss = self._last_failure
                self._add_gap("backend", miss.kind, miss.describe())
                result_text = (
                    f"{result_text}\n\n> Incomplete: {miss.describe()}. Any count or "
                    f"\"none found\" above may reflect a query that did not return, not an "
                    f"absence in the code — re-ask before relying on it."
                )
            if result_text is None:
                # A supported op that matched nothing is NOT an unsupported op, and saying so sends
                # the agent looking for a different tool when the real answer is almost always a
                # stale index. Name the cause and the one command that fixes it.
                # The project id is NOT interpolated here. For a path-slug registration it IS the
                # flattened absolute path of the repo (`Users-alice-Documents-work-myrepo`), so
                # naming it leaks the server's directory layout to any caller — the same home-path
                # disclosure the renderers were swept for, through a channel that sweep did not
                # cover because it greps for `qualified_name`.
                if self._saw_unparsable:
                    # Never claim "not in the index" when we could not read the answer. This is
                    # the difference between a true statement about the repository and a false one
                    # caused by a wire-format change upstream.
                    return safe_null_result(
                        op_str, target_str, engine="graph", reason="backend-incompatible",
                        hint=_INCOMPATIBLE_HINT,
                    )
                # Sibling of the check at line ~1449 above, and the check lsp.py:333-342 already
                # made for the LSP engine: a backend call inside this op failed (timeout / crash /
                # error) rather than genuinely returning "no rows", and that failure collapsed to
                # the same bare `None` a real miss produces. Reported as `not-in-graph` before this
                # check existed — an agent reading "not in the graph index" about a query that never
                # returned would take a backend outage for a fact about the repository, which is the
                # exact misreading that makes "safe to delete" the wrong conclusion.
                if self._last_failure is not None:
                    miss = self._last_failure
                    return safe_null_result(
                        op_str, target_str, engine="graph", reason=miss.kind,
                        hint=f"{miss.describe()} — this is not a statement about your code: the "
                             f"query did not return. Re-ask, or run `codeintel doctor`.",
                    )
                # Before claiming the symbol is absent, ask whether it is merely unreferenced.
                # These are different facts and they license opposite actions.
                if op_str in self._EDGE_OPS_WITH_A_SYMBOL_TARGET:
                    where = self._node_locations(target_str, project, timeout_ms)
                    if where:
                        # …and say what DOES point at it. "No callers" plus silence reads as
                        # "unused"; "no callers, but 3 DECORATES and 2 TESTS" is an answer.
                        kinds = self._dependency_kinds(target_str, project, timeout_ms)
                        other = (" Other relationships DO point at it: "
                                 + ", ".join(f"{n} {k}" for k, n in
                                             sorted(kinds.items(), key=lambda kv: -kv[1])[:5])
                                 + " — query those before concluding anything about it."
                                 ) if kinds else ""
                        return safe_null_result(
                            op_str, target_str, engine="graph", reason="no-edges",
                            hint=f"`{target_str}` IS indexed ({'; '.join(where[:3])}) — it has no "
                                 f"{op_str} edge in the graph, which is not the same as being "
                                 f"absent. Framework-dispatched handlers (routes, ASGI apps) and "
                                 f"symbols passed as a value rather than called look exactly like "
                                 f"this, so do NOT read it as dead code. Re-indexing will not "
                                 f"change it; confirm with `--engine lsp` or `--op pattern`."
                                 + other,
                        )
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="not-in-graph",
                    hint=f"`{target_str}` is not in the graph index for this project — if "
                         f"you just added or renamed it, refresh with: codeintel index {root_str}",
                )

            # A symbol-scoped answer served from a containing project is usually right (a real
            # subdirectory of a monorepo) but the caller cannot tell that from the envelope, and an
            # agent will not read past the answer. Say it in the result text itself, which is the
            # only channel that reaches the model today.
            if resolution.is_ancestor:
                self._add_gap(
                    "scope", "ancestor-scope",
                    "this repository is not indexed on its own, so the answer comes from the "
                    "indexed project containing it and may include results from outside it",
                )
                result_text = (
                    f"{result_text}\n\n> Scope: `{root_str}` is not indexed on its own — this "
                    f"answer comes from the indexed project that contains it, so it may include "
                    f"callers or callees from outside this repository. Index it standalone with "
                    f"`codeintel index {root_str}` for an answer scoped to it."
                )

            # Settled HERE, and not in the renderer that produced the rows, because this is the
            # first point at which the answer is whole: `impact` has rendered both its halves, and
            # every gap the body discloses — including the `ancestor-scope` one added immediately
            # above — is in `_pending_gaps`. A `safe_for_destructive` computed any earlier answers
            # "is the list clean?" against a gap list that is not yet the answer's.
            #
            # Emitted only by the ops that produce rows, and only when they produced some — an
            # empty `rows` on a `search` answer would be a claim that the op has rows and found
            # none, which is a different sentence from "this op does not work that way".
            evidence = self._settle_evidence()
            if evidence is not None:
                # Above the heading, because "first screen" is the requirement: a reader who acts
                # on the heading never reaches a caveat printed below fifty rows.
                result_text = self._first_screen(evidence) + result_text
            envelope: Result = {
                "ok": True,
                "op": op_str,
                "target": target_str,
                "result": result_text,
                "engine": "graph",
                "cached": False,
            }
            if evidence is not None:
                envelope["rows"] = list(self._pending_rows)
                envelope["evidence"] = evidence
            return attach_confidence(envelope, self._pending_gaps)
        except Exception as exc:
            log_swallowed("GraphProvider.build_result", exc)
            return safe_null_result(op, target, engine="graph", reason="error")

    def _dispatch(
        self, op: str, target: str, project: str, timeout_ms: int, root: str = ""
    ) -> str | None:
        if op == "impact" or op == "context":
            # `context` (fan-out op) → the graph's richest single-symbol view: callers + callees.
            return self._op_impact(target, project, timeout_ms)
        if op == "callers":
            return self._op_callers(target, project, timeout_ms)
        if op == "callees":
            return self._op_callees(target, project, timeout_ms)
        if op == "chain":
            return self._op_chain(target, project, timeout_ms)
        if op == "pattern":
            return self._op_pattern(target, project, timeout_ms)
        if op == "overview":
            return self._op_overview(target, project, timeout_ms, root)
        if op == "changed" or op == "changes":
            return self._op_changed(project, timeout_ms)
        if op == "hotspots":
            return self._op_hotspots(project, timeout_ms)
        return None
