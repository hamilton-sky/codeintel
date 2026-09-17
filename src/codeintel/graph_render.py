"""Pure path/label classification for the graph provider.

The first slice extracted from `GraphProvider` under docs/refactor-graph-provider.md: language-family
and non-code detection, and the module-scope container test. All provider-independent — no subprocess,
no state, no `self` — which is exactly why it can leave the God-class first, with zero coupling to the
transport and resolution state the test suite reaches into. `graph.py` imports what it uses; tests
import these names from here.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from codeintel.source_kind import looks_generated_path

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


# The backend has no node for code that runs at MODULE or class-body scope, so it hangs those
# references off a whole-file container node instead of off a callable symbol: a ``File`` node, whose
# qualified name it synthesises as ``<module>.__file__``, and/or a ``Module`` node named for the file.
# Rendered verbatim as an edge endpoint these assert a caller/callee that does not exist —
# ``src.click.core.__file__`` is not a function anyone can call — which is the defect this set exists
# to catch. These are the ONLY two labels that stand for a file rather than a symbol inside it; the
# distinction is derived, not asserted: on the pinned corpus every endpoint of a CALLS/USAGE edge
# carries exactly one of Function, Method, Class, Variable, Decorator, File or Module, and only File
# and Module lack a symbol identity. `test_callers_render_module_scope_as_a_location` re-derives the
# live caller-side label population and fails if the backend ever adds a container label this misses,
# which is the guard against this becoming a hand-typed list that goes stale.
_MODULE_SCOPE_LABELS = frozenset({"File", "Module"})


def _node_labels(value: Any) -> frozenset[str]:
    """The set of labels a backend row carries for a node.

    ``labels(a)`` comes back as a JSON-encoded list string (``'["File"]'``) over the real wire and as
    a plain list from the mocked/legacy backend; normalise both. Anything unparseable is treated as a
    single bare label so a lone value still classifies rather than silently vanishing."""
    if isinstance(value, (list, tuple)):
        return frozenset(str(x) for x in value)
    text = str(value or "").strip()
    if not text:
        return frozenset()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return frozenset({text})
    if isinstance(parsed, list):
        return frozenset(str(x) for x in parsed)
    return frozenset({str(parsed)})


def _is_module_scope_node(label_value: Any) -> bool:
    """Whether a displayed edge endpoint is the backend's whole-file container, not a callable symbol."""
    return bool(_node_labels(label_value) & _MODULE_SCOPE_LABELS)


def _cypher_literal(s: Any) -> str:
    """Escape a value for a double-quoted Cypher string literal — defense against a
    ``target`` containing quotes/backslashes (e.g. content an agent echoed from a repo)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _int_or_zero(value: Any) -> int:
    """Best-effort integer parsing for backend counts, which may arrive as JSON strings."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
