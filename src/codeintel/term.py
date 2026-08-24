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
import threading
import time

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
        ascii_mode: bool | None = None,
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
    def _detect_ascii(flag: bool | None, stream) -> bool:
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

    def ellipsis(self) -> str:
        """The in-progress marker, ascii-safe (`...` when the stream can't encode `…`)."""
        return "..." if self.ascii else "…"

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


def configure(*, no_color: bool = False, ascii_mode: bool | None = None) -> None:
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
                console.stream.write(f"  {console.dim(console.ellipsis())} {label}\r")
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


def _fmt_elapsed(seconds: float) -> str:
    """Compact elapsed time: `38s`, then `4m12s` once past a minute (seconds zero-padded so the
    string width stops jittering while a live line ticks)."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s"


class LiveCounter:
    """A multi-phase live-progress line for slow, count-bearing work (`codeintel index`).

    It duck-types ``progress.ProgressSink`` — the indexer drives it through ``scan``/``load_model``/
    ``embed`` without importing ``term`` — and owns *everything* terminal: TTY detection, redraw
    throttling, ANSI, and the phase→glyph vocabulary (the same ``✓`` / two-space checklist as
    ``LiveStep`` and ``doctor``). The indexer owns only *when* to emit and the raw counts.

    On a TTY it redraws the active phase in place (throttled to ``min_interval``) and commits each
    finished phase as a permanent ``  ✓ <phase>  <detail>  <elapsed>`` row. On a pipe/CI it prints
    one plain, glyph-less ``<phase>: <detail>`` line per phase (and per ``heartbeat`` while a phase
    runs long) — never a carriage return, never a spinner, so a log reader can always tell "still
    going" from a committed ``✓`` line. Every method swallows its own errors: a rendering fault must
    never disturb indexing (the indexer already routes these through ``progress._Guard`` too — this
    is defence in depth, and it also protects the CLI-side ``finish()`` call)."""

    def __init__(self, console: Console, *, min_interval: float = 0.1, heartbeat: float = 2.0) -> None:
        self.c = console
        try:
            self.live = console.enabled and console.stream.isatty()
        except Exception:
            self.live = False
        self._min = min_interval
        self._beat = heartbeat
        self._phase: str | None = None
        self._phase_start = 0.0
        self._last_draw = 0.0
        self._cur_label = ""
        self._cur_detail = ""

    # ---- ProgressSink surface (duck-typed) -------------------------------------------------
    def scan(self, files: int, chunks: int) -> None:
        self._tick("scan", "scan + chunk", f"{files:,} files, {chunks:,} chunks")

    def load_model(self) -> None:
        # Pre-roll of the embed phase: a distinct "downloading, not hung" line for a cold cache.
        self._tick("embed", "embed", f"loading embedding model{self.c.ellipsis()}")

    def embed(self, done: int, total: int) -> None:
        pct = 0 if total <= 0 else int(done * 100 / total)
        self._tick("embed", "embed", f"{done:,}/{total:,} chunks  {pct}%",
                   force=(total > 0 and done >= total))

    # ---- internals -------------------------------------------------------------------------
    def _tick(self, phase: str, label: str, detail: str, *, force: bool = False) -> None:
        try:
            now = time.monotonic()
            new_phase = phase != self._phase
            if new_phase:
                self._commit(now)          # finish the previous phase's row before switching
                self._phase = phase
                self._phase_start = now
                self._last_draw = 0.0
            # Record the latest text unconditionally so a throttled tick still feeds an accurate
            # committed row (the indexer's final scan()/embed() counts arrive this way).
            self._cur_label, self._cur_detail = label, detail
            gap = self._min if self.live else self._beat
            if not force and not new_phase and self._last_draw and (now - self._last_draw) < gap:
                return
            self._last_draw = now
            self._draw(label, detail)
        except Exception:
            pass

    def _draw(self, label: str, detail: str) -> None:
        try:
            if self.live:
                line = f"\x1b[2K\r  {self.c.dim(self.c.ellipsis())} {label}"
                if detail:
                    line += f"  {detail}"
                self.c.stream.write(line)
            else:
                # Glyph-less on purpose: the ✓ is reserved for the committed line, so a mid-run
                # heartbeat in a log never *looks* finished.
                self.c.stream.write(f"  {label}: {detail}\n" if detail else f"  {label}\n")
            self.c.stream.flush()
        except Exception:
            pass

    def _commit(self, now: float) -> None:
        if self._phase is None:
            return
        elapsed = _fmt_elapsed(now - self._phase_start)
        tail = f"{self._cur_detail}  {elapsed}" if self._cur_detail else elapsed
        line = f"  {self.c.glyph('ok')} {self._cur_label}  {self.c.dim(tail)}"
        try:
            self.c.stream.write(("\x1b[2K\r" + line + "\n") if self.live else (line + "\n"))
            self.c.stream.flush()
        except Exception:
            pass

    def finish(self, *, commit: bool = True) -> None:
        """End the run. ``commit=True`` seals the active phase with its ``✓`` row; ``commit=False``
        (a no-op / failed pass) erases the dangling live line on a TTY and leaves no row, so the
        caller's ``Nothing new`` / failure message stands alone."""
        try:
            if commit and self._phase is not None:
                self._commit(time.monotonic())
            elif self.live and self._phase is not None:
                self.c.stream.write("\x1b[2K\r")
                self.c.stream.flush()
            self._phase = None
        except Exception:
            pass


class LiveHeartbeat:
    """A ticking elapsed-time heartbeat for one slow, *opaque* op — work with no progress counts to
    show, like waiting on a subprocess (`codeintel index`'s graph reindex). It answers the only
    question a heartbeat must ("is it still alive?") with a changing number rather than a spinner
    glyph, so it speaks the same one-idiom visual language as ``LiveCounter`` (a static dim ``…``,
    something next to it that moves).

    A background daemon thread does the ticking so the caller can make its blocking call inline:

        beat = LiveHeartbeat(console, "graph reindex").start()
        try:
            do_the_blocking_thing()
            beat.stop("ok")
        except Exception:
            beat.stop("warn", "skipped")

    On a TTY it redraws ``  … <label>  <elapsed>`` in place about once a second; on a pipe/CI it
    prints a plain ``  <label>… <elapsed>`` line every ``heartbeat`` seconds (no carriage returns,
    so a log stays clean and a silence-timeout never fires). ``stop`` joins the thread *before* it
    writes the single committed ``  <glyph> <label>  <detail|elapsed>`` line, so the ticker and the
    commit never interleave. Every method swallows its own errors — a heartbeat can never disturb
    the work it is timing."""

    def __init__(self, console: Console, label: str, *, tick: float = 1.0, heartbeat: float = 5.0) -> None:
        self.c = console
        self.label = label
        self._tick = tick
        self._beat = heartbeat
        try:
            self.live = console.enabled and console.stream.isatty()
        except Exception:
            self.live = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0

    def start(self) -> LiveHeartbeat:
        self._start = time.monotonic()
        if self.live:
            try:
                self.c.stream.write(f"  {self.c.dim(self.c.ellipsis())} {self.label}")
                self.c.stream.flush()
            except Exception:
                self.live = False
        try:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        except Exception:
            self._thread = None
        return self

    def _run(self) -> None:
        last_sec = -1
        next_beat = self._beat
        # Event.wait doubles as the sleep AND the stop signal: it returns True the instant stop()
        # sets the event, so the loop never ticks once more after being told to stop.
        while not self._stop.wait(self._tick):
            try:
                elapsed = time.monotonic() - self._start
                if self.live:
                    sec = int(elapsed)
                    if sec != last_sec:                       # redraw only when the second changes
                        last_sec = sec
                        self.c.stream.write(
                            f"\x1b[2K\r  {self.c.dim(self.c.ellipsis())} {self.label}  "
                            f"{self.c.dim(_fmt_elapsed(elapsed))}")
                        self.c.stream.flush()
                elif elapsed >= next_beat:                     # non-TTY: one plain line per interval
                    next_beat += self._beat
                    self.c.stream.write(f"  {self.label}{self.c.ellipsis()} {_fmt_elapsed(elapsed)}\n")
                    self.c.stream.flush()
            except Exception:
                return

    def stop(self, state: str = "ok", detail: str = "") -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=1.0)              # ensure the ticker is done before we write
            except Exception:
                pass
        try:
            tail = _fmt_elapsed(time.monotonic() - self._start)
            if detail:
                tail = f"{detail}  {tail}"
            line = f"  {self.c.glyph(state)} {self.label}  {self.c.dim(tail)}"
            self.c.stream.write(("\x1b[2K\r" + line + "\n") if self.live else (line + "\n"))
            self.c.stream.flush()
        except Exception:
            pass
