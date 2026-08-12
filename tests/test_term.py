"""term.py — the shared CLI output system: glyph width-safety, color gating, ASCII fallback."""
from __future__ import annotations

from codeintel.term import GLYPHS, Console, is_display_width_safe


class _FakeTTY:
    encoding = "utf-8"

    def isatty(self):
        return True


def test_status_glyphs_are_display_width_safe():
    # Regression guard: table/list glyphs must come from single-column-safe blocks, so nobody
    # reintroduces a width-ambiguous glyph (e.g. ⚠ U+26A0) that silently misaligns the columns.
    for name, g in GLYPHS.items():
        assert is_display_width_safe(g["unicode"]), f"{name}={g['unicode']!r} may misalign in columns"


def test_warn_glyph_is_not_the_ambiguous_warning_sign():
    assert GLYPHS["warn"]["unicode"] == "▲"      # Geometric Shapes, width-stable
    assert GLYPHS["warn"]["unicode"] != "⚠"  # NOT ⚠ (Misc Symbols; drawn ~2 cols)


def test_detect_color_precedence(monkeypatch):
    for var in ("NO_COLOR", "TERM", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    tty = _FakeTTY()
    assert Console._detect_color(False, tty) is True            # tty, nothing set → on
    assert Console._detect_color(True, tty) is False            # --no-color flag → off
    monkeypatch.setenv("NO_COLOR", "")                          # presence, even empty → off
    assert Console._detect_color(False, tty) is False
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert Console._detect_color(False, tty) is False


def test_color_disabled_is_identity():
    c = Console(no_color=True)
    assert c.green("x") == "x" and c.bold("y") == "y"


def test_color_enabled_wraps_ansi():
    c = Console(no_color=True)
    c.enabled = True
    assert c.green("x") == "\x1b[32mx\x1b[0m"


def test_status_cell_exact_width_without_color():
    c = Console(no_color=True)      # no ANSI → the cell string width equals the column width
    c.ascii = False
    assert len(c.status_cell("ok", 11)) == 11
    assert len(c.status_cell("warn", 10)) == 10


def test_ascii_fallback_tokens():
    c = Console(no_color=True)
    c.ascii = True
    assert c.raw_glyph("ok") == "[ OK ]"
    assert c.raw_glyph("fail") == "[FAIL]"
