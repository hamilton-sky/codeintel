"""Where codeintel keeps its per-machine state — one definition, used by every consumer.

``CODEINTEL_HOME`` exists because ``Path.home()`` raises when a process has no resolvable home
directory: a UID with no passwd entry and no ``$HOME``. That is not exotic for this tool. It is an
MCP server whose whole purpose is to be launched by coding agents, which are routinely run in
containers shaped exactly that way.

The override used to be honoured by the semantic cache alone. ``config.py`` and ``auth.py`` each
called ``Path.home()`` directly, so on such a host ``load_config()`` raised ``RuntimeError`` — and
kept raising with ``CODEINTEL_HOME`` set, because the override never reached them. The escape hatch
did not work in the one environment it was written for, and the failure surfaced several layers
away as a generic ``provider-error``. Anything resolving this directory now asks here.
"""
from __future__ import annotations

import os
import pathlib


def codeintel_home() -> pathlib.Path:
    """The per-machine state directory (cache, global config, auth).

    Raises ``RuntimeError`` (from ``Path.home()``) when there is no override and no resolvable home
    — deliberately, so a caller that must NAME the problem can. ``SemanticProvider.probe`` reports
    it with the remediation that actually fixes it; callers for whom a missing file is merely
    "no config" should catch it instead (see ``config.global_config_path``).
    """
    override = os.environ.get("CODEINTEL_HOME", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".codeintel"
