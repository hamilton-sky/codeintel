"""Live-progress for `codeintel index` — the seam, the renderer, and the indexer callsites.

The load-bearing guarantee proved here: progress is *inert to the result*. An index pass returns
the identical chunk count whether progress is off, recording, or actively throwing — because every
emit routes through ``progress._Guard`` (null-safe + never-raise). The renderer (`term.LiveCounter`)
is proved to leak no carriage-return / ANSI bytes on a non-TTY, the one thing that would corrupt a
piped or CI log."""
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import numpy as np

from codeintel.indexer import Indexer
from codeintel.progress import _Guard
from codeintel.semantic_db import SemanticDb
from codeintel.term import Console, LiveCounter, _fmt_elapsed


class _FakeTextEmbedding:
    """Deterministic offline stub — 384-dim vectors, no fastembed, no network."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        return [np.full(384, 0.1, dtype=np.float32) for _ in list(texts)]


class RecordingSink:
    """A ProgressSink that just remembers what the indexer told it."""

    def __init__(self):
        self.scans: list[tuple[int, int]] = []
        self.embeds: list[tuple[int, int]] = []
        self.loaded = 0

    def scan(self, files, chunks):
        self.scans.append((files, chunks))

    def load_model(self):
        self.loaded += 1

    def embed(self, done, total):
        self.embeds.append((done, total))


class BoomSink:
    """Every method raises — to prove _Guard swallows it and the count is untouched."""

    def scan(self, *a):
        raise RuntimeError("boom")

    def load_model(self, *a):
        raise RuntimeError("boom")

    def embed(self, *a):
        raise RuntimeError("boom")


def _mem_db() -> SemanticDb:
    db = SemanticDb(":memory:")
    db.init()
    return db


def _corpus(tmp_path, n=40) -> None:
    """n tiny top-level defs → n def-aligned chunks → spans >1 embed batch (batch size is 32)."""
    src = "\n\n".join(f"def f{i}():\n    return {i}" for i in range(n))
    (tmp_path / "big.py").write_text(src + "\n")


# --------------------------------------------------------------------------- _Guard: never-raise

def test_guard_is_a_noop_when_the_sink_is_none():
    g = _Guard(None)
    g.scan(1, 2)          # must not raise, must do nothing observable
    g.load_model()
    g.embed(3, 4)


def test_guard_swallows_every_exception_from_a_broken_sink():
    g = _Guard(BoomSink())
    g.scan(1, 2)          # each would raise if unguarded
    g.load_model()
    g.embed(3, 4)


# --------------------------------------------------------------------------- indexer callsites

def test_progress_off_and_on_yield_the_identical_count(tmp_path):
    _corpus(tmp_path)
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        n_off = Indexer(_mem_db(), progress=None).index(str(tmp_path))
        rec = RecordingSink()
        n_on = Indexer(_mem_db(), progress=rec).index(str(tmp_path))
    assert n_off == n_on > 0                                   # a recording sink changes nothing

    total = rec.embeds[-1][1]
    assert n_on == total                                        # every chunk embedded (fake never fails)
    assert rec.loaded == 1                                      # model-load signalled exactly once
    assert rec.embeds[0] == (0, total)                          # starts at 0 …
    assert rec.embeds[-1] == (total, total)                     # … lands on 100%
    dones = [d for d, _ in rec.embeds]
    assert dones == sorted(dones)                               # embed `done` is monotonic
    assert len({t for _, t in rec.embeds}) == 1                 # total is constant
    assert len(rec.embeds) >= 3                                 # 0%, ≥1 mid batch, 100% (>32 chunks)
    files = [f for f, _ in rec.scans]
    assert files == sorted(files)                               # scan file-count never goes backwards


def test_a_throwing_sink_never_changes_the_indexed_count(tmp_path):
    _corpus(tmp_path)
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        n_clean = Indexer(_mem_db(), progress=None).index(str(tmp_path))
        n_boom = Indexer(_mem_db(), progress=BoomSink()).index(str(tmp_path))
    assert n_clean == n_boom > 0


# --------------------------------------------------------------------------- renderer: non-TTY safety

def test_livecounter_on_a_nontty_leaks_no_control_bytes():
    buf = StringIO()                                   # StringIO.isatty() is False → non-TTY path
    lc = LiveCounter(Console(stream=buf))
    assert lc.live is False
    lc.scan(1, 10)
    lc.scan(5, 120)
    lc.load_model()
    lc.embed(0, 120)
    lc.embed(64, 120)
    lc.embed(120, 120)
    lc.finish(commit=True)

    out = buf.getvalue()
    assert "\r" not in out                             # no carriage returns into a pipe/log
    assert "\x1b" not in out                           # no ANSI escapes either
    assert "scan + chunk" in out and "embed" in out    # both phases are legible
    assert "✓" in out                                  # committed rows are marked


def test_livecounter_finish_without_commit_is_safe_and_silent_on_nontty():
    buf = StringIO()
    lc = LiveCounter(Console(stream=buf))
    lc.scan(1, 0)
    lc.finish(commit=False)                            # the "Nothing new" / failure path
    out = buf.getvalue()
    assert "\r" not in out and "\x1b" not in out
    assert "✓" not in out                              # nothing was committed


# --------------------------------------------------------------------------- elapsed formatting

def test_fmt_elapsed_is_stable_width_past_a_minute():
    assert _fmt_elapsed(0) == "0s"
    assert _fmt_elapsed(38) == "38s"
    assert _fmt_elapsed(63) == "1m03s"                 # seconds zero-padded once minutes appear
    assert _fmt_elapsed(252) == "4m12s"
