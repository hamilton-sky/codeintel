"""Which definitions an edit removed, re-signed or rewrote — from the old and new text of one file.

`code.query op=changed` could say which FILES an edit touched and nothing finer, so appending one
function to an 80-symbol file reported "80 symbols impacted", and a function that was DELETED could
not be asked about at all — the graph indexed at HEAD has no node for it. The question a reviewer
actually has is "who uses the functions this branch removes or rewrites?", and it cannot be put
until something has said which functions those are. This module says it, and only that.

PURE, on purpose: two strings in, a classification out, no I/O and no subprocess. The git plumbing
that fetches the two strings lives in `changed_range.py`, so every behaviour here is a unit test
over literals.

Four classifications, keyed by qualified name (`Class.method`, no module prefix — the file is
carried beside it):

    removed    present before, gone now
    signature  the header moved: parameters, return annotation, decorators, sync/async. A caller
               can stop fitting.
    body       the header is equal and the implementation is not. A caller's behaviour can move.
    added      new. Never a "use" risk; listed so a caller that is itself new is not mistaken for
               one the diff left alone.

A moved FILE (`git diff -M`, R-status) needs no special case: names are module-relative, so the same
definitions on both sides of a rename compare equal and report nothing. A function moved between
two DIFFERENT files is `removed` in one and `added` in the other — and says so only in the caller's
hands, because deciding that two such definitions are "the same function" is a guess this module
does not make.

THREE THINGS IT DECLINES TO DO, because the alternative is a classification nobody checked:

* It never raises. A file that does not parse comes back `unparsable` with the reason, and a
  language it cannot name definitions in comes back `unsupported-language`. Both carry no
  `changes`: "we could not look" is a different claim from "nothing changed", and an empty list
  would read as the second.
* It does not guess at languages. Python is read with `ast`. Everything else goes through the same
  tree-sitter tables the indexer chunks with (`indexer._TS_*`), so the definition types are the
  ones the rest of the product already agreed on — and a language whose definitions the indexer
  cannot NAME (C and C++ keep the name in a declarator, which `_ts_node_name` does not read) is
  reported unsupported rather than diffed by position.
* It ignores whitespace and comments (Python's `ast` never sees them; the tree-sitter path drops
  `comment` leaves), so a reformat is not a rewrite. A docstring is part of a Python body, so
  editing one reports `body` — flagged `docstring only` so a reader can discount it.
"""
from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

REMOVED = "removed"
SIGNATURE = "signature"
BODY = "body"
ADDED = "added"

# Most severe first: the order the answer lists symbols in, and the order a cap keeps them in. A
# removed symbol can leave a caller with nothing to call, a signature change can make it call
# wrongly, and a body change only moves what it gets back.
SEVERITY: dict[str, int] = {REMOVED: 0, SIGNATURE: 1, BODY: 2, ADDED: 3}

STATUS_OK = "ok"
STATUS_UNPARSABLE = "unparsable"
STATUS_UNSUPPORTED = "unsupported-language"

# What the tree-sitter path can NAME definitions in — narrower than `indexer._TS_LANG_BY_EXT` on
# purpose. C and C++ are mapped there for chunking, which needs spans and not names; `_ts_node_name`
# reads a `name` field, and a C `function_definition` carries its name inside a declarator, so every
# one of its definitions would come back unnamed and the diff would report "no symbols changed" for
# a file whose functions were all rewritten.
_TS_SUPPORTED = frozenset({"typescript", "tsx", "javascript", "go", "rust", "java"})

_PYTHON_EXTS = (".py", ".pyi")


@dataclass(frozen=True)
class SymbolChange:
    """One definition that moved, and how."""

    qualified_name: str
    change: str                       # REMOVED | SIGNATURE | BODY | ADDED
    kind: str                         # "function" | "method" | "class"
    old_line: int | None
    new_line: int | None
    # What part moved: for `signature`, which header facet(s) (`parameters (+x, -y)`, `return
    # annotation`, `decorators`, `async`); for `body`, `docstring only` when nothing else did.
    facets: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileSymbolDiff:
    """The classification for one file, or the reason there is none."""

    path: str
    status: str                       # STATUS_OK | STATUS_UNPARSABLE | STATUS_UNSUPPORTED
    language: str
    changes: tuple[SymbolChange, ...] = ()
    detail: str = ""
    # Definitions the file declares without a usable name (an anonymous default export). They are
    # not compared, and the count is what lets a caller say so rather than imply it compared them.
    unnamed: int = 0


@dataclass
class _One:
    """One definition occurrence. A qualified name can have several (a property and its setter, an
    overload set, a function defined in both arms of an `if`), so the table maps to lists."""

    kind: str
    header: tuple[tuple[str, object], ...]
    body: tuple
    plain_body: tuple                 # the body without a leading docstring
    line: int
    params: tuple[str, ...] = ()


class _Unparsable(Exception):
    pass


class _Unsupported(Exception):
    pass


def language_of(path: str) -> str:
    """The language a path is diffed as, or `""` when this module has no reading of it."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _PYTHON_EXTS:
        return "python"
    # Imported here and not at module level: the indexer pulls in numpy and sqlite-vec (~0.8s), and
    # a diff of Python files — the common case — has no use for either.
    from codeintel.indexer import _TS_LANG_BY_EXT
    return _TS_LANG_BY_EXT.get(ext, "")


def diff_file(path: str, old_source: str | None, new_source: str | None) -> FileSymbolDiff:
    """Classify every definition that differs between *old_source* and *new_source*.

    `None` means the file does not exist on that side: an added file has no old text, a deleted one
    no new text. `path` is the file's name on the side that exists. Never raises."""
    language = ""
    try:
        language = language_of(path)
        if language != "python" and language not in _TS_SUPPORTED:
            shown = language or (os.path.splitext(path)[1].lower() or "unknown")
            return FileSymbolDiff(
                path, STATUS_UNSUPPORTED, language,
                detail=f"no definition-level reading of `{shown}` files")
        extract = _python_definitions if language == "python" else (
            lambda text: _treesitter_definitions(text, language))
        unnamed = 0
        old_defs: dict[str, list[_One]] = {}
        new_defs: dict[str, list[_One]] = {}
        for side, text in (("old", old_source), ("new", new_source)):
            if text is None:
                continue
            try:
                got = extract(text)
            except _Unparsable as exc:
                return FileSymbolDiff(
                    path, STATUS_UNPARSABLE, language, detail=f"the {side} version: {exc}")
            table, skipped = got
            unnamed = max(unnamed, skipped)
            (old_defs if side == "old" else new_defs).update(table)
        changes = [c for qn in sorted(set(old_defs) | set(new_defs))
                   if (c := _classify(qn, old_defs.get(qn), new_defs.get(qn))) is not None]
        changes.sort(key=lambda c: (SEVERITY[c.change], c.new_line or c.old_line or 0))
        return FileSymbolDiff(path, STATUS_OK, language, tuple(changes), unnamed=unnamed)
    except _Unsupported as exc:
        return FileSymbolDiff(path, STATUS_UNSUPPORTED, language, detail=str(exc))
    except Exception as exc:
        return FileSymbolDiff(
            path, STATUS_UNPARSABLE, language, detail=f"{type(exc).__name__}: {exc}"[:200])


# ------------------------------------------------------------------------------------ comparing

def _classify(
    qn: str, old: list[_One] | None, new: list[_One] | None
) -> SymbolChange | None:
    if old and not new:
        return SymbolChange(qn, REMOVED, old[0].kind, old[0].line, None)
    if new and not old:
        return SymbolChange(qn, ADDED, new[0].kind, None, new[0].line)
    if not old or not new:
        return None
    kind, old_line, new_line = new[0].kind, old[0].line, new[0].line
    if len(old) != len(new):
        # An overload added, or a conditional redefinition dropped: the set of ways to call this
        # name changed even though no single header can be said to have.
        return SymbolChange(
            qn, SIGNATURE, kind, old_line, new_line, ("number of definitions under this name",))
    facets: list[str] = []
    for o, n in zip(old, new, strict=True):
        facets.extend(_header_facets(o, n))
    if facets:
        return SymbolChange(qn, SIGNATURE, kind, old_line, new_line, tuple(dict.fromkeys(facets)))
    if any(o.body != n.body for o, n in zip(old, new, strict=True)):
        doc_only = all(o.plain_body == n.plain_body for o, n in zip(old, new, strict=True))
        # A class has no "implementation": what moved is its own statements — attributes, a
        # dataclass field — which is a different thing to review than a function that now behaves
        # differently, so it is named rather than left to read as the same kind of rewrite.
        notes: tuple[str, ...] = ("docstring only",) if doc_only else (
            ("class-level statements",) if kind == "class" else ())
        return SymbolChange(qn, BODY, kind, old_line, new_line, notes)
    return None


def _header_facets(o: _One, n: _One) -> list[str]:
    if o.kind != n.kind:
        # `def Foo` becoming `class Foo`: the two headers have different shapes (a function's is
        # five facets, a class's three), so there is nothing to compare facet by facet, and trying
        # raised and made the WHOLE file `unparsable`. A different KIND of definition is the largest
        # signature change there is — every caller was written against the other one.
        return [f"kind changed ({o.kind} → {n.kind})"]
    out = []
    for (label, before), (_, after) in zip(o.header, n.header, strict=True):
        if before == after:
            continue
        if label == "parameters":
            added = [p for p in n.params if p not in o.params]
            dropped = [p for p in o.params if p not in n.params]
            delta = ", ".join([f"-{p}" for p in dropped] + [f"+{p}" for p in added])
            out.append(f"parameters ({delta})" if delta else "parameters (defaults or annotations)")
        else:
            out.append(label)
    return out


# --------------------------------------------------------------------------------------- python

def _is_docstring(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _dump_all(nodes: Iterable[ast.AST]) -> tuple[str, ...]:
    return tuple(ast.dump(n) for n in nodes)


def _py_function(node: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool) -> _One:
    args = node.args
    every = [*args.posonlyargs, *args.args, *([args.vararg] if args.vararg else []),
             *args.kwonlyargs, *([args.kwarg] if args.kwarg else [])]
    body = _dump_all(node.body)
    return _One(
        kind="method" if in_class else "function",
        header=(
            ("async", isinstance(node, ast.AsyncFunctionDef)),
            ("parameters", ast.dump(args)),
            ("return annotation", ast.dump(node.returns) if node.returns else ""),
            ("decorators", _dump_all(node.decorator_list)),
            ("type parameters", _dump_all(getattr(node, "type_params", ()))),
        ),
        body=body,
        plain_body=body[1:] if node.body and _is_docstring(node.body[0]) else body,
        line=node.lineno,
        params=tuple(a.arg for a in every),
    )


def _py_class(node: ast.ClassDef) -> _One:
    # The class's own statements only. Its methods are separate definitions with their own
    # classification, and counting them here would report a class as "rewritten" whenever any
    # method was — a heading for every edit to a file's biggest class.
    own = [s for s in node.body
           if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    body = _dump_all(own)
    return _One(
        kind="class",
        header=(
            ("bases", _dump_all(node.bases)),
            ("keywords", _dump_all(node.keywords)),
            ("decorators", _dump_all(node.decorator_list)),
        ),
        body=body,
        plain_body=body[1:] if own and _is_docstring(own[0]) else body,
        line=node.lineno,
    )


def _child_blocks(node: ast.AST) -> Iterable[list[ast.stmt]]:
    """The statement lists nested directly under a compound statement (`if`/`try`/`with`/`for`/
    `while`/`match`), including `except` and `case` arms."""
    for _, value in ast.iter_fields(node):
        if not isinstance(value, list):
            continue
        stmts = [v for v in value if isinstance(v, ast.stmt)]
        if stmts:
            yield stmts
        for v in value:
            if isinstance(v, (ast.ExceptHandler, ast.match_case)):
                yield v.body


def _python_definitions(source: str) -> tuple[dict[str, list[_One]], int]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        reason = f"line {exc.lineno}: {exc.msg}" if isinstance(exc, SyntaxError) else (
            f"{type(exc).__name__}: {exc}")
        raise _Unparsable(f"does not parse as Python ({reason})"[:200]) from exc
    out: dict[str, list[_One]] = {}

    def visit(stmts: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A nested function is part of its parent's body and is not entered: it is a
                # closure, not something another module can name.
                out.setdefault(prefix + node.name, []).append(_py_function(node, in_class))
            elif isinstance(node, ast.ClassDef):
                qn = prefix + node.name
                out.setdefault(qn, []).append(_py_class(node))
                visit(node.body, qn + ".", True)
            else:
                # Definitions under `if TYPE_CHECKING:`, `try: ... except ImportError:` and the like
                # are still module-level definitions.
                for block in _child_blocks(node):
                    visit(block, prefix, in_class)

    visit(tree.body, "", False)
    return out, 0


# -------------------------------------------------------------------------------- tree-sitter

def _tokens(node, stop: int | None = None) -> tuple[str, ...]:
    """The non-comment leaf tokens of a node, so a reformat or a re-commented body is no change."""
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if "comment" in n.type:
            continue
        if n.child_count == 0:
            if stop is None or n.start_byte < stop:
                text = n.text
                out.append(text.decode("utf-8", "replace") if text is not None else n.type)
        else:
            stack.extend(reversed(n.children))
    return tuple(out)


def _ts_function_value(node):
    """The function node a `const f = () => {...}` declaration binds, or the node itself."""
    for child in node.children:
        if child.type == "variable_declarator":
            for grand in child.children:
                if grand.type in {"arrow_function", "function_expression", "function",
                                  "generator_function"}:
                    return grand
    return node


def _ts_one(node, is_method: bool) -> _One:
    target = _ts_function_value(node)
    body = target.child_by_field_name("body")
    tail: tuple[str, ...] = ()
    if body is None:
        header = _tokens(node)
    else:
        header, tail = _tokens(node, stop=body.start_byte), _tokens(body)
    return _One(
        kind="method" if is_method else "function",
        header=(("declaration header", header),),
        body=tail,
        plain_body=tail,
        line=node.start_point[0] + 1,
    )


def _ts_container_name(node, lang: str, name_of) -> str | None:
    if lang == "rust" and node.type == "impl_item":
        # `impl Foo { ... }` has no `name` field; the type it is implemented for is the qualifier
        # its methods are written under.
        ty = node.child_by_field_name("type")
        if ty is not None:
            inner = ty.child_by_field_name("type") if ty.type == "generic_type" else None
            text = (inner or ty).text
            return text.decode("utf-8", "replace") if text else None
        return None
    return name_of(node)


def _go_receiver(node) -> str | None:
    """`Foo` for `func (f *Foo) Bar()`: Go declares methods at the top level, qualified by receiver."""
    recv = node.child_by_field_name("receiver")
    if recv is None:
        return None
    stack = [recv]
    while stack:
        n = stack.pop()
        if n.type == "type_identifier" and n.text:
            return n.text.decode("utf-8", "replace")
        stack.extend(reversed(n.children))
    return None


def _treesitter_definitions(source: str, lang: str) -> tuple[dict[str, list[_One]], int]:
    # The definition types and the naming rule are the indexer's own — one reading of "what is a
    # definition" per language across chunking, symbol attribution and this diff.
    from codeintel.indexer import (
        _TS_ARROW_LANGS,
        _TS_CONTAINER_TYPES,
        _TS_DECL_TYPES,
        _TS_FUNC_TYPES,
        Indexer,
        _ts_decl_is_function,
    )
    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(lang)
    except Exception as exc:
        raise _Unsupported(f"the tree-sitter grammar for {lang} is unavailable ({exc})"[:200]) from exc
    try:
        root = parser.parse(source.encode("utf-8", errors="replace")).root_node
    except Exception as exc:
        raise _Unparsable(f"tree-sitter could not parse it ({exc})"[:200]) from exc
    has_error = root.has_error() if callable(root.has_error) else root.has_error
    if has_error:
        # Error-tolerant parsing returns a partial tree for broken code. Diffing a partial tree
        # would classify whatever happened to survive the error, which is a guess.
        raise _Unparsable("tree-sitter reports syntax errors in it")

    func_types = _TS_FUNC_TYPES.get(lang, set())
    cont_types = _TS_CONTAINER_TYPES.get(lang, set())
    arrow_ok = lang in _TS_ARROW_LANGS
    name_of = Indexer._ts_node_name
    out: dict[str, list[_One]] = {}
    unnamed = 0
    stack: list[tuple[Any, str]] = [(root, "")]
    while stack:
        node, prefix = stack.pop()
        for child in reversed([c for c in node.children if c.is_named]):
            is_func = child.type in func_types or (
                arrow_ok and child.type in _TS_DECL_TYPES and _ts_decl_is_function(child))
            if child.type in cont_types:
                name = _ts_container_name(child, lang, name_of)
                if not name:
                    unnamed += 1
                    stack.append((child, prefix))
                    continue
                qn = prefix + name
                body = child.child_by_field_name("body")
                header = _tokens(child, stop=body.start_byte) if body is not None else _tokens(child)
                out.setdefault(qn, []).append(_One(
                    kind="class", header=(("declaration header", header),), body=(),
                    plain_body=(), line=child.start_point[0] + 1))
                stack.append((child, qn + "."))
            elif is_func:
                name = name_of(child)
                if not name:
                    unnamed += 1
                    continue
                is_go_method = lang == "go" and child.type == "method_declaration"
                recv = _go_receiver(child) if is_go_method else None
                qn = f"{recv}.{name}" if recv else prefix + name
                out.setdefault(qn, []).append(_ts_one(child, bool(prefix) or is_go_method))
            else:
                stack.append((child, prefix))
    return out, unnamed


# ---------------------------------------------------------------------------------- mentions

def classify_mentions(source: str, name: str, lines: Iterable[int]) -> dict[int, str]:
    """For a PYTHON file, what each line that mentions `name` is: `definition`, `code`, `string` or
    `comment`.

    The brief for a removed function's survivors was to exclude comments and strings "only if you
    can do so reliably". For Python it can be told reliably — the parser knows what is a name and
    what is text — but EXCLUDING is the wrong use of that knowledge: `getattr(obj, "name")`,
    `__all__ = ["name"]` and `mock.patch("pkg.name")` are strings that break when `name` goes. So
    every mention is kept and labelled, and a reader (or a sort) discounts the comments.

    A file that does not parse returns `{}`: no label is a statement that nothing is known, not that
    the line is code."""
    wanted = list(lines)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    code: set[int] = set()
    defs: set[int] = set()
    text: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                defs.add(node.lineno)
        elif isinstance(node, ast.Name):
            if node.id == name:
                code.add(node.lineno)
        elif isinstance(node, ast.Attribute):
            if node.attr == name:
                code.add(getattr(node, "end_lineno", None) or node.lineno)
        elif isinstance(node, ast.alias):
            if name in (node.name.rsplit(".", 1)[-1], node.asname):
                code.add(getattr(node, "lineno", 0))
        elif isinstance(node, (ast.arg, ast.keyword)):
            if node.arg == name:
                code.add(getattr(node, "lineno", 0))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and word.search(node.value)):
            first = node.lineno
            text.update(range(first, (getattr(node, "end_lineno", None) or first) + 1))
    out: dict[int, str] = {}
    for line in wanted:
        if line in defs:
            out[line] = "definition"
        elif line in code:
            out[line] = "code"
        elif line in text:
            out[line] = "string"
        else:
            out[line] = "comment"
    return out
