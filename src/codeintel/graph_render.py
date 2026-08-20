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
from typing import Any

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
