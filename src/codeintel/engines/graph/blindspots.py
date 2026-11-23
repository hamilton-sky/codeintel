"""What the graph backend is known not to see, read from the source text it was built from.

The backend binds a call to a symbol by name, through imports and a language server. Three shapes
defeat it, and each one used to come back `confidence: complete` and `safe_for_destructive: true`:

* a call through an import ALIAS — `from pkg import helper as h` then `h(2)` — has no edge to
  `helper`, so `callers helper` and `callees entry` both leave it out;
* a function that defines its own NESTED function of the target's name and calls it — the index
  merges the two definitions, so the caller comes back as a resolved caller of the outer symbol;
* (`confidence._evidence_class`) the `lsp_ts_local` strategy, which is the label the false nested
  row carried in TypeScript, and so is not a resolution on its own.

The functions here are pure: rows and file text in, facts out. The text is read through
`qualifier_scan`'s budgeted reader (`SourceReader`), never with a bare `open`, so the containment
check and the answer's read budget hold for these reads as they do for the qualifier scan.

POSITIVE EVIDENCE ONLY. A rule fires on text it read. A file that does not exist, cannot be opened
or is not plain text adds nothing: most graph tests answer about a made-up root, and an answer about
code nobody can read is not made less safe by the reading failing. The one exception is a file the
answer's read BUDGET would not let it read: that file was there to be checked and was not, so the
caller says it "could not check" rather than nothing.
"""
from __future__ import annotations

import ast
import functools
import os
import re
import time
from collections.abc import Callable

from codeintel.engines.graph import qualifier_scan

_PY_SUFFIXES = (".py", ".pyi")

# An import or export statement's braces, in JS/TS: `import { a as b }`, `export { a as b } from`,
# `import type { a as b }`, `import def, { a as b }`.
_BRACES = re.compile(r"\b(?:import|export)\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?\{([^}]*)\}")
# One `name as alias` inside braces or a Python import list.
_AS = re.compile(r"(?:^|[\s,({])(?:type\s+)?([\w$.]+)\s+as\s+([\w$]+)")
# A Python import statement, with a parenthesised list allowed to run over several lines.
_PY_IMPORT = re.compile(r"^[ \t]*(?:from\s+[\w.]+\s+)?import\s+(\([^)]*\)|[^\n]*)", re.MULTILINE)


def _is_python(path: str) -> bool:
    return path.endswith(_PY_SUFFIXES)


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def import_aliases(text: str, path: str = "") -> dict[str, str]:
    """Every name this file imports or re-exports under another name, as `{alias: original}`.

    A dotted original (`import pkg.helper as hh`) is reduced to its last segment, which is the name
    a symbol is called by. A `.py` file is read with `ast` when it parses; anything else — another
    language, or Python that does not parse — with the regexes above."""
    out: dict[str, str] = {}
    tree = _parse(text) if _is_python(path) or not path else None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.asname and alias.name != "*":
                        out[alias.asname] = alias.name.rsplit(".", 1)[-1]
        return out
    for match in _BRACES.finditer(text):
        for original, alias in _AS.findall(match.group(1)):
            out[alias] = original.rsplit(".", 1)[-1]
    for match in _PY_IMPORT.finditer(text):
        for original, alias in _AS.findall(match.group(1)):
            if original != "*":
                out[alias] = original.rsplit(".", 1)[-1]
    return out


def alias_imports(text: str, name: str, path: str = "") -> list[str]:
    """The aliases *name* is imported or re-exported under in this file: `import … name as Y`,
    `from … import name as Y`, `{ name as Y }` and `export { name as Y }`."""
    return sorted(alias for alias, original in import_aliases(text, path).items()
                  if original == name and alias != name)


def _python_span(tree: ast.Module, name: str, qualified: str) -> tuple[int, int] | None:
    """The lines of the `def name` whose nesting path best matches *qualified*."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                path = f"{prefix}.{child.name}" if prefix else child.name
                if child.name == name and not isinstance(child, ast.ClassDef):
                    found.append((path, child))
                walk(child, path)
            else:
                walk(child, prefix)

    walk(tree, "")
    if not found:
        return None
    best = next((node for path, node in found
                 if qualified == path or qualified.endswith("." + path)), None)
    node = best if best is not None else found[0][1]
    return node.lineno, node.end_lineno or node.lineno


def _python_span_by_indent(lines: list[str], name: str) -> tuple[int, int] | None:
    head = re.compile(rf"^([ \t]*)(?:async\s+)?def\s+{re.escape(name)}\b")
    for i, line in enumerate(lines):
        match = head.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        end = i
        for j in range(i + 1, len(lines)):
            stripped = lines[j].strip()
            if not stripped:
                continue
            if len(lines[j]) - len(lines[j].lstrip()) <= indent:
                break
            end = j
        return i + 1, end + 1
    return None


def _brace_span(text: str, name: str, qualified: str) -> tuple[int, int] | None:
    """A JS/TS function or method's lines: from its header to the brace that closes its body."""
    start = 0
    parts = [p for p in qualified.split(".") if p]
    if len(parts) >= 2:
        owner = re.search(rf"\bclass\s+{re.escape(parts[-2])}\b", text)
        if owner:
            start = owner.start()
    leaf = re.escape(name)
    header = re.compile(
        rf"\bfunction\s*\*?\s*{leaf}\b"
        rf"|\b(?:const|let|var)\s+{leaf}\s*="
        rf"|^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
        rf"{leaf}\s*(?:<[^>\n]*>)?\s*\(",
        re.MULTILINE)
    match = header.search(text, start) or header.search(text)
    if not match:
        return None
    opening = text.find("{", match.end())
    if opening < 0:
        return None
    depth = 0
    for i in range(opening, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text.count("\n", 0, match.start()) + 1, text.count("\n", 0, i) + 1
    return None


def function_span(text: str, name: str, path: str = "", qualified: str = "") -> tuple[int, int] | None:
    """The first and last line (1-based, inclusive) of the function *name* in this file, or `None`.

    *qualified* is the dotted name the index gave it, used to choose between same-named functions
    (`Circle.area` and `Square.area`). Python is read with `ast`, and by indentation when the file
    does not parse; anything else by its braces."""
    if not name:
        return None
    if _is_python(path):
        tree = _parse(text)
        if tree is not None:
            return _python_span(tree, name, qualified)
        return _python_span_by_indent(text.splitlines(), name)
    return _brace_span(text, name, qualified)


def _body(text: str, span: tuple[int, int]) -> str:
    """The function's lines after its header line."""
    lines = text.splitlines()
    return "\n".join(lines[span[0]:span[1]])


def _call_of(name: str) -> re.Pattern[str]:
    # A bare call of the name, not an attribute (`x.name(`) and not a longer identifier.
    return re.compile(rf"(?<![\w$.]){re.escape(name)}\s*\(")


def nested_shadow(text: str, caller_span: tuple[int, int], name: str) -> bool:
    """Whether the caller in *caller_span* defines its own `name` before it calls `name`.

    Python is read with `ast`: a nested `def name` and a later bare call of `name` inside the same
    function. Text that does not parse as Python — TypeScript, or broken Python — is read with the
    regex `(def|function)\\s+name\\b` inside the caller's body, followed by a call of `name`."""
    tree = _parse(text)
    if tree is not None:
        caller = next((node for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.lineno == caller_span[0]), None)
        if caller is not None:
            defined = [node.lineno for node in ast.walk(caller)
                       if node is not caller
                       and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                       and node.name == name]
            if not defined:
                return False
            first = min(defined)
            return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id == name and node.lineno > first
                       for node in ast.walk(caller))
    body = _body(text, caller_span)
    definition = re.search(rf"\b(?:def|function)\s+{re.escape(name)}\b", body)
    if definition is None:
        return False
    return _call_of(name).search(body, definition.end()) is not None


def alias_calls(text: str, span: tuple[int, int], aliases: dict[str, str]) -> dict[str, str]:
    """Of *aliases* (`{alias: original}`), the ones the body in *span* calls bare: `alias(`."""
    body = _body(text, span)
    return {alias: original for alias, original in aliases.items()
            if _call_of(alias).search(body)}


def interface_declares(text: str, interface: str, member: str) -> bool:
    """Whether the TypeScript `interface <interface>` in this file declares a member `member`.

    Read by text: the interface's header (`interface Shape`, with any generics or `extends`), the
    braces of its body, and in that body a member written `member(`, `member:`, `member?` or
    `member<`, optionally `readonly`. A type alias (`type Shape = { … }`) is not read."""
    if not interface or not member:
        return False
    header = re.compile(rf"\binterface\s+{re.escape(interface)}\b[^{{;]*\{{")
    declared = re.compile(
        rf"(?:^|[;{{,\s])(?:readonly\s+)?{re.escape(member)}\s*\??\s*[(:<]")
    for match in header.finditer(text):
        depth = 0
        for i in range(match.end() - 1, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    if declared.search(text[match.end():i]):
                        return True
                    break
    return False


class SourceReader:
    """One answer's reads of source files, through `qualifier_scan`'s budgeted reader.

    `read` returns a file's text, or `None` when it is not to be judged: outside the root, missing,
    unreadable, too large, not plain text — or past the budget. Only that last case is remembered,
    in `unchecked`, because it is the only one where a file that was there to check went unchecked.
    A root that does not exist is not a root, and nothing under it is unchecked."""

    def __init__(self, root: str, budget: qualifier_scan.Budget) -> None:
        self._root = root
        self._budget = budget
        self._real: str | None = None
        self._resolved = False
        self._texts: dict[str, str | None] = {}
        self.unchecked: list[str] = []

    def read(self, rel: str) -> str | None:
        if rel not in self._texts:
            self._texts[rel] = self._load(rel)
        return self._texts[rel]

    def _refuse(self, rel: str) -> None:
        if rel not in self.unchecked:
            self.unchecked.append(rel)

    def _timed(self, call: Callable[[], object]) -> tuple[object, bool]:
        began = time.monotonic()
        value, finished = qualifier_scan._within(call, self._budget)
        self._budget.spend(time.monotonic() - began)
        if not finished:
            self._budget.exhaust()
        return value, finished

    def _load(self, rel: str) -> str | None:
        if not rel or not self._root:
            return None
        budget = self._budget
        if not self._resolved:
            if budget.exhausted or qualifier_scan._stalled_reads() >= qualifier_scan._MAX_STALLED:
                self._refuse(rel)
                return None
            real, finished = self._timed(functools.partial(qualifier_scan._root, self._root))
            if not finished:
                self._refuse(rel)
                return None
            self._resolved = True
            self._real = real if isinstance(real, str) else None
        if self._real is None:
            return None
        if (budget.exhausted or qualifier_scan._stalled_reads() >= qualifier_scan._MAX_STALLED
                or not budget.take_file()):
            self._refuse(rel)
            return None
        blob, finished = self._timed(functools.partial(
            qualifier_scan._read, self._real, os.path.join(self._root, rel), budget))
        if not finished:
            self._refuse(rel)
            return None
        if not isinstance(blob, bytes):
            if budget.out_of_bytes:
                self._refuse(rel)
            return None
        if not qualifier_scan._plain_text(blob):
            return None
        return blob.decode("utf-8", "replace")
