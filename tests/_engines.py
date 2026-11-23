"""Engine discovery from the filesystem, shared by every test whose domain is "each engine".

An engine is either a single module directly under `codeintel/engines/` (`none.py`) or a package
there with a `provider.py` adapter (`graph/`, `lsp/`, `semantic/`). Reading this off disk rather
than typing the list is what lets a fourth engine fail these guards instead of skipping them.
"""
from __future__ import annotations

import importlib
import pathlib
from types import ModuleType

import codeintel.engines

ENGINES_DIR = pathlib.Path(codeintel.engines.__file__).parent


def engine_module_names() -> dict[str, str]:
    """Engine name -> dotted module path of its CodeProvider adapter."""
    out: dict[str, str] = {}
    for entry in sorted(ENGINES_DIR.iterdir()):
        if entry.suffix == ".py" and entry.stem != "__init__":
            out[entry.stem] = f"codeintel.engines.{entry.stem}"
        elif entry.is_dir() and (entry / "provider.py").exists():
            out[entry.name] = f"codeintel.engines.{entry.name}.provider"
    return out


def engine_names() -> set[str]:
    return set(engine_module_names())


def engine_modules() -> list[tuple[str, ModuleType]]:
    return [(n, importlib.import_module(m)) for n, m in sorted(engine_module_names().items())]
