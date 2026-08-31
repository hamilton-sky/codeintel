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
    not-target   the name appears, but this file's own syntax binds   -> a claimed caller here
                 it to something else                                    is a FABRICATION
    undecidable  the site mentions the name but the syntax does not
                 settle what it binds to                             -> excluded from scoring

Why `not-target` exists, and why abstention alone was not enough. Scoring restricts every engine's
claim to the sites this oracle decided, so a site labelled `undecidable` costs an engine nothing to
claim. That is correct for `self.thing.run()` — but the first version of this file also filed the
*bare name that nothing binds* under `undecidable`, and that is the exact shape of this project's
worst observed failure: 32 fabricated callers for `describe`, a name matched across files that never
imported it. Measured under the old labels, those 32 rows cost precisely nothing, and the symbol
dropped out of the table entirely (coverage 0%, decidable population empty).

So a NEGATIVE has to be decidable too, or the benchmark is blind in exactly the direction that
matters. The rule is conservative, and deliberately narrower than "not imported here": a bare name is
labelled `not-target` only when the file's own syntax ACCOUNTS for it — a parameter, an assignment, a
`def`/`class` in scope, an import of something else, or a builtin. Then it provably denotes that
other binding. A name nothing in the file accounts for is a true injected global, which in Python
could conceivably have been installed by another module, so it stays `undecidable`.
"""
from __future__ import annotations

import ast
import builtins
import os
from dataclasses import dataclass, field

CALL = "call"
REFERENCE = "reference"
IMPORT = "import"
NOT_TARGET = "not-target"
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


_MODULE_SCOPE_KEY = "<module>"


def _scope_binds(tree: ast.AST) -> dict[str, set[str]]:
    """`scope path -> names that scope binds`, keyed the same way `_enclosing_map` names a line.

    This is what makes a NEGATIVE decidable. To say "the `describe` on line 40 is not the target" you
    have to show what it IS, and that means knowing which names this file's own syntax binds where.
    Every binding form Python has at statement level is collected; a name reached through none of
    them is a global the file does not explain, and the caller abstains on it.

    Scope paths match `_enclosing_map` exactly — including its choice to attribute a lambda or a
    comprehension to the function containing it rather than to a scope of its own. That is not how
    Python resolves names, and it is still the right key here, because of where this table is
    consulted: only for a name the binder has already established this file does NOT bind to the
    target. So the imprecision can move a site between `not-target` and `undecidable` — never onto a
    positive, and never into a charge against an engine that was right.
    """
    binds: dict[str, set[str]] = {}

    def add(path: str, name: str) -> None:
        binds.setdefault(path, set()).add(name)

    def path_of(parts: list[str]) -> str:
        return ".".join(parts) if parts else _MODULE_SCOPE_KEY

    def bind_target(path: str, node: ast.AST | None) -> None:
        """Every `Name` in an assignment/loop/with/except target, tuples and stars included."""
        if node is None:
            return
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                add(path, n.id)

    def bind_args(path: str, args: ast.arguments) -> None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                  args.vararg, args.kwarg):
            if a is not None:
                add(path, a.arg)

    def walk(node: ast.AST, parts: list[str]) -> None:
        here = path_of(parts)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                add(here, child.name)                     # the def binds its name in THIS scope
                inner = [*parts, child.name]
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bind_args(path_of(inner), child.args)
                walk(child, inner)
                continue
            if isinstance(child, ast.Lambda):
                bind_args(here, child.args)               # attributed to the enclosing function
            elif isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for t in (child.targets if isinstance(child, ast.Assign) else [child.target]):
                    bind_target(here, t)
            elif isinstance(child, (ast.NamedExpr, ast.For, ast.AsyncFor)):
                bind_target(here, child.target)
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    bind_target(here, item.optional_vars)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                add(here, child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    if alias.name != "*":
                        add(here, alias.asname or alias.name.split(".")[0])
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                for name in child.names:
                    add(here, name)
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for gen in child.generators:
                    bind_target(here, gen.target)
            walk(child, parts)

    walk(tree, [])
    return binds


def _accounted_by(name: str, scope: str, binds: dict[str, set[str]]) -> str | None:
    """Where *name* is bound, looking outward from *scope*. None when the file never binds it.

    `None` is the abstention case, not the negative case: a name this file does not bind is a global
    it does not explain, and Python lets another module install one.
    """
    parts = [] if scope == _MODULE_SCOPE_KEY else scope.split(".")
    while True:
        key = ".".join(parts) if parts else _MODULE_SCOPE_KEY
        if name in binds.get(key, frozenset()):
            return key
        if not parts:
            return None
        parts.pop()


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
    scopes = _scope_binds(tree)

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
                # Nothing here binds the name to the TARGET. That is not yet a negative: it is one
                # only if the file says what the name is instead. `describe(...)` in a file that
                # imports no `describe` and declares none is an injected global, and Python permits
                # another module to have installed the target itself under that name.
                bound_at = _accounted_by(tname, where(ln), scopes)
                if bound_at is not None:
                    verdict.sites.append(
                        Site(rel, ln, where(ln), NOT_TARGET,
                             f"a different `{tname}` — bound by "
                             + ("this module" if bound_at == _MODULE_SCOPE_KEY
                                else f"`{bound_at}`")))
                elif hasattr(builtins, tname):
                    verdict.sites.append(
                        Site(rel, ln, where(ln), NOT_TARGET, f"the builtin `{tname}`"))
                else:
                    verdict.sites.append(
                        Site(rel, ln, where(ln), UNDECIDABLE,
                             "bare name matches, and nothing in this file binds it at all — "
                             "an injected global, which the syntax cannot resolve either way"))

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
    # Sites that provably do NOT denote the target. Scored, and the reason this benchmark can
    # charge a fabricated caller anything at all.
    negatives: set[tuple[str, str]] = field(default_factory=set)
    undecidable: set[tuple[str, str]] = field(default_factory=set)
    sites: list[Site] = field(default_factory=list)
    parse_failures: int = 0

    @property
    def decided(self) -> int:
        return (len(self.calls) + len(self.references) + len(self.imports)
                + len(self.negatives))

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
            bucket = {CALL: t.calls, REFERENCE: t.references, IMPORT: t.imports,
                      NOT_TARGET: t.negatives, UNDECIDABLE: t.undecidable}[s.kind]
            bucket.add(s.key())
    # A site both called and merely referenced from the same enclosing symbol is a CALLER; the
    # weaker label would otherwise mask the stronger one during scoring.
    t.references -= t.calls
    t.imports -= t.calls | t.references
    t.undecidable -= t.calls | t.references | t.imports
    # UNDECIDABLE outranks NOT_TARGET, and the order matters more than it looks. A function that
    # both uses an unrelated `run` and calls `self.thing.run()` has one readable site and one
    # unreadable one; calling that key a proven negative would charge an engine a false positive
    # for a claim that might be correct. A key is only a negative when NOTHING in it is in doubt.
    t.negatives -= t.calls | t.references | t.imports | t.undecidable
    return t
