"""Terminal output system — one small, dependency-free styling layer shared by every
human-facing command (doctor, status, query, setup) so they read as one tool.

Two independent axes, each auto-detected and overridable:
  * color  — raw ANSI SGR, ON only for a TTY, disabled by NO_COLOR / --no-color / TERM=dumb.
  * glyphs — unicode by default, ASCII fallback when the stream can't encode them / --ascii.

Alignment note: status glyphs are drawn only from Unicode blocks that render as exactly one
column in monospace fonts. `⚠` (U+26A0, Misc Symbols) is deliberately AVOIDED — many coding
fonts draw it ~2 cells wide even though `unicodedata` calls it narrow, which is what made the
doctor table drift. `▲` (U+25B2, Geometric Shapes) is width-stable, so naive centering is correct.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

# state -> (unicode glyph, ascii glyph, color role). Glyphs are single display-column safe.
GLYPHS = {
    "ok":   {"unicode": "✓", "ascii": "[ OK ]", "color": "green"},   # ✓
    "fail": {"unicode": "✗", "ascii": "[FAIL]", "color": "red"},     # ✗
    "warn": {"unicode": "▲", "ascii": "[WARN]", "color": "yellow"},  # ▲ (NOT ⚠)
    "na":   {"unicode": "n/a",     "ascii": "[ N/A]", "color": "dim"},
}

# Blocks that render as exactly one terminal column in virtually every monospace font.
# EXCLUDES Miscellaneous Symbols (0x2600-0x26FF) — the width-ambiguous emoji-adjacent range.
_SAFE_GLYPH_BLOCKS = (
    (0x0000, 0x007F),   # Basic Latin
    (0x2500, 0x257F),   # Box Drawing      ─ │ └
    (0x25A0, 0x25FF),   # Geometric Shapes ▲ ● ○
    (0x2700, 0x27BF),   # Dingbats         ✓ ✗
)


def is_display_width_safe(ch: str) -> bool:
    """True if every char in ``ch`` is from a block that draws as one monospace column.
    Column-aligned glyphs (table/list cells) must satisfy this; prose banners need not."""
    return all(any(lo <= ord(c) <= hi for lo, hi in _SAFE_GLYPH_BLOCKS) for c in ch)


class Console:
    def __init__(
        self,
        *,
        stream=None,
        no_color: bool = False,
        ascii_mode: Optional[bool] = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = self._detect_color(no_color, self.stream)
        self.ascii = self._detect_ascii(ascii_mode, self.stream)

    @staticmethod
    def _detect_color(no_color_flag: bool, stream) -> bool:
        if no_color_flag or "NO_COLOR" in os.environ:  # presence, not truthiness (no-color.org)
            return False
        if os.environ.get("TERM") == "dumb":
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        try:
            return bool(stream.isatty())
        except Exception:
            return False

    @staticmethod
    def _detect_ascii(flag: Optional[bool], stream) -> bool:
        if flag is not None:
            return flag
        enc = getattr(stream, "encoding", None) or ""
        try:
            "✓✗▲".encode(enc or "utf-8")
            return False
        except (LookupError, UnicodeEncodeError):
            return True

    def _wrap(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.enabled else s

    def bold(self, s: str) -> str:   return self._wrap(_CODES["bold"], s)
    def dim(self, s: str) -> str:    return self._wrap(_CODES["dim"], s)
    def red(self, s: str) -> str:    return self._wrap(_CODES["red"], s)
    def green(self, s: str) -> str:  return self._wrap(_CODES["green"], s)
    def yellow(self, s: str) -> str: return self._wrap(_CODES["yellow"], s)
    def cyan(self, s: str) -> str:   return self._wrap(_CODES["cyan"], s)

    def glyph(self, state: str) -> str:
        """A colored, width-safe status token for ``state`` in {ok, fail, warn, na}."""
        g = GLYPHS.get(state, GLYPHS["na"])
        text = g["ascii"] if self.ascii else g["unicode"]
        color = g["color"]
        return self.dim(text) if color == "dim" else getattr(self, color)(text)

    def raw_glyph(self, state: str) -> str:
        """The uncolored glyph text (for width math), still respecting ascii mode."""
        g = GLYPHS.get(state, GLYPHS["na"])
        return g["ascii"] if self.ascii else g["unicode"]

    def status_cell(self, state: str, width: int) -> str:
        """A glyph centered in ``width`` columns THEN colored — so the (zero-width on screen but
        len()-counted) ANSI codes never corrupt the centering. This is the table-alignment fix."""
        g = GLYPHS.get(state, GLYPHS["na"])
        padded = self.raw_glyph(state).center(width)
        color = g["color"]
        return self.dim(padded) if color == "dim" else getattr(self, color)(padded)

    def rule(self, n: int) -> str:
        return self.dim(("-" if self.ascii else "─") * n)

    def header(self, subcommand: str, context: str = "") -> str:
        line = self.bold(f"codeintel {subcommand}")
        if context:
            line += f"  {self.dim('—')}  {self.dim(context)}"
        return line


# Auto-detected singletons; the CLI calls configure() once after parsing --no-color/--ascii.
c = Console(stream=sys.stdout)
c_err = Console(stream=sys.stderr)


def configure(*, no_color: bool = False, ascii_mode: Optional[bool] = None) -> None:
    """Re-detect both consoles honoring CLI flags (called once from __main__)."""
    global c, c_err
    c = Console(stream=sys.stdout, no_color=no_color, ascii_mode=ascii_mode)
    c_err = Console(stream=sys.stderr, no_color=no_color, ascii_mode=ascii_mode)


class LiveStep:
    """One status line for a slow op. On a TTY it redraws in place; on a pipe/CI/log it prints
    exactly one clean line when done — never leaks carriage-return/cursor bytes to a non-TTY."""

    def __init__(self, console: Console, label: str) -> None:
        self.c = console
        self.label = label
        try:
            self.live = console.enabled and console.stream.isatty()
        except Exception:
            self.live = False
        if self.live:
            try:
                console.stream.write(f"  {console.dim('…')} {label}\r")
                console.stream.flush()
            except Exception:
                self.live = False

    def done(self, state: str, detail: str = "") -> None:
        text = f"  {self.c.glyph(state)} {self.label}"
        if detail:
            text += f"  {self.c.dim(detail)}"
        try:
            if self.live:
                self.c.stream.write("\x1b[2K\r" + text + "\n")  # erase line, redraw
            else:
                self.c.stream.write(text + "\n")
            self.c.stream.flush()
        except Exception:
            pass
