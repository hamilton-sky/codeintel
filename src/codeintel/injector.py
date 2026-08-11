from __future__ import annotations

import logging
import os

_START_MARKER = "<!-- codeintel-map-start -->"
_END_MARKER = "<!-- codeintel-map-end -->"
_CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md"]

_BLOCK_CONTENT = (
    "\n## codeintel orientation map\n\n"
    "See [CODE_INTEL.md](CODE_INTEL.md) for a ranked overview of this codebase "
    "modules, key symbols (by call frequency), and entry points. "
    "Refresh with: `codeintel map`."
    "\n"
)

_logger = logging.getLogger(__name__)


class Injector:
    """Idempotently injects a CODE_INTEL.md reference block into CLAUDE.md or AGENTS.md."""

    def inject(self, project_root: str) -> tuple[str | None, str]:
        try:
            path = _find_context_file(project_root)
            if path is None:
                return (None, "no-context-file")

            content = _read_file(path)
            if content is None:
                return (None, "error")

            new_content, action = _update_block(content)
            _write_file(path, new_content)
            return (path, action)
        except Exception as exc:
            _logger.warning("Injector.inject failed: %s", exc)
            return (None, "error")


def _find_context_file(project_root: str) -> str | None:
    for name in _CONTEXT_FILES:
        candidate = os.path.join(project_root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="strict") as f:
            return f.read()
    except Exception as exc:
        _logger.warning("Injector: could not read %s: %s", path, exc)
        return None


def _write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _update_block(content: str) -> tuple[str, str]:
    has_start = _START_MARKER in content
    has_end = _END_MARKER in content

    block = _START_MARKER + _BLOCK_CONTENT + _END_MARKER

    if has_start and has_end:
        start_idx = content.index(_START_MARKER)
        end_idx = content.index(_END_MARKER) + len(_END_MARKER)
        new_content = content[:start_idx] + block + content[end_idx:]
        return (new_content, "updated")

    # Missing or corrupted (only one marker): append a fresh block
    separator = "\n\n" if not content.endswith("\n\n") else ""
    if content.endswith("\n"):
        separator = "\n"
    new_content = content + separator + block
    return (new_content, "appended")
