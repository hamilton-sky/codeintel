"""Ground truth for "what references this Python symbol, and how" — from the AST, with abstention.

Why this exists. Every claim made about the graph engine's accuracy so far in this project has
rested on a handful of hand-checked symbols. That was enough to find real defects and not remotely
enough to choose between engines: two reasonable people looked at the same evidence and reached
opposite designs, twice. A benchmark is the only thing that turns those arguments into arithmetic.

Why it can be trusted. The obvious trap is circularity — if the oracle is itself a call-graph
resolver, then scoring engines against it measures agreement with a third resolver, not correctness.
This avoids that in one specific way: **it labels only what the file itself makes unambiguous, and
abstains on everything else.** A direct import plus a direct call is decidable from the syntax and
the file's own import table, with no inference. An attribute call on a value (`self.thing.run()`), a
name re-exported through a package `__init__`, anything dynamic — those are *not* labelled, they are
recorded as `undecidable` and excluded from scoring.

That makes the coverage explicitly partial, which is the point. A benchmark that labelled every site
would be asserting a resolver's opinion as truth; this one asserts only what is mechanically certain
and tells you what fraction of sites it declined to judge. Precision and recall are then computed
over a population where the answer really is known.

The labels are relationship KINDS, not confidences, because that distinction is what the engine
under test kept getting wrong:

    call         the bound name is the callee of a Call node          -> CALLS
    reference    the bound name appears, but not as a callee: passed  -> CALL_REFERENCE / USAGE
                 as an argument, assigned, decorated with, annotated
    import       the binding site itself                              -> not a caller at all
    undecidable  the site mentions the name but the syntax does not
                 settle what it binds to                             -> excluded from scoring
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field

CALL = "call"
REFERENCE = "reference"
IMPORT = "import"
UNDECIDABLE = "undecidable"

_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".tox", "site-packages", ".serena",
})


@dataclass(frozen=True)
class Site:
    """One place a symbol is referenced, and what the syntax says it is."""

    file: str
    line: int
    enclosing: str          # dotted path of the nearest def/class, or "<module>"
    kind: str               # CALL | REFERENCE | IMPORT | UNDECIDABLE
    why: str                # the evidence, so a human can audit any single row

    def key(self) -> tuple[str, str]:
        """The identity an engine's answer is compared on.

        `(file, enclosing symbol)` rather than `(file, line)`: "who calls this" is a question about
        symbols, every engine answers in symbols, and the graph engine does not report lines at all.
        Two calls from the same function are one caller."""
        return (self.file, self.enclosing)


@dataclass
class FileVerdict:
    sites: list[Site] = field(default_factory=list)
    parse_failed: bool = False


def _walk_py(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        out.extend(os.path.join(dirpath, fn)
                   for fn in filenames if fn.endswith(".py"))
    return sorted(out)


def _module_of(path: str, root: str) -> str:
    """The module name this file is imported AS — not its path relative to the repo.

    These differ, and assuming they are the same is what made the oracle's first run report zero
    callers for a function with five. In a `src/` layout the source root is not a package, so
    `src/pkg/mod.py` is imported as `pkg.mod`; naming it `src.pkg.mod` means no import statement in
    the repository ever matches and every site becomes undecidable.

    The source root is found by walking UP from the file while each directory is a package (has an
    `__init__.py`) and stopping at the first that is not. That is arithmetic on the tree, not a
    heuristic — and it is the same defect class the upstream backend has open for aliased imports
    across a non-root source root (DeusData/codebase-memory-mcp#1390), which is a good sign it is
    worth getting right here rather than inheriting.
    """
    parts: list[str] = []
    d = os.path.dirname(os.path.abspath(path))
    stem = os.path.basename(path)[:-3] if path.endswith(".py") else os.path.basename(path)
    root_abs = os.path.abspath(root)
    while d.startswith(root_abs) and os.path.exists(os.path.join(d, "__init__.py")):
        parts.insert(0, os.path.basename(d))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if stem != "__init__":
        parts.append(stem)
    return ".".join(parts) if parts else stem


def _reexport_map(root: str, files: list[str]) -> dict[str, dict[str, tuple[str, str]]]:
    """`module -> {local name: (source module, source name)}` for top-level re-exports.

    A re-export is not a guess. When `flow_defs.py` says `from .flow_graph_ops import
    ensure_adapter_map_default`, then `flow_defs.ensure_adapter_map_default` IS
    `flow_graph_ops.ensure_adapter_map_default` — that is arithmetic on an explicit statement, the
    same class of fact as the original import, and following it is what a correct resolver does. What
    must be avoided is guessing (matching bare names, matching suffixes); transitive *stated* imports
    are the opposite of that.

    It matters in practice rather than in theory: without this the oracle judged 17% of one symbol's
    sites and abstained on the rest, because every caller reached it through exactly this pattern —
    which is ordinary Python, not an edge case. Only module-level imports count; a function-local
    import binds inside that function and cannot re-export.
    """
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError):
            continue
        mod = _module_of(path, root)
        binder_mod = mod
        table: dict[str, tuple[str, str]] = {}
        for node in tree.body:                     # top level only
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                parts = binder_mod.split(".")
                base = parts[: len(parts) - node.level]
                src = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                src = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                table[alias.asname or alias.name] = (src, alias.name)
        if table:
            out[mod] = table
    return out


_ALIAS_DEPTH = 4          # deeper chains exist; a bound keeps a cycle from hanging the run


def _alias_set(target: str, reexports: dict[str, dict[str, tuple[str, str]]]) -> set[tuple[str, str]]:
    """Every `(module, name)` that provably denotes *target*, via stated re-exports."""
    tmod, _, tname = target.rpartition(".")
    known = {(tmod, tname)}
    for _ in range(_ALIAS_DEPTH):
        grew = False
        for mod, table in reexports.items():
            for local, origin in table.items():
                if origin in known and (mod, local) not in known:
                    known.add((mod, local))
                    grew = True
        if not grew:
            break
    return known


class _Binder(ast.NodeVisitor):
    """Local names in one module that provably bind to *target*.

    Only the two import forms whose meaning is fixed by the statement itself are honoured:
    ``from pkg.mod import name [as alias]`` and ``import pkg.mod [as alias]``. A relative import is
    resolved against the file's own package, which is arithmetic on the path rather than a guess.

    Deliberately NOT honoured: a star import (which name it introduced is not in the file), a
    conditional or `try`-guarded import (which branch ran is a runtime fact), or a name reassigned
    later. Those make the module undecidable for this target and it is skipped — the alternative is
    the oracle inventing a binding, which is the failure it exists to avoid.
    """

    def __init__(self, target: str, module: str,
                 aliases: set[tuple[str, str]] | None = None) -> None:
        self.target = target                      # e.g. "pkg.mod.func"
        self.module = module
        self.tmod, _, self.tname = target.rpartition(".")
        # Every (module, name) pair that denotes the target, the original included.
        self.aliases = aliases or {(self.tmod, self.tname)}
        self.local_names: set[str] = set()        # bare names bound to the target
        self.module_aliases: set[str] = set()     # aliases of the target's MODULE
        self.import_lines: set[int] = set()
        self.opaque = False                       # a star import: give up on this file
        self.reassigned: set[str] = set()

    def _resolve_relative(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = self.module.split(".")
        base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
        # `from . import x` inside pkg.mod means pkg.x — drop the module's own last segment.
        base = base[:-1] if node.level >= 1 else base
        return ".".join([*base, node.module]) if node.module else ".".join(base)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = self._resolve_relative(node)
        for alias in node.names:
            if alias.name == "*":
                self.opaque = True
                continue
            if (mod, alias.name) in self.aliases:
                self.local_names.add(alias.asname or alias.name)
                self.import_lines.add(node.lineno)
            elif f"{mod}.{alias.name}" == self.tmod:
                self.module_aliases.add(alias.asname or alias.name)
                self.import_lines.add(node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == self.tmod:
                self.module_aliases.add(alias.asname or alias.name.split(".")[0])
                self.import_lines.add(node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # A local name rebound after import no longer means what the import said.
        for t in node.targets:
            if isinstance(t, ast.Name):
                self.reassigned.add(t.id)
        self.generic_visit(node)


def _enclosing_map(tree: ast.AST) -> dict[int, str]:
    """Line number -> dotted path of the nearest enclosing def/class.

    Built by descent rather than by parent pointers so a lambda or comprehension inside a function
    still attributes to that function, which is what "who calls this" means to a reader."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, path: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                here = [*path, child.name]
                lo = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                hi = getattr(child, "end_lineno", child.lineno) or child.lineno
                for ln in range(lo, hi + 1):
                    out[ln] = ".".join(here)
                walk(child, here)
            else:
                walk(child, path)

    walk(tree, [])
    return out


def _callee_name(func: ast.expr, binder: _Binder) -> tuple[bool, str]:
    """Whether this Call's callee provably IS the target, and the evidence for saying so."""
    if isinstance(func, ast.Name):
        if func.id in binder.local_names:
            return True, f"call to imported name `{func.id}`"
        return False, ""
    if isinstance(func, ast.Attribute):
        value = func.value
        # `mod.func(...)` where `mod` is an alias of the target's module.
        if (isinstance(value, ast.Name) and value.id in binder.module_aliases
                and func.attr == binder.tname):
            return True, f"call to `{value.id}.{func.attr}` (module alias)"
        return False, ""
    return False, ""


def label_file(path: str, root: str, target: str,
               aliases: set[tuple[str, str]] | None = None) -> FileVerdict:
    """Every site in one file that mentions *target*'s bare name, labelled or abstained on."""
    verdict = FileVerdict()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        verdict.parse_failed = True
        return verdict

    tname = target.rpartition(".")[2]
    if tname not in src:                       # cheap reject: the name never appears
        return verdict

    binder = _Binder(target, _module_of(path, root), aliases)
    binder.visit(tree)
    rel = os.path.relpath(path, root)
    enclosing = _enclosing_map(tree)

    def where(ln: int) -> str:
        return enclosing.get(ln, "<module>")

    # A star import, or the local name rebound, means this file's bindings are not readable from its
    # syntax. Every mention becomes undecidable rather than being guessed at either way.
    opaque = binder.opaque or bool(binder.local_names & binder.reassigned)

    # Emitted directly from the binder. An `ImportFrom`/`Import` statement stores its names as
    # `ast.alias` objects, which are NOT `ast.Name` nodes, so the walk below can never see them —
    # the first version of this file reported zero imports for every symbol because of it.
    for ln in sorted(binder.import_lines):
        verdict.sites.append(Site(rel, ln, where(ln), IMPORT, "the import statement"))

    seen_calls: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            hit, why = _callee_name(node.func, binder)
            if hit and not opaque:
                verdict.sites.append(
                    Site(rel, node.lineno, where(node.lineno), CALL, why))
                seen_calls.add((node.lineno, tname))

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == tname:
            ln = node.lineno
            if (ln, tname) in seen_calls:
                continue                        # already counted as the call it is part of
            if opaque:
                verdict.sites.append(
                    Site(rel, ln, where(ln), UNDECIDABLE,
                         "star import or local rebinding — the syntax does not say what this binds"))
                continue
            if node.id in binder.local_names:
                verdict.sites.append(
                    Site(rel, ln, where(ln), REFERENCE,
                         "bound name used without calling it (passed, assigned, or annotated)"))
            else:
                verdict.sites.append(
                    Site(rel, ln, where(ln), UNDECIDABLE,
                         "bare name matches but nothing in this file binds it to the target"))

    # An attribute access spelled `<something>.<target name>` where the receiver is not a known
    # module alias — `self.handler.run()`, `obj.run` — is the single largest undecidable class, and
    # the one a name-matching resolver silently claims. Recorded so the abstention is visible.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == tname:
            recv = node.value
            decidable_receiver = (isinstance(recv, ast.Name)
                                  and recv.id in binder.module_aliases)
            if decidable_receiver:
                continue                        # handled above as a module-alias call
            verdict.sites.append(
                Site(rel, node.lineno, where(node.lineno), UNDECIDABLE,
                     "attribute access on a value — the receiver's type is not a syntactic fact"))
    return verdict


@dataclass
class Truth:
    """The labelled population for one target symbol."""

    target: str
    calls: set[tuple[str, str]] = field(default_factory=set)
    references: set[tuple[str, str]] = field(default_factory=set)
    imports: set[tuple[str, str]] = field(default_factory=set)
    undecidable: set[tuple[str, str]] = field(default_factory=set)
    sites: list[Site] = field(default_factory=list)
    parse_failures: int = 0

    @property
    def decided(self) -> int:
        return len(self.calls) + len(self.references) + len(self.imports)

    @property
    def coverage(self) -> float:
        """Share of mentioning sites this oracle was willing to judge. Reported, never hidden."""
        total = self.decided + len(self.undecidable)
        return (self.decided / total) if total else 1.0


def target_from_definition(root: str, def_file: str, symbol: str) -> str:
    """The importable qualified name of *symbol* defined in *def_file*.

    Callers name a target by where it is DEFINED rather than by a dotted string, because the dotted
    string is exactly the thing in dispute: `src.pkg.mod.func` and `pkg.mod.func` denote the same
    function and only one of them appears in any import statement. Deriving it removes a whole class
    of silent zero-result runs."""
    return f"{_module_of(os.path.join(root, def_file), root)}.{symbol}"


def truth_for(root: str, target: str, files: list[str] | None = None) -> Truth:
    """Label every mention of *target* across the repository."""
    t = Truth(target=target)
    paths = files if files is not None else _walk_py(root)
    aliases = _alias_set(target, _reexport_map(root, paths))
    for path in paths:
        v = label_file(path, root, target, aliases)
        if v.parse_failed:
            t.parse_failures += 1
            continue
        for s in v.sites:
            t.sites.append(s)
            bucket = {CALL: t.calls, REFERENCE: t.references,
                      IMPORT: t.imports, UNDECIDABLE: t.undecidable}[s.kind]
            bucket.add(s.key())
    # A site both called and merely referenced from the same enclosing symbol is a CALLER; the
    # weaker label would otherwise mask the stronger one during scoring.
    t.references -= t.calls
    t.imports -= t.calls | t.references
    t.undecidable -= t.calls | t.references | t.imports
    return t
