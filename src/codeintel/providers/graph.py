from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from codeintel.outcome import Missing
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result
from codeintel.source_kind import is_code_path, looks_generated_path, looks_generated_text


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
# repo's hotspots", it is the answer to a different question. `deadcode` is the dangerous one — a
# symbol that is dead within one repo is routinely live in its sibling, so an ancestor-scoped
# answer tells an agent to delete working code. The symbol-scoped ops are not listed: for a genuine
# subdirectory of a monorepo, an ancestor index is the CORRECT place to find a symbol's callers, so
# those answer and carry a caveat instead.
_ROOT_SCOPED_OPS = frozenset({"overview", "changed", "changes", "deadcode", "hotspots"})

# Ops withdrawn from the product because they were measured wrong, not merely imprecise.
#
# `deadcode` was wrong in BOTH directions on real repositories: on one it named five candidates of
# which four were live (a rollup plugin hook; two entries of a `Record<string, fn>` reached by a
# runtime string; a `predicate` property passed inline to the call that consumes it), and on another
# it reported "(none found)" for a 4,883-function codebase containing at least three genuinely
# unreferenced private helpers. It is the one op whose output is an instruction to delete code, so
# it needs the highest evidence bar and currently has the least. It returns when a labelled corpus
# measures its precision and recall — not before.
#
# `hotspots` was withdrawn alongside it and has since been REINSTATED. Its rankings were 100% `.tsx`
# on two repositories that are two-thirds Python and backend TypeScript, caused by two request bugs
# rather than by a missing metric: it asked only for `Function` nodes (so every class method was
# invisible — 2,381 of them on one repo) and capped candidates at 200 rows returned in NAME order,
# making the client-side sort rank an alphabetical 4% slice. Both are fixed, and the fix is measured:
# `test_hotspots_ranks_across_languages` pins the mixed-language behaviour, and re-running the two
# evaluation repositories now yields 11 `.py` / 12 `.tsx` / 2 `.ts` and 18 `.ts` / 7 `.tsx` with the
# gnarliest Python and backend functions at the top. A ranking that cannot see a language now says
# so via `_language_coverage_note` instead of reading like a result.
_WITHDRAWN_OPS: dict[str, str] = {
    "deadcode": (
        "`deadcode` is withdrawn: it was measured producing both false positives (framework-"
        "dispatched symbols reported as dead) and false negatives (unreferenced helpers missed), "
        "and its output invites deletion. Use `callers` on a specific symbol instead — that is "
        "verified accurate. Set CODEINTEL_ENABLE_UNVERIFIED_OPS=1 to run it anyway."
    ),
}


def _unverified_ops_enabled() -> bool:
    return os.environ.get("CODEINTEL_ENABLE_UNVERIFIED_OPS", "").strip().lower() in (
        "1", "true", "on", "yes",
    )


# Trailing segments that mean "this is a filename, not a dotted module path".
_FILE_EXTENSIONS = frozenset({
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "rb", "php",
    "cs", "swift", "scala", "c", "h", "cpp", "cc", "hpp", "css", "scss", "less", "html", "vue",
    "svelte", "md", "mdx", "rst", "txt", "json", "yaml", "yml", "toml", "ini", "cfg", "xml",
    "sql", "sh", "bash", "zsh", "graphql", "proto",
})


def _strip_project_prefix(qualified_name: str) -> str:
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
    # actually separates a filename from a qualified name is the LAST segment: a module path ends
    # in a symbol, a filename ends in an extension.
    if qualified_name.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS:
        return qualified_name
    # `use-toast.ts` is now only distinguishable from `my-repo.Execute` by that extension check, so
    # a hyphenated head with a single non-extension segment after it is treated as a slug. That is
    # the correct call: a kebab-case FILE whose extension we do not know is rare, while a flat
    # qualified name is the norm for Go, Java, C# and Ruby.
    return rest


# Files consulted when verifying dead-code candidates, and the extensions worth reading. Bounded
# so `deadcode` on a very large monorepo stays a query rather than a second index pass.
_VERIFY_FILE_CAP = 6000
# Not hand-written source: vendored trees and build output. Only `.venv` was excluded before (as
# a dot-directory), so a plain `venv/`, `vendor/` or `third_party/` both blew the file cap and
# fed generated code into the occurrence counts.
_VERIFY_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "dist", "build", "out", "target", "vendor", "vendored",
    "third_party", "thirdparty", "venv", "env", "site-packages", "coverage", "generated",
})
# Extensions the reference scan reads. A name can be referenced from a file that is not itself a
# "source" file in the graph's sense — SCSS `@include button-base`, a template, a config — and if
# the scan cannot open it, the symbol reads as dead while the note claims verification succeeded.
# That is worse than not verifying at all, so this list is generous: a false reference only hides
# a candidate, while a missed one names live code.
_VERIFY_EXTS = frozenset({
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cs", ".rb", ".php", ".swift", ".scala",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".astro",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".graphql", ".proto",
    ".sql", ".sh", ".bash", ".zsh", ".rst", ".md", ".mdx", ".txt", ".tpl", ".j2", ".hbs", ".ejs",
})


def _drop_referenced_symbols(rows: list[dict], root: str) -> tuple[list[dict], str]:
    """Remove candidates whose name actually appears somewhere else in the source.

    `deadcode` asks the graph for functions with **in-degree 0**, and a function that is passed as
    a REFERENCE rather than called has in-degree 0 — every React event handler, every
    `addEventListener('keydown', onKeyDown)`, every callback handed to a framework. On a real
    TypeScript repo that made 181 of 181 sampled candidates false, and an agent acting on the
    answer would delete live code. A wrong answer here is worse than no answer.

    The graph cannot see those edges, so verify against the source: one pass over the repo,
    counting word-boundary occurrences of each candidate name. A name appearing anywhere beyond
    its own definition is referenced and drops out. Returns (kept, verified) — `verified` is False
    when the repo exceeded the file cap, so the caller can say the check was partial rather than
    imply a confidence it does not have.
    """
    if not rows:
        return rows, "ok"
    if not root or not os.path.isdir(root):
        # Distinct from the cap: the MCP tool defaults project_root to "", so this is the DEFAULT
        # call path, and blaming the file cap told the user their repo was too big when the real
        # cause was a missing argument.
        return rows, "no-root"

    names = {str(r.get("name") or "") for r in rows}
    names.discard("")
    if not names:
        return rows, "ok"

    # How many candidates share each name. Compared against a GLOBAL occurrence count, a fixed
    # allowance of 1 meant two dead functions with the same name each counted as the other's
    # "use" and both vanished.
    definitions: dict[str, int] = {}
    for r in rows:
        key = str(r.get("name") or "")
        definitions[key] = definitions.get(key, 0) + 1
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")

    seen: dict[str, int] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip what is NOT hand-written source. Generated bundles were the worst offender: a
        # 6.7MB minified `out/` bundle supplied 46 occurrences of a `toJSON` that appears zero
        # times in real source, hiding it. Dot-directories use the archive list, so `.github`
        # scripts and `.claude/hooks` are scanned — they were being skipped here while
        # `_is_archived_path` counted them as live, so a live CI helper still read as dead.
        dirnames[:] = [d for d in dirnames if d.lower() not in _VERIFY_SKIP_DIRS
                       and d.lower() not in _ARCHIVE_DIRS
                       and not looks_generated_path(d + "/x")]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in _VERIFY_EXTS:
                continue
            if looks_generated_path(fname):     # `*.min.js`/`*_pb2.py` beside real source
                continue
            scanned += 1
            if scanned > _VERIFY_FILE_CAP:
                return rows, "capped"           # too big to verify — report unfiltered, and say so
            try:
                with open(os.path.join(dirpath, fname), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            # This loop already holds the file's text, so the content check is free here — and it
            # is the only signal that catches a bundle whose directory and filename both look
            # ordinary. A generated file must not VOUCH for a name: its occurrences are what hid a
            # genuinely dead `toJSON` behind 46 matches from one minified chunk.
            if looks_generated_text(text):
                continue
            for match in pattern.findall(text):
                seen[match] = seen.get(match, 0) + 1

    # A name's own definitions account for that many occurrences; anything beyond them is a use
    # the graph could not see.
    kept = [r for r in rows
            if seen.get(str(r.get("name") or ""), 0) <= definitions.get(str(r.get("name") or ""), 1)]
    return kept, "ok"


# Directories whose contents are retired or generated, named explicitly. The first version of
# this excluded EVERY dot-directory, which swept up a great deal of live code: `.claude/hooks`,
# `.storybook`, `.husky`, `.server`, `src/.internal`. Naming what is actually an archive is a
# smaller claim and a safer one — an unknown dot-directory is now assumed to be source.
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


# What the source-verification pass can and cannot claim, stated per outcome. The single old note
# blamed the file cap unconditionally, so the DEFAULT MCP call path — which omits project_root —
# told users their repo was too big when the real cause was a missing argument.
_RAW_CAVEAT = (" — these are raw call-graph results, so a function used only as a callback "
               "reference (a React handler, an `addEventListener` argument) will appear here. "
               "Confirm before deleting.")
_VERIFY_NOTES = {
    "ok": ("\n\n_Verified against the source: a candidate whose name appears anywhere beyond its "
           "own definition was dropped. A name scan still cannot see a symbol reached only "
           "through a framework — an object-literal property a library calls, a decorator "
           "registry, `getattr` dispatch, or a name in a template, YAML or TOML. Treat these as "
           "candidates and confirm before deleting._"),
    "no-root": ("\n\n_Unverified: no `project_root` was given, so the source could not be checked"
                + _RAW_CAVEAT + "_"),
    "capped": ("\n\n_Unverified: the repo exceeded the source-scan cap" + _RAW_CAVEAT + "_"),
}

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


# How long `list_projects` may take before resolution gives up. The old value was 3000ms, chosen
# against a mocked backend; the real one spawns a native binary that re-initialises its allocator
# per invocation and measured ~5.8s consistently on an ordinary machine. Every graph query resolves
# a project first, so a budget below the backend's real latency does not degrade the engine — it
# disables it. Generous on purpose: a successful lookup is cached for the process's lifetime, so
# this is paid approximately once, and being slow is enormously better than being silently wrong.
# Overridable for a machine slower still, or for a test that wants to force the timeout path.
try:
    _RESOLVE_TIMEOUT_MS = max(1, int(os.environ.get("CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS", "20000")))
except ValueError:
    _RESOLVE_TIMEOUT_MS = 20000


# The supported backend range. `codebase-memory-mcp` 0.9.x answers `query_graph`/`search_graph`
# with `{"columns": [...], "rows": [...]}`, which every renderer here parses. 0.10.x replaced that
# with a compact human-readable text format; `list_projects` stayed JSON, so project resolution and
# `doctor` still work while EVERY other op silently returns nothing. That combination is the worst
# possible: the tool looks healthy and answers "not in the graph index" about a fully indexed repo.
_SUPPORTED_BACKEND = "0.9.x"
_INCOMPATIBLE_HINT = (
    "the graph backend returned a response this release cannot parse — codebase-memory-mcp "
    f"{_SUPPORTED_BACKEND} answers with JSON rows, and 0.10.x replaced that with a text format. "
    "Pin the supported backend (pip/uv: `pip install 'codebase-memory-mcp==0.9.*'`; standalone "
    "binary: re-install the 0.9.x build) or check for a newer "
    "codeintel. This is NOT a statement about whether your repository is indexed."
)


# File extensions grouped by the language they belong to. A call edge cannot cross these groups
# without an explicit FFI/IPC mechanism, and the extractor emits no such edge type — so a callee in
# another group is a name collision, not a call.
_LANG_FAMILIES: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".ts": "ts-js", ".tsx": "ts-js", ".mts": "ts-js", ".cts": "ts-js",
    ".js": "ts-js", ".jsx": "ts-js", ".mjs": "ts-js", ".cjs": "ts-js",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".java": "jvm", ".kt": "jvm", ".scala": "jvm",
    ".c": "c-cpp", ".h": "c-cpp", ".cc": "c-cpp", ".cpp": "c-cpp", ".hpp": "c-cpp",
    ".cs": "dotnet", ".swift": "swift", ".sh": "shell", ".bash": "shell",
}


# Extensions that cannot contain a callable symbol. Deliberately an ALLOW-list of things to drop
# rather than a deny-list of things to keep: an unrecognised extension might be a language we have
# not enumerated, and silently dropping real callees would trade a visible bug for an invisible
# one. Synthetic nodes with no path (`builtins.str`) are likewise kept — they are not collisions.
_NON_CODE_EXTS = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".md", ".mdx", ".rst", ".txt", ".csv", ".tsv", ".lock", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
})


def _lang_family(path: str) -> str:
    """The language family of a file, or "" when it is unknown or the path is empty."""
    if not path:
        return ""
    return _LANG_FAMILIES.get(os.path.splitext(path)[1].lower(), "")


def _is_non_code(path: str) -> bool:
    """Whether a path is a file that cannot define a callable symbol.

    The evaluation found `pathly/features/.archive/…/RUNNER_STATE.json` reported as a callee of a
    Python function, reached because the extractor matched a bare parameter name. A JSON file has
    no callees to be."""
    if not path:
        return False
    return os.path.splitext(path)[1].lower() in _NON_CODE_EXTS


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


@dataclass(frozen=True)
class ProjectLookup:
    """A resolution attempt AND why it failed, which the caller must tell apart.

    "The backend did not answer" and "this repository is not indexed" are different facts with
    different remedies, and collapsing them is what turned a timeout into `project-not-indexed`
    plus an instruction to re-index a repository that was already indexed."""

    resolution: ProjectResolution | None
    reason: str  # "ok" | "not-indexed" | "backend-unreachable"


@dataclass(frozen=True)
class ProjectResolution:
    """How a project_root resolved to a backend project, INCLUDING whether the match was exact.

    `_resolve_project` used to return the project name alone, discarding the one fact that decides
    whether an answer is about the repository the caller asked about. `probe()` recovered it by
    re-deriving the matched root a second time; `build_result` did not, so `doctor` warned about an
    ancestor match while `code.query` answered from it silently — a human running the diagnostic
    was told, and the agent actually consuming the answers was not. Both now consume this record,
    so the two cannot drift apart again."""

    name: str
    matched_root: str | None
    scope: str  # "exact" | "ancestor"

    @property
    def is_ancestor(self) -> bool:
        return self.scope == "ancestor"


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


def _same_dir(a: str, b: str) -> bool:
    """Whether two paths name the same directory.

    `os.path.realpath` resolves symlinks but does not canonicalise CASE, and macOS APFS is
    case-insensitive: `/Users/x/Project/repo` and `/Users/x/project/repo` are one directory that
    compared as two, so a correctly-indexed repo was reported unindexed. `os.path.samefile` asks
    the filesystem, which is the only authority on this; fall back to a realpath compare when
    either path does not exist."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a).rstrip(os.sep) == os.path.realpath(b).rstrip(os.sep)


def _label_of(row: dict) -> str:
    """A row's display label: its qualified name with the backend's project id removed.

    Every place that renders a qualified name must go through here or `_display`. Fixing them one
    at a time did not work — `_display` was fixed first, `_render_scan` was missed and shipped,
    and after a test was added asserting "both renderers strip the prefix", `chain` and `pattern`
    turned out to be a third and fourth. The test now enumerates the module rather than a list of
    functions someone remembered to write down."""
    return _strip_project_prefix(str(row.get("qualified_name") or row.get("name") or "?"))


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


def _int_or_zero(value: Any) -> int:
    """A node count from an untrusted backend payload, or 0 when it is missing/not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
        # Set once the backend answers something that is not JSON — i.e. it speaks a dialect this
        # provider cannot read. Sticky for the provider's lifetime: the condition is a version
        # mismatch, not a transient, and it is the difference between "your symbol is not indexed"
        # and "your backend and this release do not agree on a wire format".
        self._saw_unparsable = False
        # Why the most recent backend call failed, or None if none did. Also declared at class
        # level (see the attribute below) for callers that bypass __init__ via __new__; set here
        # too so every entry point that DOES run __init__ starts from a known state rather than
        # the class-level default it happens to share.
        self._last_failure: Missing | None = None
        self._project_cache: dict[str, ProjectResolution] = {}   # resolved projects (stable, kept)
        self._negative_until: dict[str, float] = {}                 # failed lookups, short TTL only
        self._project_cache_lock = threading.Lock()  # concurrent HTTP requests share one provider
        self._detect_backend()

    def _detect_backend(self) -> None:
        path = shutil.which("codebase-memory-mcp")
        if path:
            self.available = True
            self._cmd: str | None = path
        else:
            self.available = False
            self._cmd = None

    # Sentinel: distinguishes "the subprocess call failed" from "it succeeded and returned JSON
    # null". Overloading None for both would make a legit null result wrongly trigger the fallback.
    _FAIL = object()
    # Sentinel: the backend ran and exited 0, but did not speak JSON — a protocol/version
    # mismatch rather than a failure. Kept separate from _FAIL so it survives to the caller.
    _UNPARSABLE = object()

    def _run(self, method: str, payload: dict, timeout_ms: int) -> Any | None:
        # Prefer PIPED STDIN — the stable, non-deprecated form the backend documents
        # (`echo '<json>' | codebase-memory-mcp cli <method>`; no deprecation warning). Fall back
        # to the deprecated raw-JSON positional arg for one release so an older backend still
        # works. The two attempts SHARE one deadline (the caller's timeout_ms) so total wall time
        # can't double. Never raises. `_run` stays the single seam existing tests patch.
        body = json.dumps(payload)
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
        out = self._run_stdin(method, body, timeout_ms)
        if out is self._UNPARSABLE:
            # Retrying the deprecated positional form would only get the same dialect back.
            self._last_failure = Missing("unparsable", "the graph backend's reply could not be read")
            return None
        if out is not self._FAIL:
            return out  # success (including a legit null) → no fallback
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            self._last_failure = Missing(
                "timeout", "the graph backend did not respond within the time budget")
            return None
        out = self._run_rawjson(method, body, remaining_ms)
        if out is self._UNPARSABLE:
            self._last_failure = Missing("unparsable", "the graph backend's reply could not be read")
            return None
        if out is self._FAIL:
            self._last_failure = Missing("backend-error", "the graph backend did not answer")
            return None
        return out

    def _run_stdin(self, method: str, body: str, timeout_ms: int) -> Any:
        if self._cmd is None:                  # backend not on PATH — nothing to exec
            return self._FAIL
        try:
            result = subprocess.run(
                [self._cmd, "cli", method],
                input=body.encode(),
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            if result.returncode != 0:
                return self._FAIL  # unsupported / error → let the raw-JSON fallback try
            try:
                return json.loads(result.stdout)
            except ValueError:
                # The backend RAN and exited 0 — it simply did not answer in JSON. That is a
                # dialect mismatch, not a failure, and it must not be folded into the same `None`
                # as a crash: `codebase-memory-mcp` 0.10.x replaced the `{columns, rows}` payload
                # this provider parses with a compact human-readable text format, so every op that
                # is not `list_projects` (still JSON) silently returned "not in graph index" — on a
                # repository that was fully indexed. Distinguishing it is what lets the caller say
                # so instead of sending the user to re-index for the third time.
                self._saw_unparsable = True
                return self._UNPARSABLE
        except Exception:
            return self._FAIL

    def _run_rawjson(self, method: str, body: str, timeout_ms: int) -> Any:
        if self._cmd is None:
            return self._FAIL
        # Deprecated-but-working bridge for older backends; remove once the live stdin test
        # (tests/test_graph_stdin.py::test_live_stdin_list_projects) is green in CI.
        try:
            result = subprocess.run(
                [self._cmd, "cli", method, body],
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            if result.returncode != 0:
                return self._FAIL
            try:
                return json.loads(result.stdout)
            except ValueError:
                self._saw_unparsable = True
                return self._UNPARSABLE
        except Exception:
            return self._FAIL

    @staticmethod
    def _match_project(raw: Any, project_root: str) -> ProjectResolution | None:
        """Resolve a list_projects response to the project for ``project_root``.

        The real codebase-memory-mcp returns ``{"projects": [...]}``; a bare list is the
        older/mocked shape — accept both. Prefer an exact ``root_path`` match; otherwise the
        LONGEST prefix match (so ``.../project/codeintel`` resolves to codeintel, not its
        parent ``.../project``). Static so ``_resolve_project`` and ``probe`` share it.

        The backend can hold MORE THAN ONE project for the same root — typically one registered
        under a short name and one under a path slug — and the two drift apart independently.
        Returning the first match meant a query could be answered from a months-stale index while
        a complete one sat beside it: observed on this repo as 1475 nodes vs 2631 for the same
        path, which is how `callers` reported a function's pre-refactor shape hours after the
        refactor. Among exact matches, prefer the most complete index."""
        # Normalize the input to an absolute realpath: the backend stores absolute root_paths, so a
        # relative ``project_root`` (e.g. `codeintel map .` passing ".") would otherwise never match
        # — the bug where the map/query silently reported "not indexed" from inside the repo.
        try:
            project_root = os.path.realpath(project_root)
        except Exception:
            pass
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return None
        exact: list[dict] = []
        best_prefix_len = -1
        best_prefix: dict | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rp = entry.get("root_path", "")
            if not rp:
                continue
            if rp == project_root:
                exact.append(entry)
                continue
            if project_root.startswith(rp.rstrip("/") + "/") and len(rp) > best_prefix_len:
                best_prefix_len = len(rp)
                best_prefix = entry

        if exact:
            # Node count is the available completeness signal — list_projects carries no indexed-at
            # timestamp, and head_sha is recorded per registration rather than per index pass, so
            # duplicates routinely report the same SHA with wildly different graphs. `max` keeps
            # the FIRST maximal entry, so with no completeness signal to go on (ties, or a backend
            # that omits `nodes`) this falls back to the original first-listed rule rather than
            # inventing an ordering.
            best = max(exact, key=lambda e: _int_or_zero(e.get("nodes")))
            name = best.get("name")
            if not name:
                return None
            return ProjectResolution(
                name=str(name), matched_root=str(best.get("root_path") or ""), scope="exact",
            )
        if best_prefix is not None:
            name = best_prefix.get("name")
            if name:
                # A prefix hit is only "ancestor" if it is genuinely a DIFFERENT directory. An
                # exact match that differed by case or a symlink lands here on the filesystems
                # where realpath cannot canonicalise it, and calling that an ancestor would refuse
                # scan ops on a correctly-indexed repo.
                root_path = str(best_prefix.get("root_path") or "")
                scope = "exact" if _same_dir(root_path, project_root) else "ancestor"
                return ProjectResolution(name=str(name), matched_root=root_path, scope=scope)
        return None

    def _lookup_project(self, project_root: str) -> ProjectLookup:
        """Resolve a root to a backend project, distinguishing "not indexed" from "could not ask".

        The timeout here used to be a hardcoded 3000ms, and the real backend takes appreciably
        longer than that: `list_projects` spawns a native binary that initialises its own allocator
        on every invocation, measured at ~5.8s CONSISTENTLY — not just on a cold start. The effect
        was total. Every graph query timed out during resolution, `_match_project` was handed
        `None`, and the caller reported `project-not-indexed` with the advice to run
        `codeintel index` — which cannot help, because the repository was already indexed. The
        entire graph engine was dead on any machine where the backend is this slow, and it said so
        in the one way guaranteed to send the user somewhere useless.

        Nothing caught it because the backend is installed in no CI job, and the one live test that
        would have failed instead SKIPPED — with "project not indexed in this environment", a
        condition produced by this very bug.
        """
        with self._project_cache_lock:
            if project_root in self._project_cache:
                return ProjectLookup(self._project_cache[project_root], "ok")
            neg_until = self._negative_until.get(project_root)
            if neg_until is not None and time.monotonic() < neg_until:
                return ProjectLookup(None, "not-indexed")  # recent genuine miss, within its TTL
        # list_projects shells out — resolve it OUTSIDE the lock so a slow backend can't serialize
        # every concurrent request. A rare duplicate lookup on first contact is harmless.
        raw = self._run("list_projects", {}, _RESOLVE_TIMEOUT_MS)
        if raw is None:
            # `_run` returns None on timeout/crash. That is NOT evidence the project is unindexed,
            # and it must not be cached as a miss: a backend having a slow moment would then be
            # remembered as "this repo has no index" for the next 30 seconds.
            return ProjectLookup(None, "backend-unreachable")
        resolution = self._match_project(raw, project_root)
        with self._project_cache_lock:
            if resolution is not None:
                self._project_cache[project_root] = resolution
                self._negative_until.pop(project_root, None)
            else:
                # Cache the MISS only briefly, so a repo indexed into the graph AFTER this failed
                # lookup is picked up within the TTL rather than staying stuck until a restart.
                self._negative_until[project_root] = time.monotonic() + 30.0
        return ProjectLookup(resolution, "ok" if resolution is not None else "not-indexed")

    def _resolve_project(self, project_root: str) -> ProjectResolution | None:
        """The resolution alone, for callers that only branch on found/not-found."""
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
                "detail": f"incompatible codebase-memory-mcp — this release needs "
                          f"{_SUPPORTED_BACKEND}, which answers queries with JSON rows; the "
                          f"installed backend replies in a text format, so every graph op except "
                          f"project resolution returns nothing",
                # Two install shapes exist and only one takes a pip command: the PyPI launcher, and
                # a standalone native binary that self-manages. Naming only pip left the binary
                # users — including this project's own maintainer — with an instruction they could
                # not run, which is the failure mode this whole check exists to avoid.
                "remediation": "downgrade the backend to 0.9.x — pip/uv installs: "
                               "`pip install 'codebase-memory-mcp==0.9.*'`; standalone binary: "
                               "re-install the 0.9.x build for your platform (note `codebase-"
                               "memory-mcp update` self-updates back to 0.10.x). Or check for a "
                               "newer codeintel that speaks the new format.",
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
            scoped = "the repo-wide ops (overview, changed, deadcode, hotspots) will refuse"
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

    # Process-wide, because the answer is a property of the INSTALLED BACKEND, not of a provider
    # instance — and providers are constructed per call in several paths. Without this, every
    # `doctor`/`status` paid an extra `query_graph` round trip against a backend that takes
    # seconds per invocation, which turned a health check into a visible stall.
    _wire_format_ok: bool | None = None
    _wire_format_lock = threading.Lock()

    # Declared at class level, not only in __init__: several call sites (and the test helpers)
    # build a provider with `GraphProvider.__new__(GraphProvider)` to stub the subprocess seam,
    # which skips __init__ entirely. An instance-only attribute then raises AttributeError deep in
    # build_result, where the never-raise handler turns it into a generic "error" — a fault
    # injected by the fix itself.
    _saw_unparsable: bool = False
    # The root the ANSWERING project is registered under, recorded per query so a renderer can
    # check what it is about to attribute. Class-level default for the same __new__ reason.
    _answered_root: str | None = None
    # Why the most recent backend call failed, or None if none did. Set at `_run` — the one seam all
    # nine ops funnel through — so a single check downstream covers the whole op population instead
    # of each op having to remember. `_run` used to collapse four distinguishable states (binary
    # absent, non-zero exit, unparsable payload, timeout) into a bare `None`, `_query_rows` turned
    # that `None` into `[]`, the ops turned `[]` into `None`, and `_op_impact` turned `None` into
    # "(none found)" — B1's exact bytes, reproduced in the graph engine after it had been fixed in
    # the LSP engine and declared closed. Fixing it per-op is what produced that miss; this is the
    # population-level equivalent.
    _last_failure: Missing | None = None
    # Parts of this answer known to be short of an answer. Graph has two real cases: a symbol-scoped
    # answer served from a CONTAINING project, and callee rows dropped as name collisions.
    _pending_gaps: tuple[dict[str, Any], ...] = ()

    def _clear_failure(self) -> None:
        """Reset the per-query failure record.

        Cleared through a method rather than an inline ``self._last_failure = None`` for the same
        reason ``lsp.py`` clears its backend error through ``_clear_backend_error`` — a lesson that
        module learned and this one then repeated. ``_dispatch`` sets the attribute as a SIDE
        EFFECT, which a type checker cannot see, so an inline assignment narrows it to ``None`` for
        the rest of the function and makes both "did a backend call fail?" checks below read as
        unreachable code. The checks are the entire point of the attribute."""
        self._last_failure = None

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
        """Forget the cached compatibility verdict.

        Process-wide caches need an explicit way back or they leak between callers — in tests, one
        real backend call would otherwise fix the verdict for every later case in the run. Also the
        hook to call if the backend is upgraded under a long-lived server."""
        with cls._wire_format_lock:
            cls._wire_format_ok = None

    def _probe_wire_format(self, project: str) -> bool | None:
        """Whether the backend answers a real QUERY in a shape this release can read.

        `list_projects` alone is not enough to judge compatibility — it is the one call that stayed
        JSON across the 0.9→0.10 change, so a probe based on it reports a perfectly healthy engine
        that cannot answer a single question. It must therefore be a genuine `query_graph`, and
        against a REAL project name: an empty or unknown project is rejected before the backend
        ever formats a response, so the reply says nothing about which dialect it speaks.
        ``None`` when the check could not run, so an unrelated hiccup is never called an
        incompatibility.
        """
        if not project:
            return None
        with GraphProvider._wire_format_lock:
            if GraphProvider._wire_format_ok is not None:
                return GraphProvider._wire_format_ok
        self._saw_unparsable = False
        raw = self._run(
            "query_graph", {"project": project, "query": "MATCH (a) RETURN a.name LIMIT 1"}, 15000,
        )
        verdict = False if self._saw_unparsable else (None if raw is None else True)
        if verdict is not None:                 # don't cache "could not tell"
            with GraphProvider._wire_format_lock:
                GraphProvider._wire_format_ok = verdict
        return verdict

    @staticmethod
    def _any_project_name(raw: Any) -> str:
        """Any registered project name, to give the wire-format probe something real to ask about."""
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return ""
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                return str(entry["name"])
        return ""

    @staticmethod
    def _project_root_of(raw: Any, name: str | None) -> str | None:
        """The ``root_path`` a list_projects response records for *name*."""
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list) or not name:
            return None
        # Among entries sharing a name, take the one `_match_project` would have chosen — the most
        # complete. Returning the first produced a false "not indexed on its own" warning on a
        # correctly-indexed repo whenever a stale duplicate registration existed.
        matches = [e for e in entries
                   if isinstance(e, dict) and e.get("name") == name and e.get("root_path")]
        if not matches:
            return None
        return str(max(matches, key=lambda e: _int_or_zero(e.get("nodes"))).get("root_path"))

    # ------------------------------------------------------------------ helpers

    def _query_rows(self, cypher: str, project: str, timeout_ms: int) -> list[dict]:
        """Run a Cypher query and return rows as column→value dicts.

        The real backend returns ``{"columns": [...], "rows": [[v, ...], ...]}`` where each row is
        a value-array aligned to ``columns``. Tolerates the legacy/mocked list-of-dicts shape and
        any malformed response by returning ``[]`` (never raises)."""
        raw = self._run("query_graph", {"project": project, "query": cypher}, timeout_ms)
        if isinstance(raw, list):
            # Legacy/mocked shape: already a list of dicts.
            return [r for r in raw if isinstance(r, dict)]
        if not isinstance(raw, dict):
            return []
        cols = raw.get("columns")
        rows = raw.get("rows")
        if not isinstance(cols, list) or not isinstance(rows, list):
            return []
        out: list[dict] = []
        for row in rows:
            if isinstance(row, list):
                out.append({str(cols[i]): row[i] for i in range(min(len(cols), len(row)))})
            elif isinstance(row, dict):
                out.append(row)
        return out

    @staticmethod
    def _display(row: dict, name_key: str, qn_key: str, file_key: str) -> str:
        name = str(row.get(name_key) or "?")
        qn = _strip_project_prefix(str(row.get(qn_key) or ""))
        file = str(row.get(file_key) or "")
        edge = str(row.get("type(c)") or "").strip()
        label = qn or name
        tail = f" ({file})" if file and file != qn else ""
        badge = f" [{edge}]" if edge else ""
        return f"- {label}{badge}{tail}"

    def _search_symbols(self, extra: dict, project: str, timeout_ms: int) -> list[dict] | None:
        """``search_graph`` → parsed result dicts. ``None`` = backend failed/malformed (→ safe-null
        upstream); ``[]`` = backend answered but nothing matched (→ an informative empty render).
        Preserving that distinction is why the repo-scan ops return a string on empty-success but
        ``None`` on can't-answer. Never raises."""
        raw = self._run("search_graph", {"project": project, **extra}, timeout_ms)
        if raw is None:
            return None
        results = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return None
        return [r for r in results if isinstance(r, dict)]

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

    @classmethod
    def _is_noise(cls, r: dict) -> bool:
        """Rows a code-quality scan should hide: builtins/generated nodes and test code (the backend's
        own ``is_test`` is unreliable — see ``_looks_like_test``). Shared by deadcode + hotspots."""
        fp = str(r.get("file_path") or "")
        return (cls._is_synthetic(fp)
                or cls._looks_like_test(fp, str(r.get("name") or ""))
                or _is_archived_path(fp))

    def _render_scan(self, kept: list[dict], title: str, cap: int, meta_fn) -> str:
        """Render a repo-scan op's markdown from filtered+sorted rows: ``## title (count)`` + one
        ``- label (file)  [meta]`` line per row (top ``cap``) + a ``+N more`` note when truncated.
        ``meta_fn(row) -> list[str]`` supplies the per-op metric badge, so deadcode/hotspots share
        the row format and truncation note (the drift-prone parts) and differ only in their metrics."""
        lines = []
        for r in kept[:cap]:
            label = _collapse_repeats(
                _strip_project_prefix(str(r.get("qualified_name") or r.get("name") or "?")))
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
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE b.name="{_cypher_literal(target)}" '
            "RETURN a.name, a.qualified_name, a.file_path, type(c) LIMIT 50"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        lines = [self._display(r, "a.name", "a.qualified_name", "a.file_path") for r in rows]
        return f"## Callers of {target} ({len(lines)})\n" + "\n".join(lines)

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
        real. What survives is marked so the caller knows the resolution is name-based.
        """
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE a.name="{_cypher_literal(target)}" '
            "RETURN b.name, b.qualified_name, b.file_path, type(c), a.file_path LIMIT 50"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None

        caller_families = {
            _lang_family(str(r.get("a.file_path") or "")) for r in rows
        } - {""}

        kept: list[dict] = []
        dropped = 0
        for r in rows:
            path = str(r.get("b.file_path") or "")
            if _is_non_code(path):
                dropped += 1          # a data/doc file cannot be a callee
                continue
            fam = _lang_family(path)
            if fam and caller_families and fam not in caller_families:
                dropped += 1          # cross-language name collision
                continue
            kept.append(r)

        if not kept:
            # `rows` was non-empty (the `if not rows: return None` above already handled the
            # genuine miss), so every row this op found was dropped by OUR OWN collision filter.
            # That is an answer WE emptied, not an absence in the repository, and routing it into
            # the `result_text is None` branch would report it as `not-in-graph` — "0 callees" —
            # when the honest statement is "N callees, all of them filtered out for a reason that
            # has nothing to do with the code". Same wording as the partial-drop note below,
            # adjusted for the total case.
            self._add_gap(
                "callees", "name-collisions-dropped",
                f"{dropped} row(s) were dropped as name collisions (a different language, or a "
                f"non-code file, than the caller); every row returned was dropped, so this may "
                f"under-report — resolution is by symbol name, not by type",
            )
            note = (f"\n\n_Resolved by symbol NAME, not by type: if more than one symbol in this "
                    f"repository is called `{target}`, their callees are merged here. Verify before "
                    f"relying on a row you did not expect._")
            return (f"## Callees of {target} (0)\n(no callee survived name-collision filtering)"
                    + note)
        lines = [self._display(r, "b.name", "b.qualified_name", "b.file_path") for r in kept]
        out = f"## Callees of {target} ({len(lines)})\n" + "\n".join(lines)
        note = (f"\n\n_Resolved by symbol NAME, not by type: if more than one symbol in this "
                f"repository is called `{target}`, their callees are merged here. Verify before "
                f"relying on a row you did not expect._")
        if dropped:
            self._add_gap(
                "callees", "name-collisions-dropped",
                f"{dropped} row(s) were dropped as name collisions (a different language, or a "
                f"non-code file, than the caller); resolution is by symbol name, not by type",
            )
            note = (f"\n\n_{dropped} row(s) dropped as name collisions (a different language, or a "
                    f"non-code file, than the caller)._" + note)
        return out + note

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
                    qn = _strip_project_prefix(str(it.get("qualified_name") or ""))
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
            parts = [f"## Changes impact ({len(files)} files → {len(syms)} symbols)"]
            if files:
                parts.append(f"### Changed files ({len(files)})")
                parts.extend(f"- {f}" for f in files[:40])
                if len(files) > 40:
                    parts.append(f"… (+{len(files) - 40} more)")
            if syms:
                parts.append(f"### Impacted symbols ({len(syms)})")
                for label, fp in syms[:40]:
                    tail = f"  ({fp})" if fp and fp != label else ""
                    parts.append(f"- {label}{tail}")
                if len(syms) > 40:
                    parts.append(f"… (+{len(syms) - 40} more)")
            return "\n".join(parts)
        except Exception:
            return None

    def _op_deadcode(self, project: str, timeout_ms: int, root: str = "") -> str | None:
        """Unreferenced non-test symbols (dead-code candidates): in-degree 0 Functions, entry points
        excluded server-side, tests/builtins filtered client-side, biggest first."""
        try:
            rows = self._search_symbols(
                {"label": "Function", "max_degree": 0, "exclude_entry_points": True, "limit": 200},
                project, timeout_ms,
            )
            if rows is None:
                return None
            # `is_entry_point` is deliberate belt-and-suspenders on top of the server-side
            # exclude_entry_points flag: it's an external-backend flag we don't independently verify,
            # and main() shown as "dead" would be a visible embarrassment — one cheap client check.
            kept = [r for r in rows if not self._is_noise(r) and not r.get("is_entry_point")]
            kept, verify_state = _drop_referenced_symbols(kept, root)
            if not kept:
                return "## Dead-code candidates\n(none found)"
            kept.sort(key=lambda r: r.get("lines") or 0, reverse=True)

            def _meta(r: dict) -> list[str]:
                m = []
                if r.get("out_degree") is not None:
                    m.append(f"out:{r.get('out_degree')}")
                if r.get("lines") is not None:
                    m.append(f"{r.get('lines')} lines")
                return m

            rendered = self._render_scan(kept, "Dead-code candidates", 30, _meta)
            return rendered + _VERIFY_NOTES.get(verify_state, "")
        except Exception:
            return None

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
                    {"label": label, "min_degree": 1, "limit": 2000}, project, timeout_ms,
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
                return safe_null_result(op_str, target_str, engine="graph", reason="unsupported-op")

            # The repo asked about is not indexed on its own; this answer would come from a project
            # that merely CONTAINS it. For a root-scoped op that is not a weaker answer to the
            # question, it is a confident answer to a different one — `deadcode` over a parent
            # directory reports symbols that are live in a sibling repo as dead. Refuse, and say
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
            if op_str in _WITHDRAWN_OPS and not _unverified_ops_enabled():
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
        if op == "deadcode":
            return self._op_deadcode(project, timeout_ms, root)
        if op == "hotspots":
            return self._op_hotspots(project, timeout_ms)
        return None
