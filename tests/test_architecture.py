"""The package layout is the architecture: these tests keep the two from drifting apart.

`src/codeintel/` is six layer packages, top to bottom (see docs/architecture.md):

    cli > transports > tools (setup, c4, mapping) > gateway > engines > core

An import may point down or stay inside its own layer, never up. Engines additionally do not import
each other: each one is an independent adapter behind the CodeProvider protocol, and a reach from
one engine into another is how "swap the backend" quietly stops being true.

This reads imports from the AST, so it needs no backend and runs on every test leg. CI's
`c4-validate` job also runs `codeintel c4 . --check` against `.codeintel.toml`, which judges the same
rule from the live graph. Both exist on purpose: this one cannot be fooled by a backend resolution
bug, and that one is the product checking its own repository.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "codeintel"

# Top to bottom. Index 0 is the highest layer.
LAYERS = ["cli", "transports", "tools", "gateway", "engines", "core"]

# First path segment under src/codeintel -> layer. Single files at the package root are listed by
# name; everything else is a package.
_LAYER_OF_TOP = {
    "__main__.py": "cli",
    "cli": "cli",
    "transports": "transports",
    "setup": "tools",
    "c4": "tools",
    "mapping": "tools",
    "gateway": "gateway",
    "engines": "engines",
    "core": "core",
    "__init__.py": "core",   # only `__version__`, imported from everywhere
}

# Engine-to-engine imports that are deliberate. Each needs a reason, and an entry that no longer
# matches any import fails `test_every_engine_exception_is_still_needed`, so the list cannot rot.
ENGINE_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("engines/graph/symbol_diff.py", "engines/semantic/indexer.py"):
        "the symbol diff reuses the indexer's tree-sitter definition tables so both engines agree "
        "on what a definition is; extracting them to a shared syntax module is a follow-up",
}


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(SRC).as_posix()


def _layer(rel: str) -> str | None:
    return _LAYER_OF_TOP.get(rel.split("/", 1)[0])


def _engine(rel: str) -> str | None:
    """`engines/graph/x.py` -> "graph", `engines/none.py` -> "none", the package init -> None."""
    parts = rel.split("/")
    if parts[0] != "engines" or rel == "engines/__init__.py":
        return None
    return parts[1].removesuffix(".py")


def _resolve(dotted: str) -> str | None:
    """`codeintel.engines.graph.render` -> its file path relative to SRC, or None if not ours."""
    if dotted != "codeintel" and not dotted.startswith("codeintel."):
        return None
    parts = dotted.split(".")[1:]
    if not parts:
        return "__init__.py"
    base = SRC.joinpath(*parts)
    if base.with_suffix(".py").exists():
        return _rel(base.with_suffix(".py"))
    if (base / "__init__.py").exists():
        return _rel(base / "__init__.py")
    return None


def _imports(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every codeintel module *path* imports, function-local and TYPE_CHECKING imports included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(("codeintel", *path.relative_to(SRC).with_suffix("").parts))
    is_init = path.name == "__init__.py"
    package = package.removesuffix(".__init__") if is_init else package.rsplit(".", 1)[0]
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve(alias.name)
                if target:
                    out.append((node.lineno, target))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[:len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            for alias in node.names:
                # `from pkg import mod` names a module; `from mod import name` names an attribute.
                target = _resolve(f"{module}.{alias.name}") or _resolve(module)
                if target:
                    out.append((node.lineno, target))
    return out


def _edges() -> list[tuple[str, int, str]]:
    return [(_rel(p), line, target) for p in _modules() for line, target in _imports(p)]


def test_every_module_belongs_to_a_layer():
    """A new top-level module would otherwise sit outside every rule below."""
    orphans = [_rel(p) for p in _modules() if _layer(_rel(p)) is None]
    assert not orphans, (
        f"modules outside every layer: {orphans}. Put them in the layer package they belong to "
        f"(see docs/architecture.md), or add a new layer here and in .codeintel.toml")


def test_the_census_sees_the_imports_it_judges():
    """An empty or tiny domain would make the rules below pass vacuously."""
    edges = _edges()
    assert len(edges) > 200, len(edges)
    seen = {(src, dst) for src, _, dst in edges}
    assert ("transports/mcp.py", "gateway/factory.py") in seen
    assert ("gateway/gateway.py", "core/provider.py") in seen
    assert ("cli/query.py", "gateway/factory.py") in seen   # a function-local import


def test_imports_only_point_down_the_layers():
    upward = []
    for src, line, dst in _edges():
        a, b = _layer(src), _layer(dst)
        if a and b and LAYERS.index(b) < LAYERS.index(a):
            upward.append(f"{src}:{line} ({a}) imports {dst} ({b})")
    assert not upward, (
        "imports that point UP the layers (" + " > ".join(LAYERS) + "):\n  " + "\n  ".join(upward)
        + "\nMove the shared code down to the lowest layer that needs it, or pass it in from above.")


def test_engines_do_not_import_each_other():
    crossing = []
    for src, line, dst in _edges():
        a, b = _engine(src), _engine(dst)
        if a and b and a != b and (src, dst) not in ENGINE_EXCEPTIONS:
            crossing.append(f"{src}:{line} imports {dst}")
    assert not crossing, (
        "one engine imports another:\n  " + "\n  ".join(crossing)
        + "\nShared code belongs in codeintel/core; a deliberate exception goes in ENGINE_EXCEPTIONS "
          "with its reason.")


def test_every_engine_exception_is_still_needed():
    live = {(src, dst) for src, _, dst in _edges()}
    stale = [k for k in ENGINE_EXCEPTIONS if k not in live]
    assert not stale, f"engine exceptions that no longer match an import, delete them: {stale}"


def test_the_layer_list_matches_the_self_check_config():
    """`.codeintel.toml` drives `codeintel c4 --check` in CI; the two descriptions must agree."""
    import tomllib

    config = tomllib.loads((SRC.parent.parent / ".codeintel.toml").read_text(encoding="utf-8"))
    assert config["layers"]["order"] == LAYERS
    members = config["layers"]["members"]
    for top, layer in _LAYER_OF_TOP.items():
        expected = f"src/codeintel/{top}" if top.endswith(".py") else f"src/codeintel/{top}/**"
        assert expected in members[layer], (top, layer, members[layer])
