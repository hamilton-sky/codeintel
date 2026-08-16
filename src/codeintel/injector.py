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
        # newline="" keeps the file's own line endings intact. Universal-newline mode silently
        # converted a CRLF file to LF on write, producing a whole-file diff on every --inject.
        with open(path, encoding="utf-8", errors="strict", newline="") as f:
            return f.read()
    except Exception as exc:
        _logger.warning("Injector: could not read %s: %s", path, exc)
        return None


def _write_file(path: str, content: str) -> None:
    # Atomic: write a sibling temp file then os.replace it into place, so an interrupted
    # write can never leave the user's CLAUDE.md/AGENTS.md truncated or half-written.
    #
    # os.replace swaps in a NEW inode, so without care it also (a) drops the original's
    # permissions in favour of whatever the umask gives, and (b) replaces a symlinked
    # CLAUDE.md — a dotfile-manager or shared-team-rules setup — with a regular file, orphaning
    # the source. Resolve the link and carry the mode across.
    target = os.path.realpath(path)
    try:
        mode: int | None = os.stat(target).st_mode & 0o7777
    except OSError:
        mode = None

    tmp = target + ".codeintel.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, target)


def _update_block(content: str) -> tuple[str, str]:
    has_start = _START_MARKER in content
    has_end = _END_MARKER in content

    block = _START_MARKER + _BLOCK_CONTENT + _END_MARKER

    if has_start and has_end:
        start_idx = content.index(_START_MARKER)
        # The END marker must be searched for AFTER the start. `content.index(_END_MARKER)` finds
        # the FIRST one, which can sit before start_idx — a hand-edit, a bad merge, or this
        # function's own append branch below, which happily creates that ordering. The slice
        # `content[:start_idx] + block + content[end_idx:]` then RE-EMITS everything between the
        # stray marker and the block, duplicating the user's own instructions once per run and
        # growing without bound. CLAUDE.md is prompt context, so this silently degrades the agent
        # it is supposed to help.
        end_rel = content.find(_END_MARKER, start_idx)
        if end_rel != -1:
            end_idx = end_rel + len(_END_MARKER)
            return (content[:start_idx] + block + content[end_idx:], "updated")
        # Start with no end after it: the block is truncated. Replace from the start marker to
        # the end of the file rather than splicing around a marker that precedes it.
        return (content[:start_idx] + block, "repaired")

    # Missing or corrupted (only one marker): append a fresh block
    separator = "\n\n" if not content.endswith("\n\n") else ""
    if content.endswith("\n"):
        separator = "\n"
    new_content = content + separator + block
    return (new_content, "appended")
