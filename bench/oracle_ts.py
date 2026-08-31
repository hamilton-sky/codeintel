"""Ground truth for "what references this TypeScript symbol, and how" — from the AST, with abstention.

The Python oracle exists because two careful readings of the same hand-checked evidence produced
opposite designs. This one exists because the *worst* failure ever observed in this project is not in
Python at all: 32 fabricated callers for `describe`, a name matched across files that never imported
it. Nothing in the Python table speaks to that, and a TypeScript arm built on positives-only truth
would not have spoken to it either — the sites would all have been unjudged, and the fabrication free.

It shares `Site`, `FileVerdict` and `Truth` with `oracle_py`, so the scorer does not care which
language produced a label, and the same five kinds mean the same five things.

**Where TypeScript is MORE decidable than Python.** An ES module's bindings are exhaustively stated:
a module-scope symbol in another file is reachable only through an `import`, and `import * as ns`
binds a namespace object, so its uses stay `ns.foo` and remain readable. Python has to abstain on an
unbound bare name because another module can install a global; in a module, that same shape is a
PROVEN NEGATIVE. That is precisely the `describe` case, which is why this arm can measure the failure
it was built for.

Three guards keep that argument honest, because each one is a real way it can fail:

    script files     a file with no import and no export is not a module. Its top-level names share
                     the global scope, so module reachability says nothing and every bare name in it
                     is undecidable.
    self-installed   a repo that assigns `globalThis.foo = ...` anywhere has manufactured exactly the
    globals          escape hatch the argument denies. The oracle abstains on that NAME tree-wide.
    unresolvable     `import { foo } from "@app/proxy"` may be a path alias for the target or a real
    specifiers       package that shares a name. Unless tsconfig `paths` or `node_modules` settles
                     it, the name is undecidable in that file.

Everything else follows the Python oracle: stated re-exports are followed (arithmetic on an explicit
statement), a property access on a value is an abstention (the receiver's type is not a syntactic
fact), and labels are relationship KINDS rather than confidences.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from oracle_py import CALL, IMPORT, NOT_TARGET, REFERENCE, UNDECIDABLE, FileVerdict, Site, Truth

_TS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "out", "coverage", ".next", ".turbo",
    "__pycache__", ".venv", "venv", ".serena", ".cache",
})
# Candidate suffixes for a specifier, in the order a bundler would try them.
_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
                     "/index.ts", "/index.tsx", "/index.js", "/index.jsx")
_MODULE_SCOPE_KEY = "<module>"
_GLOBAL_OBJECTS = frozenset({"globalThis", "global", "window", "self"})


def _parser(path: str):
    from tree_sitter_language_pack import get_parser
    return get_parser("tsx" if path.endswith((".tsx", ".jsx")) else "typescript")


def _walk_ts(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        out.extend(os.path.join(dirpath, fn)
                   for fn in filenames if fn.endswith(_TS_EXTS))
    return sorted(out)


def _txt(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1


# --- specifier resolution ------------------------------------------------------------------------

def _tsconfig_paths(root: str) -> tuple[str, dict[str, list[str]]]:
    """`(baseUrl, paths)` from the nearest tsconfig, or `("", {})`.

    Read with the comments and trailing commas stripped, because real tsconfigs are JSONC and
    `json.loads` refuses them. A tsconfig that cannot be read yields no aliases, which makes aliased
    specifiers undecidable rather than silently wrong.
    """
    for name in ("tsconfig.json", "jsconfig.json"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
            raw = re.sub(r"//[^\n]*", "", raw)
            raw = re.sub(r",(\s*[}\]])", r"\1", raw)
            opts = (json.loads(raw) or {}).get("compilerOptions") or {}
        except (OSError, ValueError):
            continue
        base = os.path.join(root, opts.get("baseUrl") or ".")
        return base, opts.get("paths") or {}
    return "", {}


def _first_existing(base: str) -> str | None:
    for suffix in _RESOLVE_SUFFIXES:
        cand = base + suffix
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


@dataclass
class _Resolution:
    """Where a specifier points, and how sure we are."""

    path: str | None = None          # a file inside the tree
    external: bool = False           # provably a package, so provably not the target
    unknown: bool = False            # neither — the caller must abstain on names from it


def _resolve(spec: str, importer: str, root: str,
             base_url: str, paths: dict[str, list[str]]) -> _Resolution:
    if spec.startswith("."):
        hit = _first_existing(os.path.normpath(os.path.join(os.path.dirname(importer), spec)))
        # A relative specifier that resolves to nothing is a broken import or an extension this
        # walk does not know. Either way the name it binds is not readable.
        return _Resolution(path=hit) if hit else _Resolution(unknown=True)
    for pattern, targets in paths.items():
        if "*" in pattern:
            head, _, tail = pattern.partition("*")
            if spec.startswith(head) and spec.endswith(tail):
                middle = spec[len(head):len(spec) - len(tail) or None]
                for t in targets:
                    hit = _first_existing(os.path.join(base_url, t.replace("*", middle)))
                    if hit:
                        return _Resolution(path=hit)
        elif spec == pattern:
            for t in targets:
                hit = _first_existing(os.path.join(base_url, t))
                if hit:
                    return _Resolution(path=hit)
    pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
    if os.path.isdir(os.path.join(root, "node_modules", pkg)):
        return _Resolution(external=True)
    # Bare, unmapped, and not installed. It could be a path alias for the target's own file or a
    # package that happens to export the same name; claiming either is a guess.
    return _Resolution(unknown=True)


# --- one parse of the tree, reused ----------------------------------------------------------------

@dataclass
class Repo:
    """Everything about a checkout that is the same for every target: parses, re-exports, globals."""

    root: str
    files: list[str] = field(default_factory=list)
    trees: dict[str, tuple[object, bytes]] = field(default_factory=dict)
    reexports: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    # Names the repository installs onto `globalThis`/`window`. The one documented hole in the
    # module-reachability argument, so it is closed by abstaining rather than left implicit.
    injected_globals: set[str] = field(default_factory=set)
    base_url: str = ""
    paths: dict[str, list[str]] = field(default_factory=dict)
    parse_failures: int = 0

    def tree(self, path: str):
        return self.trees.get(os.path.abspath(path))


def _collect_injected_globals(node, src: bytes, out: set[str]) -> None:
    """`globalThis.foo = ...`, anywhere at any depth."""
    if node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        if left is not None and left.type == "member_expression":
            obj = left.child_by_field_name("object")
            prop = left.child_by_field_name("property")
            if (obj is not None and prop is not None
                    and _txt(obj, src) in _GLOBAL_OBJECTS):
                out.add(_txt(prop, src))
    for child in node.named_children:
        _collect_injected_globals(child, src, out)


def index_repo(root: str, files: list[str] | None = None) -> Repo:
    """Parse the tree once, and derive everything that does not depend on which symbol is asked."""
    repo = Repo(root=root, files=files if files is not None else _walk_ts(root))
    repo.base_url, repo.paths = _tsconfig_paths(root)
    for path in repo.files:
        try:
            with open(path, "rb") as fh:
                src = fh.read()
            tree = _parser(path).parse(src)
        except (OSError, ValueError):
            repo.parse_failures += 1
            continue
        abs_path = os.path.abspath(path)
        repo.trees[abs_path] = (tree, src)
        _collect_injected_globals(tree.root_node, src, repo.injected_globals)

    for abs_path, (tree, src) in repo.trees.items():
        table: dict[str, tuple[str, str]] = {}
        for node in tree.root_node.named_children:          # top level only, as in oracle_py
            if node.type != "export_statement":
                continue
            source = node.child_by_field_name("source")
            if source is None:
                continue
            spec = _txt(source, src).strip("\"'`")
            res = _resolve(spec, abs_path, root, repo.base_url, repo.paths)
            if res.path is None:
                continue
            clause = next((c for c in node.named_children if c.type == "export_clause"), None)
            if clause is None:                              # `export * from "./x"`
                table["*"] = (res.path, "*")
                continue
            for spec_node in clause.named_children:
                if spec_node.type != "export_specifier":
                    continue
                name = spec_node.child_by_field_name("name")
                alias = spec_node.child_by_field_name("alias")
                if name is None:
                    continue
                local = _txt(alias or name, src)
                table[local] = (res.path, _txt(name, src))
        if table:
            repo.reexports[abs_path] = table
    return repo


_ALIAS_DEPTH = 4


def alias_set(target_file: str, target_name: str, repo: Repo) -> set[tuple[str, str]]:
    """Every `(file, name)` that provably denotes the target, via stated re-exports.

    `export * from "./x"` counts: it is an explicit statement that this module re-exports whatever
    `x` exports, which is the same class of fact as naming the symbol. What is never done is
    matching a bare name across files, which is the failure being measured.
    """
    known = {(os.path.abspath(target_file), target_name)}
    for _ in range(_ALIAS_DEPTH):
        grew = False
        for mod, table in repo.reexports.items():
            for local, (src_file, src_name) in table.items():
                origin = (src_file, target_name if src_name == "*" else src_name)
                here = (mod, target_name if local == "*" else local)
                if origin in known and here not in known:
                    known.add(here)
                    grew = True
        if not grew:
            break
    return known


# --- scopes, mirroring `oracle_py._enclosing_map` / `_scope_binds` ---------------------------------

_FN_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_NAMED_SCOPES = frozenset({
    "function_declaration", "generator_function_declaration", "class_declaration",
    "method_definition", "function_signature",
})


def _declarator_scope_name(node, src: bytes) -> str | None:
    """`const useToast = () => {...}` names a scope. In TS that is not a corner case; it is the
    dominant way functions are written, and attributing its body to `<module>` would put half a
    real repository's callers under one key."""
    if node.type != "variable_declarator":
        return None
    value = node.child_by_field_name("value")
    name = node.child_by_field_name("name")
    if value is not None and value.type in _FN_VALUES and name is not None \
            and name.type == "identifier":
        return _txt(name, src)
    return None


def _enclosing_map(root_node, src: bytes) -> dict[int, str]:
    """Line -> dotted path of the nearest enclosing named scope, or absent for module level."""
    out: dict[int, str] = {}

    def walk(node, path: list[str]) -> None:
        for child in node.named_children:
            if child.type in _NAMED_SCOPES:
                n = child.child_by_field_name("name")
                name = _txt(n, src) if n is not None else None
            else:
                name = _declarator_scope_name(child, src)
            if name:
                here = [*path, name]
                for ln in range(_line(child), child.end_point[0] + 2):
                    out[ln] = ".".join(here)
                walk(child, here)
            else:
                walk(child, path)

    walk(root_node, [])
    return out


def _pattern_names(node, src: bytes) -> list[str]:
    """Names a binding pattern introduces — plain, destructured, rest or defaulted."""
    if node is None:
        return []
    found = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("identifier", "shorthand_property_identifier_pattern"):
            found.append(_txt(n, src))
            continue
        stack.extend(n.named_children)
    return found


def _scope_binds(root_node, src: bytes) -> dict[str, set[str]]:
    """`scope path -> names that scope binds`, keyed as `_enclosing_map` keys a line."""
    binds: dict[str, set[str]] = {}

    def add(path: str, name: str) -> None:
        binds.setdefault(path, set()).add(name)

    def add_all(path: str, names: list[str]) -> None:
        for n in names:
            add(path, n)

    def path_of(parts: list[str]) -> str:
        return ".".join(parts) if parts else _MODULE_SCOPE_KEY

    def bind_params(path: str, node) -> None:
        params = node.child_by_field_name("parameters")
        if params is None:
            return
        for p in params.named_children:
            add_all(path, _pattern_names(p.child_by_field_name("pattern") or p, src))

    def walk(node, parts: list[str]) -> None:
        here = path_of(parts)
        for child in node.named_children:
            if child.type in _NAMED_SCOPES:
                n = child.child_by_field_name("name")
                scope_name = _txt(n, src) if n is not None else None
                if scope_name and child.type != "method_definition":
                    add(here, scope_name)                    # the declaration binds its own name
            else:
                scope_name = _declarator_scope_name(child, src)

            if child.type == "variable_declarator":
                add_all(here, _pattern_names(child.child_by_field_name("name"), src))
            elif child.type == "import_statement":
                add_all(here, _import_bound_names(child, src))
            elif child.type == "catch_clause":
                add_all(here, _pattern_names(child.child_by_field_name("parameter"), src))
            elif child.type in ("for_in_statement", "for_statement"):
                add_all(here, _pattern_names(child.child_by_field_name("left"), src))

            if scope_name:
                inner = [*parts, scope_name]
                bind_params(path_of(inner), child)
                if child.type == "variable_declarator":
                    value = child.child_by_field_name("value")
                    if value is not None:
                        bind_params(path_of(inner), value)
                walk(child, inner)
                continue
            if child.type in _FN_VALUES:                     # an anonymous callback
                bind_params(here, child)
            walk(child, parts)

    walk(root_node, [])
    return binds


def _accounted_by(name: str, scope: str, binds: dict[str, set[str]]) -> str | None:
    parts = [] if scope == _MODULE_SCOPE_KEY else scope.split(".")
    while True:
        key = ".".join(parts) if parts else _MODULE_SCOPE_KEY
        if name in binds.get(key, frozenset()):
            return key
        if not parts:
            return None
        parts.pop()


# --- what one file binds to the target ------------------------------------------------------------

def _import_bound_names(node, src: bytes) -> list[str]:
    """Every bare name an `import` statement introduces, in any of its forms."""
    names: list[str] = []
    clause = next((c for c in node.named_children if c.type == "import_clause"), None)
    if clause is None:
        return names
    for child in clause.named_children:
        if child.type == "identifier":                        # default import
            names.append(_txt(child, src))
        elif child.type == "namespace_import":
            names.extend(_txt(c, src) for c in child.named_children if c.type == "identifier")
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type != "import_specifier":
                    continue
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                if name is not None:
                    names.append(_txt(alias or name, src))
    return names


@dataclass
class _Bindings:
    local_names: set[str] = field(default_factory=set)
    namespace_aliases: set[str] = field(default_factory=set)
    import_lines: set[int] = field(default_factory=set)
    # Names imported from a specifier that could not be resolved. Claiming these either way is a
    # guess, so every mention of them in this file is an abstention.
    unresolved: set[str] = field(default_factory=set)
    is_module: bool = False
    skip: set[int] = field(default_factory=set)               # specifier + declaration name nodes


def _walk(node):
    """Every node in the subtree, parents before children."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.named_children)


def _bindings(tree, src: bytes, abs_path: str, root: str, target_name: str,
              aliases: set[tuple[str, str]], repo: Repo) -> _Bindings:
    """What this file binds to the target, and what it refuses to say.

    Only the forms whose meaning is fixed by the statement itself are honoured — a named import, an
    aliased one, a namespace import, and a stated re-export. `require()` with a computed path, a
    dynamic `import()`, and an ambient declaration are not, because which binding they introduce is
    a runtime or type-space fact rather than something this file states.
    """
    b = _Bindings()
    for node in tree.root_node.named_children:
        if node.type in ("import_statement", "export_statement"):
            b.is_module = True                                # anything exported makes it a module

    for node in tree.root_node.named_children:
        if node.type not in ("import_statement", "export_statement"):
            continue
        source = node.child_by_field_name("source")
        if source is None:
            continue                                          # a local `export { x }`, not an edge
        spec = _txt(source, src).strip("\"\'`")
        res = _resolve(spec, abs_path, root, repo.base_url, repo.paths)

        for sub in _walk(node):
            if sub.type == "namespace_import":
                ns = next((_txt(c, src) for c in sub.named_children if c.type == "identifier"), None)
                if ns and res.path and (res.path, target_name) in aliases:
                    b.namespace_aliases.add(ns)
                    b.import_lines.add(_line(node))
                continue
            if sub.type not in ("import_specifier", "export_specifier"):
                continue
            name = sub.child_by_field_name("name")
            alias = sub.child_by_field_name("alias")
            if name is None:
                continue
            # An import specifier is a binding site, not a mention. The Python oracle reported zero
            # imports for every symbol until the equivalent nodes were emitted directly rather than
            # hoped for from the walk; the same trap is here, one node type over.
            b.skip.add(name.id)
            if alias is not None:
                b.skip.add(alias.id)
            local, imported = _txt(alias or name, src), _txt(name, src)
            if res.path and (res.path, imported) in aliases:
                b.local_names.add(local)
                b.import_lines.add(_line(node))
            elif res.unknown and imported == target_name:
                # It could be a path alias for the target's own file, or a package that exports the
                # same name. Both readings are consistent with what this file says.
                b.unresolved.add(local)
    return b


# --- labelling -------------------------------------------------------------------------------------

def _is_callee(node) -> bool:
    """Whether *node* is the thing being invoked, directly or as `ns.node(...)`."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "call_expression":
        return parent.child_by_field_name("function") is not None \
            and parent.child_by_field_name("function").id == node.id
    if parent.type == "member_expression" and parent.parent is not None \
            and parent.parent.type == "call_expression":
        fn = parent.parent.child_by_field_name("function")
        return fn is not None and fn.id == parent.id
    return False


def label_file(abs_path: str, root: str, target_file: str, target_name: str,
               aliases: set[tuple[str, str]], repo: Repo) -> FileVerdict:
    """Every site in one file that mentions *target_name*, labelled or abstained on."""
    verdict = FileVerdict()
    entry = repo.trees.get(os.path.abspath(abs_path))
    if entry is None:
        verdict.parse_failed = True
        return verdict
    tree, src = entry
    if target_name.encode() not in src:                       # cheap reject
        return verdict

    b = _bindings(tree, src, os.path.abspath(abs_path), root, target_name, aliases, repo)
    rel = os.path.relpath(abs_path, root)
    enclosing = _enclosing_map(tree.root_node, src)
    binds = _scope_binds(tree.root_node, src)
    is_defining_file = os.path.abspath(abs_path) == os.path.abspath(target_file)

    def where(ln: int) -> str:
        return enclosing.get(ln, _MODULE_SCOPE_KEY)

    # An aliased import binds a DIFFERENT spelling — `import { forwardReleasedItem as fwd }` means
    # every call site reads `fwd(...)`. Scanning only for the target's own name finds the import and
    # none of its callers, which is a silent under-count of exactly the kind this oracle exists to
    # catch in other tools.
    watch = b.local_names | b.unresolved | {target_name}

    def emit(node, kind: str, why: str) -> None:
        ln = _line(node)
        verdict.sites.append(Site(rel, ln, where(ln), kind, why))

    for ln in sorted(b.import_lines):
        verdict.sites.append(Site(rel, ln, where(ln), IMPORT, "the import statement"))

    for node in _walk(tree.root_node):
        if node.id in b.skip:
            continue

        # `ns.forwardReleasedItem(...)` and `obj.forwardReleasedItem(...)` look identical until you
        # ask what the receiver is. One is arithmetic on a stated namespace import; the other is the
        # single largest abstention class, and the one a name-matching resolver silently claims.
        if node.type == "property_identifier" and _txt(node, src) == target_name:
            parent = node.parent
            if parent is None or parent.type != "member_expression":
                continue                                      # a type member or object key
            obj = parent.child_by_field_name("object")
            if obj is not None and obj.type == "identifier" and _txt(obj, src) in b.namespace_aliases:
                emit(node, CALL if _is_callee(node) else REFERENCE,
                     f"`{_txt(obj, src)}.{target_name}` through a namespace import")
            else:
                emit(node, UNDECIDABLE,
                     "property access on a value — the receiver's type is not a syntactic fact")
            continue

        if node.type != "identifier":
            continue
        text = _txt(node, src)
        if text not in watch:
            continue
        # The declaration itself binds a name; it does not mention one.
        if node.parent is not None and node.parent.type in _NAMED_SCOPES \
                and node.parent.child_by_field_name("name") is not None \
                and node.parent.child_by_field_name("name").id == node.id:
            continue

        scope = where(_line(node))
        kind_if_target = CALL if _is_callee(node) else REFERENCE

        if text in b.unresolved:
            emit(node, UNDECIDABLE,
                 "imported from a specifier that resolves to neither a file in the tree nor an "
                 "installed package")
        elif text in b.local_names:
            emit(node, kind_if_target,
                 "the imported name, used directly"
                 + ("" if text == target_name else f" (imported as `{text}`)"))
        elif text != target_name:
            continue                                          # a watched alias handled above
        elif is_defining_file:
            # Inside the file that defines it, the module-scope name IS the target — unless an
            # inner scope has shadowed it, which the scope table can see.
            bound_at = _accounted_by(target_name, scope, binds)
            if bound_at in (None, _MODULE_SCOPE_KEY):
                emit(node, kind_if_target, "the module-scope name, in its own file")
            else:
                emit(node, NOT_TARGET, f"shadowed inside `{bound_at}`")
        elif not b.is_module:
            # No import and no export: this file is a SCRIPT. Its top-level names share the global
            # scope, so the reachability argument below simply does not apply to it.
            emit(node, UNDECIDABLE,
                 "a script, not a module — its bare names are not governed by import reachability")
        elif target_name in repo.injected_globals:
            # The repo installs this exact name onto `globalThis` somewhere, which manufactures the
            # escape hatch that module reachability otherwise denies.
            emit(node, UNDECIDABLE,
                 f"the tree assigns `globalThis.{target_name}` somewhere, so a bare use of it "
                 "could reach the target after all")
        else:
            # THE decidable negative, and the reason this arm can measure what it was built for.
            # This file is a module, it imports no such name, and nothing ambient can supply the
            # target: a module-scope symbol in another file is reachable ONLY through an import.
            bound_at = _accounted_by(target_name, scope, binds)
            why = (f"a different `{target_name}` — bound by "
                   + ("this module" if bound_at == _MODULE_SCOPE_KEY else f"`{bound_at}`")
                   ) if bound_at else (
                f"a free name in a module that imports no `{target_name}` — an ambient or "
                "framework-injected global, which cannot be this target")
            emit(node, NOT_TARGET, why)
    return verdict


# --- the labelled population for one target --------------------------------------------------------

def target_from_definition(root: str, def_file: str, symbol: str) -> str:
    """`<path/to/file.ts>::<symbol>` — a TypeScript symbol's identity is its FILE, not a dotted name.

    The Python oracle derives an importable dotted name because Python imports by module name and
    `src.pkg.mod.f` vs `pkg.mod.f` was a real source of silent zero-result runs. TypeScript has the
    opposite shape: every import names a path, so the path IS the identity and there is nothing to
    derive. The two oracles agree on the comparison key — `(file, enclosing symbol)` — which is what
    lets one scorer read both.
    """
    return f"{def_file}::{symbol}"


def truth_for(root: str, target: str, repo: Repo | None = None) -> Truth:
    """Label every mention of *target* across the repository."""
    def_file, _, name = target.partition("::")
    repo = repo or index_repo(root)
    aliases = alias_set(os.path.join(root, def_file), name, repo)
    t = Truth(target=target)
    t.parse_failures = repo.parse_failures
    for path in repo.files:
        v = label_file(path, root, os.path.join(root, def_file), name, aliases, repo)
        if v.parse_failed:
            t.parse_failures += 1
            continue
        for s in v.sites:
            t.sites.append(s)
            bucket = {CALL: t.calls, REFERENCE: t.references, IMPORT: t.imports,
                      NOT_TARGET: t.negatives, UNDECIDABLE: t.undecidable}[s.kind]
            bucket.add(s.key())
    # Identical precedence to `oracle_py.truth_for`, for the same reasons: the stronger positive
    # wins, and doubt anywhere in a key outranks a proven negative.
    t.references -= t.calls
    t.imports -= t.calls | t.references
    t.undecidable -= t.calls | t.references | t.imports
    t.negatives -= t.calls | t.references | t.imports | t.undecidable
    return t
