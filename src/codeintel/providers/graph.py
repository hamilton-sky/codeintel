from __future__ import annotations

import difflib
import os
import re

# `shutil`/`subprocess` are no longer called from this module — `BackendClient` (graph_backend.py)
# owns the transport now — but many tests monkeypatch `codeintel.providers.graph.shutil.which` /
# `.subprocess.run` by dotted string path, which pytest resolves by walking attributes off THIS
# module. Since `shutil`/`subprocess` are process-wide singletons, patching either through this
# module's reference patches the same object `graph_backend.py` calls through, so the imports stay
# here purely as a resolution anchor for those tests.
import shutil  # noqa: F401
import subprocess  # noqa: F401
import threading
from dataclasses import dataclass
from typing import Any

from codeintel.graph_backend import BackendClient, _parse_query_rows, _parse_search_results
from codeintel.graph_render import _is_module_scope_node, _is_non_code, _lang_family
from codeintel.graph_resolution import (
    _RESOLVE_TIMEOUT_MS,
    ProjectLookup,
    ProjectResolution,
    ProjectResolver,
)
from codeintel.outcome import Missing
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result
from codeintel.source_kind import is_code_path, looks_generated_path


def _cypher_literal(s: Any) -> str:
    """Escape a value for a double-quoted Cypher string literal — defense against a
    ``target`` containing quotes/backslashes (e.g. content an agent echoed from a repo)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


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


# Trailing segments that mean "this is a filename, not a dotted module path".
_FILE_EXTENSIONS = frozenset({
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "rb", "php",
    "cs", "swift", "scala", "c", "h", "cpp", "cc", "hpp", "css", "scss", "less", "html", "vue",
    "svelte", "md", "mdx", "rst", "txt", "json", "yaml", "yml", "toml", "ini", "cfg", "xml",
    "sql", "sh", "bash", "zsh", "graphql", "proto",
})


def _strip_project_prefix(qualified_name: str, *, may_be_filename: bool = True) -> str:
    """Drop the backend's project id from the head of a qualified name.

    The backend prefixes every qualified name with its own project id, which for a path-slug
    registration is the flattened absolute path — so each result line began
    `Users-alice-Documents-project-myrepo.src.pkg.fn`. That is the author's home directory
    layout repeated on every row: noise for a human, wasted tokens for the agent this tool
    exists to serve, on results that can run to a hundred lines.

    Only a leading path-slug-looking segment is removed. A qualified name that starts with a
    real module (`src.codeintel.gateway.query`) is left exactly as it is.
    """
    head, sep, rest = qualified_name.partition(".")
    if not sep:
        return qualified_name
    # "hyphen ⇒ project slug" was wrong: kebab-case FILENAMES are the dominant TS/JS convention,
    # so `use-toast.ts` became `ts` and `1731900000000-CreateInitialTables.ts` became `ts` too —
    # 985 names in one real repo, 811 in another. The `changed` op takes exactly this path,
    # because its rows carry `name` and no `qualified_name`, so its symbols rendered as `ts`.
    #
    # A project slug is the head of a dotted path. What follows it does NOT have to be dotted:
    # requiring that broke every flat-namespace language. Go qualified names are `project.FuncName`
    # — one segment, no package path — so on a Go repository this stripped nothing and every row
    # rendered the full flattened absolute path, username included, straight into `result`. Found
    # by evaluating against a third language; two languages could not have shown it, because both
    # of them happen to produce dotted remainders.
    # The space test covers the WHOLE name, not just the head. Dropping the dotted-remainder rule
    # cost this: `EC-1.1: Empty workflow plan` is a document heading whose head happens to be
    # hyphenated, and it was previously spared only because its remainder had no dot. A qualified
    # name never contains a space, in any language, so that is the durable discriminator.
    if "-" not in head or " " in qualified_name or not rest:
        return qualified_name
    # `my-component.spec.ts` also has a dotted remainder, so "has dots" is not enough. What
    # separates a filename from a qualified name is the LAST segment: a module path ends in a
    # symbol, a filename ends in an extension.
    #
    # Except that a symbol is allowed to BE that word. `requests.models.Response.json` is a method —
    # the most-called method in that library — and this guard read it as a `.json` file and returned
    # the name unstripped, leaking `private-tmp-codeintel-corpus-requests` (in normal use, the
    # user's home directory) into a rendered hotspots row. `my-component.spec.ts` and
    # `Response.json` are the same shape; no rule on the string can separate them.
    #
    # What separates them is which FIELD the value came from. Filenames arrive in a row's `name` —
    # that is why this guard exists, for `changed` rows, which carry `name` and no
    # `qualified_name` — while a `qualified_name` is a module path whose last segment is a symbol.
    # So the guard is the caller's to claim, defaulting to on so that an unexamined call site keeps
    # today's behaviour.
    if may_be_filename and qualified_name.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS:
        return qualified_name
    # `use-toast.ts` is now only distinguishable from `my-repo.Execute` by that extension check, so
    # a hyphenated head with a single non-extension segment after it is treated as a slug. That is
    # the correct call: a kebab-case FILE whose extension we do not know is rare, while a flat
    # qualified name is the norm for Go, Java, C# and Ruby.
    return rest


# Directory names that are not hand-written source: vendored trees and build output. Retained after
# `deadcode` was retired because `_is_archived_path` — and through it `_is_noise`, which `hotspots`
# and `changed` both use — still classifies paths with it.
_VERIFY_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "dist", "build", "out", "target", "vendor", "vendored",
    "third_party", "thirdparty", "venv", "env", "site-packages", "coverage", "generated",
})
_ARCHIVE_DIRS = frozenset({
    ".archive", ".archived", ".backup", ".backups", ".bak", ".old", ".deprecated", ".trash",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".gradle", ".terraform",
})


def _is_archived_path(file_path: str) -> bool:
    """Whether *file_path* lives under a retired, vendored or generated directory.

    A repo-scan op ranks by complexity and fan-in, and archived code scores well on both — an
    8MB `.archive/` tree put a retired 507-line component third in a repo's refactor hotspots, a
    near-duplicate of the live one. Pointing an agent at dead code as the thing most worth
    refactoring is worse than returning nothing.

    Generated output is the same problem and worse: a checked-in minified bundle took the top TWO
    hotspot slots on a real repo with cx:586 / cog:1145, because a webpack chunk is by far the
    most "complex" function in any tree that contains one. The first version excluded only
    dot-directories, so a plain `out/`, `dist/` or `vendor/` sailed through. Shares the skip list
    with the source verifier — the definition of "not hand-written source" is one thing, not two.

    The name lists below are kept as a fast local pre-filter, but they are no longer the whole
    answer: `looks_generated_path` also recognises Bazel's `bazel-*` trees, `_generated`, `.output`,
    `Pods`, `bower_components` and generated FILENAMES that sit beside real source (`*.min.js`,
    `*_pb2.py`, `*.g.dart`), none of which any name list here covered. Every entry in those lists
    was added after a real repository produced a wrong answer; recognising the shape rather than
    the specific name is what stops the next one from doing it again.
    """
    parts = [p.lower() for p in file_path.replace("\\", "/").split("/")[:-1]]
    if any(p in _ARCHIVE_DIRS or p in _VERIFY_SKIP_DIRS for p in parts):
        return True
    return looks_generated_path(file_path)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _collapse_repeats(label: str) -> str:
    """`A.EditorHeader.EditorHeader.EditorHeader` -> `A.EditorHeader`.

    The backend emits a segment per nesting level, so a component in a file of the same name in a
    directory of the same name repeats three times — and the file path is printed right beside it
    anyway. Across 200 rows that is real token cost for the agent this output exists to serve."""
    out: list[str] = []
    for seg in label.split("."):
        # Only collapse identifiers. Splitting on "." also splits version numbers and dotted
        # quads, where consecutive equal parts are meaningful: `CHANGELOG.1.1.0` became
        # `CHANGELOG.1.0` (a different real release) and `127.0.0.1` became `127.0.1`.
        if out and out[-1] == seg and _IDENTIFIER_RE.match(seg):
            continue
        out.append(seg)
    return ".".join(out)


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


def _language_coverage_note(rows: list[dict]) -> str:
    """Warn when a ranking is dominated by one file type.

    A "where is my complexity?" answer that is 100% `.tsx` on a repo that is two-thirds Python is
    not a ranking, it is a coverage failure — and it reads identically to a real result. Both repos
    in the 2026-08-17 evaluation returned exactly that, and nothing in the output said so. This
    cannot decide whether the cause is a metric the extractor does not compute for a language or a
    genuine concentration of complexity, so it states the observation and lets the reader judge."""
    exts: dict[str, int] = {}
    for r in rows:
        path = str(r.get("file_path") or "")
        ext = os.path.splitext(path)[1].lower()
        if ext:
            exts[ext] = exts.get(ext, 0) + 1
    total = sum(exts.values())
    # Too few rows to say anything. A genuinely single-language ranking is NOT excluded here — it
    # is exactly the case worth reporting, and test_a_single_language_ranking_says_so pins that.
    if total < 5 or not exts:
        return ""
    top_ext, top_n = max(exts.items(), key=lambda kv: kv[1])
    if top_n / total < 0.9:
        return ""
    return (
        f"\n\n_Coverage: {top_n} of the top {total} ranked symbols are `{top_ext}` files. If this "
        f"repository has substantial code in other languages, they are absent from this ranking "
        f"rather than less complex — treat it as covering `{top_ext}` only._"
    )


# The row cap on the two symbol-edge queries. Named rather than inlined because the RENDERER needs
# to know it: an answer that came back exactly AT the cap was truncated by us, and a truncated
# callee list that reads as complete is the same defect class as a filtered one that reads as
# complete. `callees` feeds "is this safe to change?", where "unknown" and "none" are opposite
# answers.
_EDGE_ROW_LIMIT = 50

# How much the backend trusts an edge's target resolution — and the line below which it is a GUESS.
#
# The graph backend resolves each call target through a prioritised cascade and stamps the edge with
# a confidence: 0.95 when it followed the file's import map, 0.90 same-module, 0.85 import-suffix —
# and then 0.75 for "the only symbol in the whole repository carrying this bare name", 0.55 for a
# suffix match among several candidates, and 0.30-0.40 for raw string similarity. Only the first
# three consult the imports of the file the call is written in. Everything below them is name
# matching, and name matching is exactly how a call to a framework global (`describe` from vitest,
# `dict.get`) or to a local callback (`onClose`, `setScope`) acquires an edge to whichever project
# symbol happens to share its name.
#
# These are not rare. Measured over the CALLS edges of three real repositories: 24%, 33% and 43% of
# every edge sat below this floor. codeintel selected these rows and then dropped the confidence
# column on the floor, so a fabricated caller rendered identically to a real one and the envelope
# still said `confidence: "complete"` — the one combination the safe-null contract exists to make
# impossible.
#
# The rows are KEPT, not filtered: dropping a 0.75 row would trade a false positive for a false
# negative, and "no callers" is the more dangerous of the two when the next action is a delete.
# They are labelled in the body, counted in a note, and raised as a gap so the envelope goes
# `partial`.
_EDGE_CONFIDENCE_FLOOR = 0.85
# Below the floor the cascade stops consulting imports, but it does not become uniformly wrong, and
# a check that treats it as such is its own precision bug. Two tiers, because they fail differently:
#
#   0.55 < c < 0.85  — `unique_name`. The call resolved here because this is the ONLY symbol in the
#                      index carrying that bare name. That is right whenever the call really does
#                      target a project symbol (measured by hand: `runAlerts -> evaluate` and
#                      `buildSnapshot -> usageDayFor` are both genuine, and both stamped 0.75), and
#                      wrong whenever it targets a same-named symbol the index never saw. Suspicion,
#                      not a verdict.
#   c <= 0.55        — suffix match among several candidates, or raw string similarity. These are
#                      the rows that put an archived UI component in one tree "calling" a hook in
#                      another because both mention `setScope`.
_EDGE_CONFIDENCE_WEAK = 0.55

# How many same-named candidates to name when the answer has to ask "which one?". A list long enough
# to be unreadable is not a choice offered, and the count always states the full total.
_CANDIDATE_CAP = 12


@dataclass(frozen=True)
class _SymbolTarget:
    """A ``target`` that may say WHICH symbol it means.

    The symbol-edge ops key on the unqualified name, so a repository with three functions called
    `handle` answers for all three at once and the caller has no way to ask for one. The graph
    already tells them apart — every row carries `qualified_name` and `file_path` — and both are
    printed on the result lines the caller has just read, so the disambiguator is text they already
    hold:

    * ``handle``                     every symbol named `handle`
    * ``api.routes.handle``          the one whose qualified name ends in those segments
    * ``handle@src/api/routes.py``   the one defined in that file

    Applied to rows rather than pushed into the Cypher ``WHERE``: a suffix match needs a string
    predicate, and which of those this backend supports is not something this project can pin — the
    0.9→0.10 wire-format break is the standing reminder that its dialect is not a stable interface.
    Narrowing rows already in hand costs one pass and cannot be broken by a backend release.
    """

    name: str
    qualified: str = ""
    file_hint: str = ""

    @property
    def narrowed(self) -> bool:
        """Whether the caller asked for one specific symbol rather than every symbol by that name."""
        return bool(self.qualified or self.file_hint)

    def describe(self) -> str:
        parts = []
        if self.qualified:
            parts.append(f"qualified name `{self.qualified}`")
        if self.file_hint:
            parts.append(f"file `{self.file_hint}`")
        return " in ".join(parts) or f"`{self.name}`"

    def matches(self, qualified_name: Any, file_path: Any) -> bool:
        """Whether the symbol at *qualified_name* / *file_path* is the one this target names."""
        if self.qualified and not _qualified_name_matches(qualified_name, self.qualified):
            return False
        return not (self.file_hint and not _file_path_matches(file_path, self.file_hint))


def _parse_symbol_target(target: Any) -> _SymbolTarget:
    """Split a ``target`` into a symbol name and whatever disambiguator it carries."""
    raw = str(target or "").strip()
    file_hint = ""
    if "@" in raw:
        head, _, tail = raw.rpartition("@")
        # A leading `@` is a decorator (`@app.route`), not a file hint, and `handle@` names no file.
        if head.strip() and tail.strip():
            raw, file_hint = head.strip(), tail.strip()
    qualified = ""
    if "." in raw:
        head, _, last = raw.rpartition(".")
        # `use-toast.ts` is a FILENAME, not a qualified name. Reading `ts` as the symbol name would
        # send a query for something nobody asked about — the same trap `_strip_project_prefix`
        # guards against, so the same derived extension set answers it.
        if head and last and last.lower() not in _FILE_EXTENSIONS:
            qualified, raw = raw, last
    return _SymbolTarget(name=raw, qualified=qualified, file_hint=file_hint)


def _qualified_name_matches(qualified_name: Any, wanted: str) -> bool:
    """Whether *qualified_name* ends with the dotted segments *wanted*.

    Segment-aligned, so `routes.handle` does not match `api.myroutes.handle`. Compared against both
    the raw name and its `_strip_project_prefix` form, because the caller will have copied the
    stripped one off a previous result line while the backend still stores the prefixed one."""
    raw = str(qualified_name or "")
    if not raw or not wanted:
        return False
    return any(have == wanted or have.endswith("." + wanted)
               for have in (raw, _strip_project_prefix(raw, may_be_filename=False)))


def _file_path_matches(file_path: Any, hint: str) -> bool:
    """Whether *file_path* is the file the caller named.

    A path-segment suffix match: `routes.py`, `api/routes.py` and the full repo-relative path all
    identify `src/api/routes.py` — all three are things a caller reasonably types, and the last is
    what the result lines print. A hint that stays ambiguous is not a problem to be solved here: two
    files can match, and the answer then says so rather than picking one silently."""
    have = str(file_path or "").replace("\\", "/").strip().lower()
    want = str(hint or "").replace("\\", "/").strip().strip("/").lower()
    if not have or not want:
        return False
    return have == want or have.endswith("/" + want)


@dataclass
class _EdgeGroup:
    """The rows belonging to ONE symbol, held apart from every other symbol sharing its bare name.

    Grouping is what stops a `callees` answer from being the union of several questions with no way
    to tell which row came from where. It also makes the per-row language check structural rather
    than remembered: a row can only ever be compared against its own group's caller, so the
    caller-family UNION bug that `0.15.5` fixed by hand cannot be written again here."""

    label: str
    qn_raw: str
    file: str
    rows: list[dict]

    def describe(self) -> str:
        if self.label and self.file:
            return f"`{self.label}` ({self.file})"
        if self.label:
            return f"`{self.label}`"
        return self.file or "(a symbol the index does not name)"


def _edge_confidence(row: dict) -> float | None:
    """How much the backend vouches for THIS edge's target resolution, or None if it never said.

    An unstamped edge is not a confident one: older index generations wrote no confidence at all,
    and on one evaluated repository 409 of 8,969 CALLS edges came back blank. Returning None rather
    than a default keeps "the backend did not say" distinguishable from "the backend said 0.95",
    which is the same distinction the envelope's `confidence` field exists to preserve one level up.
    """
    raw = row.get("c.confidence")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _confidence_badge(row: dict) -> str:
    """The per-row mark for an edge the backend did not resolve through an import.

    Two glyphs rather than one because the tiers mean different things and a reader scanning a list
    should be able to tell "unverified" from "probably junk" without consulting the note: `?` is a
    unique-name binding, `!` a suffix or string-similarity one."""
    conf = row.get("_low_confidence")
    if conf is None:
        return ""
    conf = float(conf)
    glyph = "!" if conf <= _EDGE_CONFIDENCE_WEAK else "?"
    return f" [{glyph}{conf:.2f}]"


def _group_edges(rows: list[dict], name_key: str, qn_key: str, file_key: str) -> list[_EdgeGroup]:
    """Partition *rows* by the distinct symbol they belong to, preserving the backend's row order.

    Keyed on the qualified name AND the file: one file legitimately holds two symbols with the same
    bare name (a method on two classes), and a row carrying neither still has to land somewhere
    rather than being dropped."""
    groups: dict[tuple[str, str], _EdgeGroup] = {}
    for r in rows:
        file = str(r.get(file_key) or "")
        qn_raw = str(r.get(qn_key) or "")
        label = _strip_project_prefix(qn_raw, may_be_filename=False) or str(r.get(name_key) or "")
        key = (label, file)
        if key not in groups:
            groups[key] = _EdgeGroup(label=label, qn_raw=qn_raw, file=file, rows=[])
        groups[key].rows.append(r)
    return list(groups.values())


def _same_path(a: str | None, b: str | None) -> bool:
    """Whether two paths denote the same directory, after realpath.

    Needed because a registry can hold `/tmp/x` while the caller asks about `/private/tmp/x` (macOS
    symlinks every `/tmp`), and a string comparison there reports a mismatch that does not exist."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except Exception:
        return str(a) == str(b)


def _has_own_git_dir(path: str) -> bool:
    """Whether *path* is the root of its own git repository.

    This is what separates the two cases an ancestor match conflates. A subdirectory of a monorepo
    has no `.git` of its own, and answering it from the monorepo's index is correct. A repository
    that merely happens to sit inside an indexed directory does have one, and answering it from the
    parent is the bug. A worktree or submodule records `.git` as a FILE rather than a directory, so
    test for existence, not `is_dir()`."""
    try:
        return os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False


def _label_of(row: dict) -> str:
    """A row's display label: its qualified name with the backend's project id removed.

    Every place that renders a qualified name must go through here or `_display`. Fixing them one
    at a time did not work — `_display` was fixed first, `_render_scan` was missed and shipped,
    and after a test was added asserting "both renderers strip the prefix", `chain` and `pattern`
    turned out to be a third and fourth. The test now enumerates the module rather than a list of
    functions someone remembered to write down."""
    qualified = str(row.get("qualified_name") or "")
    if qualified:
        return _strip_project_prefix(qualified, may_be_filename=False)
    return _strip_project_prefix(str(row.get("name") or "?"))


def _repo_display_name(root: str) -> str:
    """The repo's own directory name, for headings a human will read.

    Resolves first, because callers routinely pass "." (`codeintel map .`) — the basename of which
    is "." and would title the committed map file with a dot."""
    if not root:
        return ""
    try:
        return os.path.basename(os.path.realpath(root).rstrip(os.sep))
    except Exception:
        return ""


class GraphProvider:
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

    def probe(self, project_root: str, timeout_ms: int = _RESOLVE_TIMEOUT_MS) -> dict:
        """Cheap, never-raise, single-subprocess health check for the doctor.

        Returns ``{installed, runnable, repo_indexed, project, detail, remediation}`` — one
        ``list_projects`` call, bounded by ``timeout_ms`` (``_run`` returns None on timeout)."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": False, "project": None,
                "detail": "codebase-memory-mcp not found on PATH",
                "remediation": "put the codebase-memory-mcp binary on PATH — it's an external "
                               "native backend (see docs/graph.md); once present it self-updates "
                               "via `codebase-memory-mcp update`",
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
            return {
                "installed": True, "runnable": True, "repo_indexed": True,
                "project": resolution.name,
                "detail": (f"this repo is NOT indexed on its own — answers would come from "
                           f"'{resolution.name}' ({resolution.matched_root}), which contains it; "
                           f"{scoped}"
                           + (" (this directory is its own git repository, so it is a nested repo "
                              "rather than a subdirectory of that project)" if own_repo else "")),
                "remediation": f"codeintel index {project_root}",
            }
        return {
            "installed": True, "runnable": True, "repo_indexed": True, "project": resolution.name,
            "detail": f"resolved project '{resolution.name}' in codebase-memory-mcp",
            "remediation": None,
        }

    # The root the ANSWERING project is registered under, recorded per query so a renderer can
    # check what it is about to attribute. Class-level default for the same __new__ reason.
    _answered_root: str | None = None
    # Parts of this answer known to be short of an answer. Graph has two real cases: a symbol-scoped
    # answer served from a CONTAINING project, and callee rows dropped as name collisions.
    _pending_gaps: tuple[dict[str, Any], ...] = ()

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

    @staticmethod
    def _display(row: dict, name_key: str, qn_key: str, file_key: str) -> str:
        # A module-scope container row (marked by `_collapse_module_scope`) renders as the LOCATION
        # it is, never as the synthetic `__file__`/module symbol the backend attached the edge to.
        # The edge is real — code at that file's module scope references the symbol — so it stays,
        # but calling it `src.click.core.__file__` asserts a caller/callee that does not exist. No
        # edge badge: after the File/Module double-representation is collapsed the CALLS-vs-USAGE
        # distinction is a backend artifact of which node kind carried the edge, not a fact about the
        # code.
        scope = row.get("_module_scope")
        if scope is not None:
            where = str(scope) or str(row.get(file_key) or "")
            # The name-match badge has to be reachable from this branch too. On the repository that
            # motivated the floor, 31 of the 32 fabricated rows were module-scope ones, so a badge
            # applied only to the named-symbol branch below would have marked exactly one of them
            # and left the summary count looking unsupported by the list it summarises.
            mark = _confidence_badge(row)
            return f"- module scope of {where}{mark}" if where else f"- module scope{mark}"
        name = str(row.get(name_key) or "?")
        qn = _strip_project_prefix(str(row.get(qn_key) or ""), may_be_filename=False)
        file = str(row.get(file_key) or "")
        edge = str(row.get("type(c)") or "").strip()
        label = qn or name
        tail = f" ({file})" if file and file != qn else ""
        badge = f" [{edge}]" if edge else ""
        # A row the backend resolved by bare name rather than by import carries its score into the
        # line itself. The summary note says how many there are; only the badge says WHICH, and a
        # reader scanning for "is my symbol in here" reads rows, not notes.
        badge += _confidence_badge(row)
        return f"- {label}{badge}{tail}"

    @staticmethod
    def _collapse_module_scope(
        groups: list[_EdgeGroup], label_key: str, file_key: str
    ) -> None:
        """Relabel the DISPLAYED-side module-scope rows in each group, in place.

        Both edge ops render one end of the edge as their rows — the caller for `callers`, the callee
        for `callees` — so the pseudo-node filter lives here, off the displayed side's label/file
        keys, and both ops reach it the same way. A row whose displayed node is a whole-file container
        (`_is_module_scope_node`) is not a symbol; the backend has simply no node for module- or
        class-body-scope code and hangs the edge off the file. Three choices were weighed on what the
        backend actually emits:

        * DROP the row. Rejected: it under-counts, and worse it manufactures false absences. The edge
          is real — `src.click.core.__file__` references `builtins.len` and an exception class from
          another file, which is module-scope code, not a containment artifact — and on the pinned
          corpus 147 symbols are referenced ONLY from module scope, so dropping would report a live,
          referenced function as having zero callers: the "safe to delete" misread this project keeps
          re-learning to avoid.
        * RELABEL it as the location it is. Chosen: the edge stays, the count stays honest, and the
          row stops asserting a caller/callee that does not exist.
        * Something else — leave it. Rejected: the row reads as a real symbol a reader will try to
          open.

        A single file can carry BOTH a `File` (`__file__`) and a `Module` representation of the same
        scope (38 target/file pairs do on the corpus), so collapse them to one row per file — two
        rows both reading "module scope of core.py" is the double-count relabelling would otherwise
        introduce. Real callable rows are never touched, so a symbol called from both a function and
        module scope keeps both.
        """
        for group in groups:
            seen_files: set[str] = set()
            kept: list[dict] = []
            for row in group.rows:
                if not _is_module_scope_node(row.get(label_key)):
                    kept.append(row)
                    continue
                fp = str(row.get(file_key) or "")
                if fp in seen_files:
                    continue                      # File+Module double-representation of one file
                seen_files.add(fp)
                marked = dict(row)                # don't mutate the shared backend row
                marked["_module_scope"] = fp
                kept.append(marked)
            group.rows = kept

    @staticmethod
    def _drop_edge_collisions(groups: list[_EdgeGroup], file_key: str, label_key: str) -> int:
        """Drop the displayed-side rows that are name collisions rather than real edges, in place.

        The extractor emits an edge for a bare local name, so a symbol whose body says `conn`,
        `tmp_path` or `write` acquires an edge to anything else in the repository carrying that name
        -- including a function in another language, or a node in a data file. A call edge cannot
        cross a language family without an FFI/IPC mechanism the extractor does not emit
        (`_LANG_FAMILIES`), and a `.json`/`.md` file defines no callable at all, so a displayed
        endpoint in a different family than the OTHER end of the edge, or in a non-code file, is a
        collision and dropping it costs nothing real.

        This began as `callees`-only. A Python function's CALLERS are polluted the same way -- a `.ts`
        function three files over that shares the bare name -- so both ops now share the one filter.
        `anchor` is the family of the far end (the group's own key symbol: the caller for `callees`,
        the called symbol for `callers`), so the comparison is per-group and cannot be confused by an
        unrelated same-named symbol elsewhere in the result.

        Module-scope nodes are left untouched, for `_collapse_module_scope` to relabel. The backend
        mis-attributes their file path -- it labelled `examples/aliases/aliases.py`'s module scope
        `aliases.ini`, a sibling file -- so a non-code or cross-language path on one is its own
        artifact, NOT evidence the reference is spurious; the seven click-API references behind that
        one node are genuine. Returns the number dropped, which the caller must disclose."""
        dropped = 0
        for group in groups:
            anchor_fam = _lang_family(group.file)
            keep: list[dict] = []
            for r in group.rows:
                if _is_module_scope_node(r.get(label_key)):
                    keep.append(r)              # a location, relabelled later; never a collision
                    continue
                path = str(r.get(file_key) or "")
                if _is_non_code(path):
                    dropped += 1                # a data/doc file defines no callable
                    continue
                fam = _lang_family(path)
                if fam and anchor_fam and fam != anchor_fam:
                    dropped += 1                # a cross-language collision, against THIS group's key
                    continue
                keep.append(r)
            group.rows = keep
        return dropped

    # Same reasoning as `_query_rows` above: fetch via `self._run`, parse via the shared helper.
    def _search_symbols(self, extra: dict, project: str, timeout_ms: int) -> list[dict] | None:
        raw = self._run("search_graph", {"project": project, **extra}, timeout_ms)
        return _parse_search_results(raw)

    @staticmethod
    def _looks_like_test(fp: str, name: str) -> bool:
        """Heuristic test detection. The backend's own ``is_test`` flag comes back False for pytest
        functions (verified by dogfooding), so dead-code / hotspot scans must filter by path+name
        or drown in test noise — this is the single most load-bearing renderer detail."""
        f = (fp or "").lower()
        if f.startswith(("tests/", "test/")) or "/tests/" in f or "/test/" in f:
            return True
        base = f.rsplit("/", 1)[-1]
        return (base.startswith("test_") or base.endswith("_test.py")
                or base == "conftest.py" or (name or "").startswith("test_"))

    @staticmethod
    def _is_synthetic(fp: str) -> bool:
        """Builtins / generated nodes carry an empty or ``<...>`` file_path (e.g. <python-builtins>)."""
        return (not fp) or fp.startswith("<")

    @staticmethod
    def _is_data_file(fp: str) -> bool:
        """Whether *fp* is a pure-data serialization format (JSON) that a permissive parser can
        still emit "symbol"-shaped nodes for — a JSON object's top-level keys, walked as if they
        were definitions — but that never defines a CALLABLE in any language. On this repo's own
        committed CODE_INTEL.md, two untracked JSON blobs (`pathly/project/SPEC.md.comments.json`,
        `...diagrams.json`) ranked as the 2nd and 8th most load-bearing symbols in the project,
        because their top-level keys (`body`, `status`) were indexed as `Variable` nodes with real
        USAGE edges from sibling JSON files reusing the same key names.

        Deliberately a DENYLIST of one unambiguous data extension, not an allowlist of code
        extensions the way `source_kind.is_code_path` is: `_is_noise` is shared by every language
        the graph backend indexes, so allowlisting would silently hide a real symbol in any
        language not on that list — the exact over-filtering failure `deadcode` was retired for
        (see `_WITHDRAWN_OPS` above). `.json` can never contain a callable in any language, so
        excluding it by extension carries none of that risk.

        Scoped to `.json` alone, not its sibling data formats (`.yaml`/`.toml`/...), because that
        is the one measured on this repo — `test_scan_ops_hide_archived_code` pins that a `.yml`
        under `.github/workflows/` is NOT noise (a live workflow, not data-format junk), and a
        broader denylist would need its own evidence before touching that boundary."""
        return fp.rsplit(".", 1)[-1].lower() == "json" if "." in fp else False

    @classmethod
    def _is_noise(cls, r: dict) -> bool:
        """Rows a code-quality scan should hide: builtins/generated nodes, test code (the backend's
        own ``is_test`` is unreliable — see ``_looks_like_test``), and pure-data files a permissive
        parser mistook for symbols (see ``_is_data_file``). Shared by the repo-scan ops.

        Does NOT filter by symbol NAME, on purpose — not even a name shaped like a builtin/stdlib
        method (`get`, `keys`, ...). A real project method named `get` (e.g.
        `ContentHashCache.get` in this very repo) must survive here; a wrong-but-plausible-looking
        fan-in count on a REAL symbol is a backend call-resolution precision problem (bare-name
        matching with no type inference), not a noise-filtering one, and blacklisting the name
        would hide the real symbol along with the noise — the same over-aggressive-filter failure
        that retired `deadcode` at 25% precision. See
        `tests/test_graph_provider.py::test_is_noise_does_not_filter_a_real_symbol_named_get`."""
        fp = str(r.get("file_path") or "")
        return (cls._is_synthetic(fp)
                or cls._looks_like_test(fp, str(r.get("name") or ""))
                or _is_archived_path(fp)
                or cls._is_data_file(fp))

    def _render_scan(self, kept: list[dict], title: str, cap: int, meta_fn) -> str:
        """Render a repo-scan op's markdown from filtered+sorted rows: ``## title (count)`` + one
        ``- label (file)  [meta]`` line per row (top ``cap``) + a ``+N more`` note when truncated.
        ``meta_fn(row) -> list[str]`` supplies the per-op metric badge, so the repo-scan ops share
        the row format and truncation note (the drift-prone parts) and differ only in their metrics."""
        lines = []
        for r in kept[:cap]:
            qualified = str(r.get("qualified_name") or "")
            label = _collapse_repeats(
                _strip_project_prefix(qualified, may_be_filename=False) if qualified
                else _strip_project_prefix(str(r.get("name") or "?")))
            fp = str(r.get("file_path") or "")
            meta = meta_fn(r)
            badge = f"  [{', '.join(meta)}]" if meta else ""
            tail = f"  ({fp})" if fp else ""
            lines.append(f"- {label}{tail}{badge}")
        body = "\n".join(lines)
        if len(kept) > cap:
            body += f"\n… (+{len(kept) - cap} more)"
        return f"## {title} ({len(kept)})\n" + body

    # ------------------------------------------------------------------ ops

    def _op_callers(self, target: str, project: str, timeout_ms: int) -> str | None:
        """What calls or uses *target*.

        Honours the same disambiguator `callees` does (`_SymbolTarget`), applied to the far end of
        the edge — the symbol being called. Without it, `impact` would narrow one of its two halves
        and not the other, and a blast-radius answer whose callers belong to a DIFFERENT symbol of
        the same name is worse than an un-narrowed one: it reads as precise."""
        wanted = _parse_symbol_target(target)
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE b.name="{_cypher_literal(wanted.name)}" '
            "RETURN a.name, a.qualified_name, a.file_path, labels(a), type(c), c.confidence, "
            f"b.name, b.qualified_name, b.file_path LIMIT {_EDGE_ROW_LIMIT}"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        truncated = len(rows) >= _EDGE_ROW_LIMIT

        called = _group_edges(rows, "b.name", "b.qualified_name", "b.file_path")
        selected = [g for g in called if wanted.matches(g.qn_raw, g.file)]
        if wanted.narrowed and not selected:
            return self._no_symbol_matched_the_hint("callers", target, wanted, called)

        # The caller is the displayed side here, so both the collision pollution and the module-scope
        # pseudo-nodes land in the rows a reader sees. Drop the cross-language / non-code collisions
        # (a `.ts` function three files over sharing the bare name is not a caller), relabel the
        # module-scope nodes that remain, and disclose anything dropped — all before the renderer
        # counts or prints a row.
        dropped = self._drop_edge_collisions(selected, "a.file_path", "labels(a)")
        self._collapse_module_scope(selected, "labels(a)", "a.file_path")
        notes = self._confidence_note("callers", selected) + self._collision_note("callers", dropped)
        if truncated:
            notes = self._row_cap_note("callers", target) + notes
        if not any(g.rows for g in selected):
            return self._empty_edge_answer(
                "callers", "caller", target, wanted, selected, notes, truncated, dropped)
        return self._render_edge_answer(
            "callers", "caller", target, wanted, selected,
            ("a.name", "a.qualified_name", "a.file_path"), truncated, notes)

    def _no_symbol_matched_the_hint(
        self, op: str, target: str, wanted: _SymbolTarget, candidates: list[_EdgeGroup]
    ) -> str:
        """The caller named a specific symbol, and no symbol matching it has edges of this kind.

        Two wrong answers to avoid. Falling back to every symbol with that bare name answers a
        question the caller explicitly narrowed away from. Reporting zero rows claims the symbol has
        none, which is a statement about the code rather than about the lookup. So: say the hint
        matched nothing, and name the symbols that DO carry the name — the information needed to ask
        again correctly, and already in hand.

        Careful about what is claimed: the population here is the symbols this op's own query
        returned, NOT the index. A symbol can be perfectly well indexed and still be absent from
        this list — `Group.invoke` has callees but no callers on one real repository — so saying
        "not in this index" would be a second false claim in a message written to avoid the first."""
        self._add_gap(
            op, "target-hint-unmatched",
            f"no symbol matching {wanted.describe()} has {op} here; {len(candidates)} other "
            f"symbol(s) named `{wanted.name}` do, so this is not evidence that `{target}` has none",
        )
        listing = "\n".join(f"- {g.describe()}" for g in candidates[:_CANDIDATE_CAP])
        more = (f"\n… (+{len(candidates) - _CANDIDATE_CAP} more)"
                if len(candidates) > _CANDIDATE_CAP else "")
        return (f"## {op.capitalize()} of {target}\n"
                f"**No symbol matching {wanted.describe()} has {op} in this index** — which says "
                f"nothing about whether that symbol exists or what it calls; it may simply have no "
                f"edge of this kind. {len(candidates)} symbol(s) named `{wanted.name}` do have "
                f"{op} here:\n" + listing + more)

    def _row_cap_note(self, op: str, target: str) -> str:
        """Disclose a list this op truncated itself.

        A query that came back exactly at its own `LIMIT` has almost certainly been cut short, and
        the rendered list gives no sign of it. For `callees` in particular the whole point is
        "everything this reaches", so a silently-capped list is the partial-reads-as-complete
        failure in its purest form."""
        self._add_gap(
            op, "row-cap-reached",
            f"the query returned the maximum {_EDGE_ROW_LIMIT} rows, so this list is truncated "
            f"and may be missing rows — not a complete answer for `{target}`",
        )
        return (f"\n\n_Truncated: the graph returned the maximum {_EDGE_ROW_LIMIT} rows, so rows "
                f"beyond that are missing from this list._")

    def _confidence_note(self, op: str, groups: list[_EdgeGroup]) -> str:
        """Mark every row the backend resolved by NAME rather than by import, and disclose the count.

        This is the fix for the failure that motivated the floor. Asking `callers describe` on a
        TypeScript repo returned 32 rows — every one a call to vitest's global `describe`, imported
        from "vitest" in the very file it appears in — all bound to the project's own
        `domain.budget.describe` because that was the only indexed symbol with the name. The backend
        had stamped 31 of them 0.75 and one 0.38; codeintel dropped the column and rendered all 32
        as plain callers under `confidence: "complete"`, while the one REAL caller (reached through
        an aliased import) was missing entirely.

        What this does NOT do is call every sub-floor row wrong. `runAlerts -> evaluate` is a real,
        hand-verified caller and the backend stamped it 0.75, so a note reading "these may not be
        callers at all" over a two-row answer would replace a false positive with a false alarm —
        the same defect facing the other way. The two tiers are reported as what they are: a
        `unique_name` binding is unverified, a suffix/fuzzy binding is probably junk.

        The one signal that separates the two cases cheaply is the SHARE. A project symbol with a
        genuinely unique name picks up an unverified row here and there; a name the index does not
        own — a framework global, a builtin method — collects nothing else, because every call in
        the repository lands on it. So an answer that is entirely unverified is called out as the
        collision signature it almost always is.

        Rows are marked in place so the badge travels with the row into whichever section the
        renderer puts it, including the per-symbol sections of an ambiguous answer that a note alone
        would never reach. Unstamped rows are counted apart and never badged: silence from the
        backend is not a low score, and flattening the two is the same error one level down.
        """
        weak = unverified = unstamped = no_column = total = 0
        for g in groups:
            for r in g.rows:
                total += 1
                conf = _edge_confidence(r)
                if conf is None:
                    # Two different silences, and only one of them is about the code. A row with NO
                    # `c.confidence` key came from a backend (or a generation) that does not return
                    # the column at all; a row whose key is present but empty is an edge that
                    # backend declined to score. The first says nothing about this answer, the
                    # second says this specific edge's provenance is unknown.
                    if "c.confidence" in r:
                        unstamped += 1
                    else:
                        no_column += 1
                elif conf <= _EDGE_CONFIDENCE_WEAK:
                    r["_low_confidence"] = conf
                    weak += 1
                elif conf < _EDGE_CONFIDENCE_FLOOR:
                    r["_low_confidence"] = conf
                    unverified += 1
        if not (weak or unverified or unstamped):
            return ""
        # A backend that never returns the column at all is not producing partial answers — it is a
        # generation that does not report confidence. Marking every such answer `partial` would
        # repeat, one level up, the defect `attach_confidence` exists to fix: a field that fires
        # everywhere tells a reader nothing, and "partial" has to keep meaning "a named part of THIS
        # answer could not be retrieved". An edge the backend returned UNSCORED is the opposite case
        # and stays a gap, even when every row in one answer happens to be unscored — that is how a
        # symbol the repository never defines (`get`, resolved onto `dict.get`) is caught.
        if no_column == total:
            return ""

        details, parts = [], []
        if weak:
            details.append(
                f"{weak} of {total} row(s) were resolved by suffix or string-similarity match "
                f"(confidence <= {_EDGE_CONFIDENCE_WEAK}) and are likely spurious")
            parts.append(
                f"**{weak} of {total} row(s) are LIKELY SPURIOUS** — resolved by suffix or "
                f"string-similarity match (confidence <= {_EDGE_CONFIDENCE_WEAK}), not by any "
                f"import.")
        if unverified:
            details.append(
                f"{unverified} of {total} row(s) were resolved by bare symbol name because this is "
                f"the only symbol of that name in the index (confidence < {_EDGE_CONFIDENCE_FLOOR}), "
                f"not by following the calling file's imports")
            parts.append(
                f"**{unverified} of {total} row(s) are UNVERIFIED** — bound because this is the only "
                f"symbol of that name in the index (confidence < {_EDGE_CONFIDENCE_FLOOR}), not by "
                f"following the calling file's imports. Correct when the call really targets this "
                f"symbol; wrong when it targets a same-named symbol the index never saw.")
        if unstamped:
            details.append(
                f"{unstamped} of {total} row(s) carry no confidence from the backend at all, so how "
                f"they were resolved is unknown")
            parts.append(
                f"{unstamped} of {total} row(s) carry no confidence from the backend, so how they "
                f"were resolved is unknown — re-index to have them scored.")
        # The collision signature: nothing here was resolved through an import, across enough rows
        # that the pattern means something. A project symbol collects the occasional unverified
        # caller; a name the index does not own collects every call in the repository.
        if total >= 5 and (weak + unverified) == total:
            # Raised as its OWN kind, not folded into the one above, because this is the condition
            # the gateway escalates on: it is machine-checkable, and matching on a phrase inside a
            # prose `detail` would be a string-matching contract between two modules — the kind that
            # breaks silently the first time the wording is improved.
            self._add_gap(
                op, "all-rows-name-resolved",
                "no row in this answer was resolved through an import, which is the signature of a "
                "name the index does not own (a library function, a framework global, a builtin "
                "method) collecting every call site that mentions it",
            )
            parts.append(
                "**Not one row here was resolved through an import.** That is the signature of a "
                "name this index does not own — a library function, a framework global, a builtin "
                "method — collecting every call site in the repository that mentions it. Treat the "
                "whole answer as unconfirmed until `--engine lsp` agrees.")
        self._add_gap(op, "low-confidence-edges", "; ".join(details))
        marked = "Marked `[?…]` (unverified) and `[!…]` (likely spurious) below. " if (
            weak or unverified) else ""
        return "\n\n_" + marked + " ".join(parts) + "_"

    def _collision_note(self, op: str, dropped: int) -> str:
        """Disclose rows dropped as name collisions, in the body AND as a machine-readable gap.

        Shared by both edge ops so one cannot end up disclosing while the other stays silent -- the
        exact drift this project keeps guarding against. The far end named differs by op: a `callees`
        row is a collision against the CALLER, a `callers` row against the symbol being called."""
        if not dropped:
            return ""
        anchor = "caller" if op == "callees" else "called symbol"
        self._add_gap(
            op, "name-collisions-dropped",
            f"{dropped} row(s) were dropped as name collisions (a different language, or a "
            f"non-code file, than the {anchor}); resolution is by symbol name, not by type",
        )
        return (f"\n\n_{dropped} row(s) dropped as name collisions (a different language, or a "
                f"non-code file, than the {anchor})._")

    def _empty_edge_answer(
        self, op: str, unit: str, target: str, wanted: _SymbolTarget,
        selected: list[_EdgeGroup], notes: str, truncated: bool, dropped: int,
    ) -> str:
        """Every row this op found was set aside by its own collision filter.

        `rows` was non-empty (the genuine miss returned None earlier), so this is an answer WE
        emptied, not an absence in the repository. Routing it into the `not-in-graph` branch would
        read as "0 {unit}s" -- a statement about the code -- when the honest one is "N found, all
        filtered for a reason that has nothing to do with it". Shared by both ops for the same
        anti-drift reason."""
        if not dropped:                # nothing found and nothing dropped cannot both be true
            self._add_gap(
                op, "name-collisions-dropped",
                "every row returned was set aside, so this may under-report — resolution is "
                "by symbol name, not by type",
            )
        return (f"## {op.capitalize()} of {target} (0)\n(no {unit} survived name-collision filtering)"
                + notes + self._name_resolution_note(wanted, selected, truncated))

    def _op_callees(self, target: str, project: str, timeout_ms: int) -> str | None:
        """What *target* calls or uses.

        This keys on the UNQUALIFIED name, which is the honest limitation of the traversal: every
        node named `write_board_mirror` matches, and so do the edges out of all of them. Worse, the
        extractor emits edges for bare local names, so a function whose body says `f.write(...)`,
        `conn`, `tmp_path` or `snapshot` acquires edges to whatever else in the repository happens
        to carry those names. On one evaluated symbol that produced five wrong rows out of seven —
        including a TypeScript function in an Electron preload reached from a Python file-writer,
        and a JSON file inside an `.archive/` directory reported as a callee.

        Two of those three causes are upstream in the extractor and can only be filtered here. This
        does filter them: a callee in a different language family than the caller, or in a file that
        is not code at all, is not a callee — it is a name collision, and dropping it costs nothing
        real.

        The remaining cause — several distinct symbols sharing the bare name — is not a collision to
        drop but a question to ask. It used to be neither: rows from every matched caller were
        flattened into one list, so the answer was the union of several questions with nothing
        saying which row came from where. Now the rows are GROUPED by their caller, and:

        * a target carrying a disambiguator (`pkg.mod.handle`, `handle@src/mod.py`) selects one
          group and answers only for it — resolution rather than disclosure;
        * without one, every group is rendered under its own heading, the count of same-named
          symbols is stated, and nothing is dropped for being ambiguous. Three symbols named
          `handle` is not a degraded answer, it is a question, and the result can ask it.

        Grouping also makes the language check structural. It is resolved against the group's own
        caller file, so the union-across-callers bug that `0.15.5` fixed — a `.ts` callee reached
        from a *Python* caller surviving because an unrelated TypeScript caller shared the bare name
        contributed `ts-js` to a shared set — is no longer expressible here: there is no shared set
        to compare against.
        """
        wanted = _parse_symbol_target(target)
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE a.name="{_cypher_literal(wanted.name)}" '
            "RETURN b.name, b.qualified_name, b.file_path, labels(b), type(c), c.confidence, "
            f"a.name, a.qualified_name, a.file_path LIMIT {_EDGE_ROW_LIMIT}"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        truncated = len(rows) >= _EDGE_ROW_LIMIT

        callers = _group_edges(rows, "a.name", "a.qualified_name", "a.file_path")
        selected = [g for g in callers if wanted.matches(g.qn_raw, g.file)]
        if wanted.narrowed and not selected:
            return self._no_symbol_matched_the_hint("callees", target, wanted, callers)

        dropped = self._drop_edge_collisions(selected, "b.file_path", "labels(b)")
        # Symmetric with `callers`: relabel any module-scope pseudo-node on the displayed (callee)
        # side. The backend never emits a File/Module node as a callee today — the callee side of
        # every edge is a Function/Method/Class/Variable/Decorator — so this is a no-op now, kept so
        # the two ops treat the pseudo-node population identically and a future callee container is
        # handled without a second fix.
        self._collapse_module_scope(selected, "labels(b)", "b.file_path")
        notes = self._confidence_note("callees", selected) + self._collision_note("callees", dropped)
        if truncated:
            notes = self._row_cap_note("callees", target) + notes
        if not any(g.rows for g in selected):
            return self._empty_edge_answer(
                "callees", "callee", target, wanted, selected, notes, truncated, dropped)
        return self._render_edge_answer(
            "callees", "callee", target, wanted, selected,
            ("b.name", "b.qualified_name", "b.file_path"), truncated, notes)

    def _render_edge_answer(
        self, op: str, unit: str, target: str, wanted: _SymbolTarget,
        groups: list[_EdgeGroup], row_keys: tuple[str, str, str], truncated: bool,
        extra_notes: str = "",
    ) -> str:
        """Render one edge-op answer from rows already grouped by the symbol they belong to.

        Shared by `callers` and `callees` for the same reason `_render_scan` is shared by the
        repo-scan ops: the drift-prone parts are the heading, the ambiguity disclosure and the
        truncation note, and having two copies of those is how one op ends up honest and the other
        one silent. Each group's heading names the matched TARGET symbol and its rows are the other
        end of the edge, which is the same shape in both directions."""
        name_key, qn_key, file_key = row_keys
        answered = [g for g in groups if g.rows]
        kept = sum(len(g.rows) for g in answered)
        head = f"## {op.capitalize()} of {target} ({kept})\n"

        if len(answered) == 1:
            body = head + "\n".join(self._display(r, name_key, qn_key, file_key)
                                    for r in answered[0].rows)
        else:
            # Several distinct symbols share the name. Keep every one of them, each under its own
            # heading — a merged list presented as one symbol's answer is the reading these ops most
            # need to prevent, since they feed "is this safe to change?".
            self._add_gap(
                op, "target-ambiguous",
                f"{len(answered)} distinct symbols named `{wanted.name}` match this target; their "
                f"{unit}s are grouped separately rather than merged into one list. Narrow the target "
                f"with a qualified name or `{wanted.name}@<file>` to answer about one of them",
            )
            sections = [
                f"### {g.describe()} — {len(g.rows)} {unit}(s)\n"
                + "\n".join(self._display(r, name_key, qn_key, file_key) for r in g.rows)
                for g in answered[:_CANDIDATE_CAP]
            ]
            if len(answered) > _CANDIDATE_CAP:
                sections.append(f"… (+{len(answered) - _CANDIDATE_CAP} more symbol(s) with this "
                                f"name, not shown)")
            body = (head
                    + f"**{len(answered)} distinct symbols in this index are named `{wanted.name}`** "
                      f"— these are that many separate answers, not one. Ask again as "
                      f"`{answered[0].label or wanted.name}` or "
                      f"`{wanted.name}@{answered[0].file or '<file>'}` for a single symbol.\n"
                    + "\n" + "\n\n".join(sections))
        return body + extra_notes + self._name_resolution_note(wanted, answered, truncated)

    def _name_resolution_note(
        self, wanted: _SymbolTarget, groups: list[_EdgeGroup], truncated: bool
    ) -> str:
        """State how the target was resolved — which is a different fact in each case.

        The old note was one conditional sentence for every answer ("IF more than one symbol is
        called X, their callees are merged here"), which is true but tells the reader to worry
        without saying whether there is anything to worry about. Grouping means the count is now
        known, so this says which of the three situations produced the answer in hand. It does not
        claim uniqueness when the row cap was hit: a symbol whose rows fell past the cap is
        indistinguishable from one that does not exist."""
        if wanted.narrowed:
            named = ", ".join(g.describe() for g in groups) or "nothing"
            return (f"\n\n_Narrowed by the {wanted.describe()} in the target to {named}. Other "
                    f"symbols named `{wanted.name}` are not included._")
        if len(groups) == 1 and groups[0].label and not truncated:
            # Being the only indexed holder of a name is not only a reassurance — it is the exact
            # precondition for the backend's `unique_name` strategy, which binds ANY unresolved call
            # to that bare name here, including calls to symbols this index never saw (an npm or pip
            # package, a test-framework global, a builtin method). So the sentence that used to end
            # in "the only symbol …" now says what that implies, because on the evaluated repository
            # it was the whole cause of a 32-row answer in which no row was a caller.
            # The consequence clause is only true of an answer that HAS such rows. Printing it over
            # a fully import-resolved answer would point at badges that are not there, which is the
            # cry-wolf failure the tiering above exists to avoid, arriving through the legend.
            badged = any(r.get("_low_confidence") is not None
                         for g in groups for r in g.rows)
            because = (
                f" — which is also why any unresolved call to a `{wanted.name}` the index does not "
                f"contain (a library function, a framework global, a builtin method) binds here. "
                f"Rows badged `[?…]` or `[!…]` are those bindings."
            ) if badged else "."
            return (f"\n\n_Resolved by symbol NAME, not by type: {groups[0].describe()} is the "
                    f"only symbol named `{wanted.name}` with edges in this index{because}_")
        return (f"\n\n_Resolved by symbol NAME, not by type: if more than one symbol in this "
                f"repository is called `{wanted.name}`, their edges are reported together here. "
                f"Verify before relying on a row you did not expect._")

    def _op_impact(self, target: str, project: str, timeout_ms: int) -> str | None:
        callers = self._op_callers(target, project, timeout_ms)
        callees = self._op_callees(target, project, timeout_ms)
        if callers is None and callees is None:
            return None
        # callers/callees already carry their own "## Callers of X (N)" header — don't wrap them
        # in a second "### Callers" header (that produced a redundant double heading).
        parts = [f"## Impact of {target}"]
        parts.append(callers or f"## Callers of {target} (0)\n(none found)")
        parts.append(callees or f"## Callees of {target} (0)\n(none found)")
        return "\n".join(parts)

    def _op_chain(self, target: str, project: str, timeout_ms: int) -> str | None:
        # Accept an "A->B" form (trace from the source symbol) or a bare symbol.
        src = target.split("->")[0].strip() if "->" in target else target.strip()
        # A `name@file` hint is codeintel's own disambiguator (see `_SymbolTarget`); `trace_path`
        # would look for a function literally called that and report nothing found. A DOTTED target
        # is deliberately left intact — the backend resolves qualified names itself and reports its
        # own ambiguity, which is better information than anything reconstructed from a bare name.
        if "@" in src:
            head, _, tail = src.rpartition("@")
            if head.strip() and tail.strip():
                src = head.strip()
        if not src:
            return None
        raw = self._run(
            "trace_path",
            {"project": project, "function_name": src, "mode": "calls", "risk_labels": True},
            timeout_ms,
        )
        if not isinstance(raw, dict):
            return None
        if raw.get("status") == "ambiguous":
            sugg = raw.get("suggestions") or []
            names = [_label_of(s) for s in sugg if isinstance(s, dict)]
            if not names:
                return None
            body = "\n".join(f"- {n}" for n in names)
            return f"## Ambiguous symbol '{src}' — candidates\n{body}"

        def _fmt(items: Any) -> list[str]:
            out = []
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    nm = str(it.get("name") or "?")
                    qn = _strip_project_prefix(str(it.get("qualified_name") or ""),
                                               may_be_filename=False)
                    hop = it.get("hop")
                    risk = it.get("risk")
                    label = qn or nm
                    hop_s = f" [hop {hop}]" if hop is not None else ""
                    risk_s = f" [risk: {risk}]" if risk else ""
                    out.append(f"- {label}{hop_s}{risk_s}")
            return out

        callees = _fmt(raw.get("callees"))
        callers = _fmt(raw.get("callers"))
        if not callees and not callers:
            return None
        parts = [f"## Call chain for {src}"]
        parts.append("### Callees (downstream)")
        parts.extend(callees or ["(none)"])
        parts.append("### Callers (upstream)")
        parts.extend(callers or ["(none)"])
        return "\n".join(parts)

    def _op_pattern(self, target: str, project: str, timeout_ms: int) -> str | None:
        try:
            raw = self._run("search_code", {"project": project, "pattern": target}, timeout_ms)
            results = raw.get("results") if isinstance(raw, dict) else raw
            if not isinstance(results, list) or not results:
                return f'## Pattern matches for "{target}"\n(no matches)'
            lines = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                node = _strip_project_prefix(str(r.get("node") or r.get("qualified_name") or "?"))
                label = str(r.get("label") or "")
                file = str(r.get("file") or "")
                start = r.get("start_line")
                # `codebase-memory-mcp` reports 1-based line numbers (LINE_BASES["graph"] in
                # loc.py), so a usable line here has to be a real int >= 1 — the same policy
                # loc.py:48-49 already applies for the 0-based engines. Rendering anything less
                # (0, -1, or a non-int the backend happened to send) produced `path:0`, a line
                # number that does not exist in any editor.
                usable = isinstance(start, int) and not isinstance(start, bool) and start >= 1
                loc = f"{file}:{start}" if file and usable else file
                ml = r.get("match_lines")
                ml_s = f"  (lines {', '.join(str(x) for x in ml)})" if isinstance(ml, list) and ml else ""
                badge = f" [{label}]" if label else ""
                lines.append(f"- {node}{badge} {loc}{ml_s}".rstrip())
            if not lines:
                return f'## Pattern matches for "{target}"\n(no matches)'
            return f'## Pattern matches for "{target}" ({len(lines)})\n' + "\n".join(lines)
        except Exception:
            return None

    def _op_overview(self, target: str, project: str, timeout_ms: int, root: str = "") -> str | None:
        try:
            raw = self._run("get_architecture", {"project": project}, timeout_ms)
            if not isinstance(raw, dict):
                return None
            # Title with the REPO's own name, not the backend's project id. That id is often a
            # flattened absolute path (`Users-alice-Documents-project-myrepo`), and this heading
            # lands in CODE_INTEL.md — a file that gets committed and pushed, so an internal
            # identifier there leaks the author's home directory layout into the repository.
            #
            # But naming the CALLER's directory unconditionally made the presentation layer assert
            # a provenance the data layer never established. With a project's index file removed
            # from under it, the backend answered from a different tree and this heading still read
            # `## Architecture: <the caller's repo>` — wrong numbers, confidently attributed. Claim the
            # repo's name only when the resolved project actually matched THIS root; otherwise say
            # what the answer is really about.
            answered_elsewhere = self._answered_root_mismatch(root)
            name = "" if answered_elsewhere else _repo_display_name(root)
            name = name or str(raw.get("project") or project)
            parts = [f"## Architecture: {name}"]
            if answered_elsewhere:
                parts.append(
                    "> Provenance: this answer comes from the indexed project that contains "
                    "the directory you asked about, so the counts below describe a different "
                    "tree. Index it standalone for an answer scoped to it."
                )
            tn, te = raw.get("total_nodes"), raw.get("total_edges")
            if tn is not None or te is not None:
                parts.append(f"{tn or 0} nodes, {te or 0} edges")

            def _counts(items: Any, key: str, ckey: str = "count") -> list[str]:
                if not isinstance(items, list):
                    return []
                return [f"- {it.get(key)}: {it.get(ckey)}" for it in items
                        if isinstance(it, dict) and it.get(key) is not None]

            node_labels = _counts(raw.get("node_labels"), "label")
            edge_types = _counts(raw.get("edge_types"), "type")
            if node_labels:
                parts.append("### Node types")
                parts.extend(node_labels)
            if edge_types:
                parts.append("### Edge types")
                parts.extend(edge_types)

            langs = raw.get("languages")
            if isinstance(langs, list) and langs:
                lang_lines = []
                for it in langs:
                    if isinstance(it, dict):
                        lang_lines.append("- " + ", ".join(f"{k}: {v}" for k, v in it.items()))
                    else:
                        lang_lines.append(f"- {it}")
                if lang_lines:
                    parts.append("### Languages")
                    parts.extend(lang_lines)

            if len(parts) == 1:  # nothing but the title — treat as no data
                return None
            return "\n".join(parts)
        except Exception:
            return None

    # -------------------------------------------------- repo-scan ops (no target)
    # These key on the whole index / git worktree, not a symbol — `target` is ignored. A clean/empty
    # scan is a TRUE answer ("nothing changed", "no dead code"), not a lookup miss, so they return an
    # informative string; only a backend failure returns None (→ safe-null upstream).

    def _op_changed(self, project: str, timeout_ms: int) -> str | None:
        """Impact of the working tree's UNCOMMITTED changes: changed files → impacted symbols. The
        flagship pre-edit op. detect_changes drives a backend-side reindex of the changed files, so
        it gets a higher timeout floor than a plain read."""
        try:
            raw = self._run("detect_changes", {"project": project}, max(timeout_ms, 15000))
            if not isinstance(raw, dict):
                return None
            files_raw = raw.get("changed_files")
            syms_raw = raw.get("impacted_symbols")
            # Guard against a non-detect_changes dict (e.g. a backend error object): if NEITHER key
            # is a list, this isn't a real response — degrade to safe-null, NOT a false "clean tree".
            if not isinstance(files_raw, list) and not isinstance(syms_raw, list):
                return None
            # The backend returns DUPLICATE changed_files (staged + unstaged views) — dedupe,
            # order-preserving (dogfooding showed 6 real files reported as 11).
            # …and scope to SOURCE. Dogfooding reported "4 files → 28 symbols" where the files were
            # `.gitignore`, two plan JSONs and `CODE_INTEL.md` — codeintel's OWN artifact — and all
            # 28 "impacted symbols" were markdown headings out of it. The indexer's corpus policy
            # cannot be reused here: it admits `.md` on purpose, for semantic search. `dropped`
            # remembers that non-source changes existed, so a tree full of them cannot be reported
            # as "clean" — that would trade a noisy answer for a false one.
            files, seen_f, dropped = [], set(), 0
            for f in files_raw if isinstance(files_raw, list) else []:
                if isinstance(f, str) and f not in seen_f:
                    seen_f.add(f)
                    if is_code_path(f):
                        files.append(f)
                    else:
                        dropped += 1
            # impacted_symbols interleaves real symbols with bare file/module markers whose label IS
            # its own path (name == qualified_name == file_path). Drop those structurally by comparing
            # label to file_path — this catches a root-level marker (`main.py`, no "/") AND avoids
            # dropping a real symbol whose qualified name legitimately contains "/" (e.g. Go's
            # github.com/org/pkg.Func). Files are already listed above; dedupe the rest.
            syms, seen_s = [], set()
            for s in syms_raw if isinstance(syms_raw, list) else []:
                if not isinstance(s, dict):
                    continue
                label = _label_of(s).strip()
                fp = str(s.get("file_path") or s.get("file") or "")
                if not label or label == fp:
                    continue
                # Same source scoping as the file list. A symbol whose file_path is MISSING is kept:
                # an absent path is a backend quirk, not evidence of junk, and dropping it would
                # under-report real impact — the one failure mode worse than over-reporting here.
                if fp and not is_code_path(fp):
                    dropped += 1
                    continue
                key = (label, fp)
                if key in seen_s:
                    continue
                seen_s.add(key)
                syms.append((label, fp))
            if not files and not syms:
                if dropped:
                    return ("## Changes impact\n(no source changes — the working tree's "
                            f"{dropped} uncommitted change(s) are all non-source files)")
                return "## Changes impact\n(working tree clean — no uncommitted changes)"
            ripple, ripple_truncated = self._changed_ripple(files, project, timeout_ms)
            parts = [f"## Changes impact ({len(files)} files → {len(syms)} symbols defined in them "
                     f"→ {len(ripple)} callers elsewhere)"]
            if files:
                parts.append(f"### Changed files ({len(files)})")
                parts.extend(f"- {f}" for f in files[:40])
                if len(files) > 40:
                    parts.append(f"… (+{len(files) - 40} more)")
            if syms:
                # Named for what they are. These are the symbols the edit CONTAINS — everything
                # defined in a touched file, whether or not the edit came near it — which is a
                # different and much weaker claim than "impacted", the word that used to head this
                # list and the reason it read as a blast radius.
                parts.append(f"### Symbols defined in the changed files ({len(syms)})")
                for label, fp in syms[:40]:
                    tail = f"  ({fp})" if fp and fp != label else ""
                    parts.append(f"- {label}{tail}")
                if len(syms) > 40:
                    parts.append(f"… (+{len(syms) - 40} more)")
            if ripple:
                parts.append(f"### Callers elsewhere that reach into them ({len(ripple)})")
                parts.append("_This is the blast radius: symbols outside the changed files whose "
                             "behaviour can move because of this edit._")
                for label, fp, conf in ripple[:40]:
                    tail = f"  ({fp})" if fp and fp != label else ""
                    if conf is None:
                        mark = " [unscored]"
                    elif conf <= _EDGE_CONFIDENCE_WEAK:
                        mark = f" [!{conf:.2f}]"
                    elif conf < _EDGE_CONFIDENCE_FLOOR:
                        mark = f" [?{conf:.2f}]"
                    else:
                        mark = ""
                    parts.append(f"- {label or fp}{mark}{tail}")
                if len(ripple) > 40:
                    parts.append(f"… (+{len(ripple) - 40} more)")
                weak = sum(1 for _, _, c in ripple
                           if c is not None and c < _EDGE_CONFIDENCE_FLOOR)
                if weak:
                    parts.append(f"\n_{weak} of {len(ripple)} caller(s) were resolved by symbol "
                                 f"name rather than by import (`?`/`!` above) and may not reach "
                                 f"this code at all._")
            elif files:
                # An empty ripple is a real and useful answer — but only if it cannot be confused
                # with one that was never computed.
                parts.append("### Callers elsewhere that reach into them (0)")
                parts.append("_No symbol outside the changed files calls into them, by the graph's "
                             "CALLS edges. Framework dispatch and calls through a value are not "
                             "edges, so this is not proof that nothing else is affected._")
            if ripple_truncated:
                self._add_gap(
                    "changed", "ripple-truncated",
                    "the downstream caller list hit its own cap, so the blast radius shown is a "
                    "lower bound, not the whole of it",
                )
                parts.append("\n_Downstream list truncated at its cap — this is a lower bound._")
            return "\n".join(parts)
        except Exception:
            return None

    _RIPPLE_FILE_CAP = 40
    _RIPPLE_ROW_CAP = 60

    def _changed_ripple(
        self, files: list[str], project: str, timeout_ms: int
    ) -> tuple[list[tuple[str, str, float | None]], bool]:
        """Symbols OUTSIDE the changed files that call into them — the actual blast radius.

        `changed` used to stop at containment and call it impact. Editing one function in
        `src/domain/budget.ts` reported "1 file -> 7 symbols", and all seven were the symbols DEFINED
        in that file; `runAlerts`, which calls into it from another file and is the one thing a
        reviewer needed to look at, appeared nowhere. The tool's own instructions promise the symbols
        an edit "ripples into", so the op was answering a different question than the one it
        advertised — and the containment answer is the one an agent is least likely to notice is
        wrong, because it is never empty.

        Deliberately NOT filtered by the confidence floor. A pre-commit checklist errs toward
        over-inclusion: `runAlerts -> evaluate` is a genuine ripple edge stamped 0.75, so a floor
        here would drop the very row that motivated the fix. Instead every caller is collapsed to its
        best-scored edge and carries that score, so a name-collision flood (every test file in the
        repo, reached through a framework global) is visible as the low-confidence block it is rather
        than swamping the list.
        """
        if not files:
            return [], False
        listed = files[: self._RIPPLE_FILE_CAP]
        in_list = ", ".join(f'"{_cypher_literal(f)}"' for f in listed)
        cypher = (
            f"MATCH (a)-[c:CALLS]->(b) WHERE b.file_path IN [{in_list}] "
            f"AND NOT a.file_path IN [{in_list}] "
            "RETURN a.qualified_name, a.file_path, c.confidence "
            f"LIMIT {self._RIPPLE_ROW_CAP}"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        truncated = len(rows) >= self._RIPPLE_ROW_CAP or len(files) > self._RIPPLE_FILE_CAP
        # One entry per calling symbol, keeping its best-scored edge: a caller that reaches three
        # changed symbols is one thing to review, not three.
        best: dict[tuple[str, str], float | None] = {}
        for r in rows:
            label = _strip_project_prefix(
                str(r.get("a.qualified_name") or ""), may_be_filename=False)
            fp = str(r.get("a.file_path") or "")
            if not label and not fp:
                continue
            conf = _edge_confidence(r)
            key = (label, fp)
            prev = best.get(key, -1.0)
            if key not in best or (conf is not None and (prev is None or conf > prev)):
                best[key] = conf
        out = [(lbl, fp, c) for (lbl, fp), c in best.items()]
        out.sort(key=lambda t: (-(t[2] if t[2] is not None else -1.0), t[0]))
        return out, truncated

    def _op_hotspots(self, project: str, timeout_ms: int) -> str | None:
        """Highest complexity / fan-in symbols (refactor-risk hotspots). search_graph returns rows
        UNSORTED (name order) and caps at ``limit``, so we over-request then sort CLIENT-SIDE by
        (complexity, in_degree). Tests/builtins filtered out.

        Two things were wrong with the request itself, and together they made this op report the
        UI layer of every repo it was pointed at:

        - It asked for ``label: "Function"`` only. A class method is a ``Method`` node, so every
          Python method and every TypeScript class method was invisible to it — on one evaluated
          repo that hid 2,381 symbols behind 1,343 that were considered.
        - It capped at 200 rows and then sorted those. The backend returns rows in NAME order, so
          that is an arbitrary alphabetical slice, not the 200 most complex — the client-side sort
          could only ever rank what the truncation happened to admit. On a 4,883-function repo the
          sample was 4% of the candidates and the "top hotspots" were the top of that 4%.
        """
        try:
            rows: list[dict] = []
            saw_any = False
            for label in ("Function", "Method"):
                got = self._search_symbols(
                    # `fields` asks 0.10.x for the per-node metrics this op RANKS on. They are core
                    # columns in 0.9.x and optional ones in 0.10.x, where omitting them yields rows
                    # whose complexity is uniformly zero — a hotspots list sorted by nothing.
                    # 0.9.x ignores the key, so one payload serves both.
                    {"label": label, "min_degree": 1, "limit": 2000,
                     "fields": ["complexity", "cognitive", "is_test"]},
                    project, timeout_ms,
                )
                if got is None:
                    continue
                saw_any = True
                rows.extend(got)
            if not saw_any:
                return None
            kept = [r for r in rows if not self._is_noise(r)]
            if not kept:
                return "## Complexity / fan-in hotspots\n(none found)"
            kept.sort(key=lambda r: (r.get("complexity") or 0, r.get("in_degree") or 0), reverse=True)
            coverage = _language_coverage_note(kept[:30])

            def _meta(r: dict) -> list[str]:
                m = [f"in:{r.get('in_degree') or 0} out:{r.get('out_degree') or 0}",
                     f"cx:{r.get('complexity') or 0} cog:{r.get('cognitive') or 0}"]
                if r.get("lines") is not None:
                    m.append(f"{r.get('lines')} lines")
                return m

            return self._render_scan(kept, "Complexity / fan-in hotspots", 25, _meta) + coverage
        except Exception:
            return None

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
                return safe_null_result(op_str, target_str, engine="graph", reason="engine-unavailable")

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
                        return safe_null_result(
                            op_str, target_str, engine="graph", reason="no-edges",
                            hint=f"`{target_str}` IS indexed ({'; '.join(where[:3])}) — it has no "
                                 f"{op_str} edge in the graph, which is not the same as being "
                                 f"absent. Framework-dispatched handlers (routes, ASGI apps) and "
                                 f"symbols passed as a value rather than called look exactly like "
                                 f"this, so do NOT read it as dead code. Re-indexing will not "
                                 f"change it; confirm with `--engine lsp` or `--op pattern`.",
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

            return attach_confidence({
                "ok": True,
                "op": op_str,
                "target": target_str,
                "result": result_text,
                "engine": "graph",
                "cached": False,
            }, self._pending_gaps)
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
