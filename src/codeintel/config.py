from __future__ import annotations

import logging
import pathlib
import tomllib  # stdlib since 3.11, which is this package's floor

logger = logging.getLogger("codeintel")

_DEFAULTS: dict = {
    "backend": "auto",
    "semantic": "on",
    "reindex": "on-demand",
    "window": 20,
    "stride": 10,
    "max_chunks": 500,          # per file
    "max_total_chunks": 100000,  # safety ceiling on chunks embedded in one index pass
    "cosine_floor": 0.25,
    "model": "BAAI/bge-small-en-v1.5",
    "chunk_strategy": "syntax",  # syntax-aware (def/class boundaries) vs fixed line windows
    "rerank": "on",              # hybrid lexical+semantic rerank of search results
    "rerank_candidates": 30,     # cosine candidates fused/re-ranked before returning top-k
}

# Values restricted to a fixed set — anything else falls back to the default.
_ENUMS: dict = {
    "backend": {"auto", "graph", "lsp", "semantic"},
    "semantic": {"on", "off"},
    "reindex": {"on-demand", "never"},
    "chunk_strategy": {"syntax", "lines"},
    "rerank": {"on", "off"},
}
_POSITIVE_INTS = ("window", "stride", "max_chunks", "max_total_chunks", "rerank_candidates")


def _read_toml(path: pathlib.Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}


def _coerce(cfg: dict) -> dict:
    """Coerce/clamp a merged config to safe values, warning on and dropping bad ones. Never raises:
    a malformed ``.codeintel.toml`` (a string where a number belongs, an out-of-range floor, an
    unknown enum) must degrade to the default for that key, not break every query that loads it."""
    out = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        if key not in cfg:
            continue
        val = cfg[key]
        try:
            if key in _ENUMS:
                s = str(val).strip().lower()
                if s in _ENUMS[key]:
                    out[key] = s
                else:
                    logger.warning("config: %s=%r invalid (expected %s) — using %r",
                                   key, val, sorted(_ENUMS[key]), default)
            elif key == "model":
                out[key] = str(val).strip() or default
            elif key == "cosine_floor":
                f = float(val)
                if f != f:  # NaN (TOML allows `nan`) — reject so it can't silently disable the floor
                    raise ValueError("nan")
                out[key] = min(1.0, max(0.0, f))
            elif key in _POSITIVE_INTS:
                n = int(val)
                if n > 0:
                    out[key] = n
                else:
                    logger.warning("config: %s=%r must be > 0 — using %r", key, val, default)
        except (TypeError, ValueError, OverflowError):
            # int(float('inf')) raises OverflowError (TOML allows `inf`); float("x") → ValueError;
            # int([]) → TypeError. Any non-usable value logs and keeps the default, so the
            # docstring's never-raise promise holds even for the CLI paths that don't wrap this.
            logger.warning("config: %s=%r not usable — using default %r", key, val, default)
    # Keep any extra keys the user set (forward-compat) without validating them.
    out.update({k: v for k, v in cfg.items() if k not in _DEFAULTS})
    return out


def load_config(project_root: str | None = None) -> dict:
    """Return the merged, validated config: defaults < global < project. Values that fail
    validation fall back to their default (logged), so a bad config file never breaks a query."""
    root = pathlib.Path(project_root) if project_root is not None else pathlib.Path.cwd()

    global_cfg = _read_toml(pathlib.Path.home() / ".codeintel" / "config.toml")
    project_cfg = _read_toml(root / ".codeintel.toml")

    merged = {**_DEFAULTS, **global_cfg, **project_cfg}
    return _coerce(merged)
