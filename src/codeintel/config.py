from __future__ import annotations

import pathlib
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

_DEFAULTS: dict = {
    "backend": "auto",
    "semantic": "on",
    "reindex": "on-demand",
    "window": 20,
    "stride": 10,
    "max_chunks": 500,
    "cosine_floor": 0.3,
    "model": "BAAI/bge-small-en-v1.5",
}


def _read_toml(path: pathlib.Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}


def load_config(project_root: str | None = None) -> dict:
    """Return merged config: defaults < global < project."""
    root = pathlib.Path(project_root) if project_root is not None else pathlib.Path.cwd()

    global_cfg = _read_toml(pathlib.Path.home() / ".codeintel" / "config.toml")
    project_cfg = _read_toml(root / ".codeintel.toml")

    merged = {**_DEFAULTS, **global_cfg, **project_cfg}
    return merged
