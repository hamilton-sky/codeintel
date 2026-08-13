"""Syntax-aware chunking (0.6.0, Phase 1 of docs/roadmap-semantic.md).

Covers the six acceptance cases: def-aligned/non-overlapping/complete spans, decorator
inclusion, oversized-def splitting, syntax-error fallback, ``chunk_strategy="lines"`` parity
with the legacy windower, and per-file orphan reconciliation. Span helpers are pure (no DB, no
embedder); the orphan test drives a full index pass with a deterministic fake embedder.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from codeintel.indexer import Indexer, _project_key
from codeintel.semantic_db import SemanticDb


class _FakeTextEmbedding:
    """Deterministic stub: yields 384-dim numpy arrays filled with 0.1."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        return [np.full(384, 0.1, dtype=np.float32) for _ in list(texts)]


def _mem_db() -> SemanticDb:
    db = SemanticDb(":memory:")
    db.init()
    return db


def _chunk_starts(db: SemanticDb, file_path: str) -> set[int]:
    rows = db.conn().execute(
        "SELECT chunk_start FROM chunk_hashes WHERE file_path = ?", (file_path,)
    ).fetchall()
    return {r[0] for r in rows}


def _assert_gapless_cover(spans: list[tuple[int, int]], n: int) -> None:
    """spans cover [0, n) with no gaps, no empty/negative spans, and unique starts (no chunk_id
    collision). Overlap IS allowed — window-filled runs and oversized-def splits reuse the
    overlapping window/stride, exactly like the legacy line windower."""
    assert spans, "expected at least one span"
    assert len({s for s, _ in spans}) == len(spans), "duplicate start → chunk_id collision"
    ordered = sorted(spans)
    assert ordered[0][0] == 0, f"coverage must start at 0, got {ordered[0]}"
    frontier = 0
    for s, e in ordered:
        assert s < e, f"empty/negative span {(s, e)}"
        assert s <= frontier, f"gap before {(s, e)} (covered to {frontier})"
        frontier = max(frontier, e)
    assert frontier == n, f"coverage must reach {n}, reached {frontier}"


def _assert_non_overlapping(spans: list[tuple[int, int]]) -> None:
    """Stronger tiling check — used only where the fixture is genuinely def-aligned end to end."""
    for (a, b), (c, d) in zip(sorted(spans), sorted(spans)[1:]):
        assert b == c, f"overlap/gap between {(a, b)} and {(c, d)}"


# --------------------------------------------------------------------------- 1: def-aligned cover

def test_syntax_spans_are_def_aligned_non_overlapping_and_complete():
    src = (
        '"""Module docstring."""\n'      # 0
        "import os\n"                     # 1
        "import sys\n"                    # 2
        "\n"                              # 3
        "\n"                              # 4
        "def top_level():\n"             # 5
        "    return os.getcwd()\n"       # 6
        "\n"                              # 7
        "\n"                              # 8
        "class Widget:\n"                # 9
        '    """A widget."""\n'          # 10
        "\n"                              # 11
        "    def method_a(self):\n"      # 12
        "        return 1\n"             # 13
        "\n"                              # 14
        "    def method_b(self):\n"      # 15
        "        return sys.version\n"   # 16
    )
    lines = src.splitlines(keepends=True)
    idx = Indexer(_mem_db())
    spans = idx._chunk_python_ast(lines, src)

    _assert_gapless_cover(spans, len(lines))
    _assert_non_overlapping(spans)  # this fixture's runs are all < stride → genuine tiling

    starts = {s for s, _ in spans}
    # every real def/class line (0-based) is a chunk boundary
    assert {5, 9, 12, 15} <= starts

    def span_of(line: int) -> tuple[int, int]:
        return next((s, e) for (s, e) in spans if s <= line < e)

    # each def maps to a whole unit, not a mid-function window
    assert span_of(5) == (5, 7)      # top_level(): def line + body
    assert span_of(12) == (12, 14)   # method_a
    assert span_of(15) == (15, 17)   # method_b (contains the sys.version use)
    assert span_of(9)[0] == 9        # class header chunk starts on the class line
    # imports live in the leading module-level window, not inside any def
    assert span_of(1)[0] == 0


# --------------------------------------------------------------------------- 2: decorators

def test_decorated_function_span_includes_the_decorator():
    src = (
        "import functools\n"     # 0
        "\n"                      # 1
        "@functools.cache\n"     # 2
        "@staticmethod\n"        # 3
        "def decorated():\n"     # 4
        "    return 1\n"         # 5
    )
    lines = src.splitlines(keepends=True)
    spans = Indexer(_mem_db())._chunk_python_ast(lines, src)

    span = next((s, e) for (s, e) in spans if s <= 4 < e)
    assert span == (2, 6)  # begins at the first decorator (idx 2), not the def line (idx 4)
    _assert_gapless_cover(spans, len(lines))


# --------------------------------------------------------------------------- 3: oversized defs

def test_oversized_def_is_split_into_multiple_embeddable_chunks():
    body = "".join(f"    x{i} = {i}\n" for i in range(30))
    src = "def big():\n" + body                       # 1 + 30 = 31 lines
    lines = src.splitlines(keepends=True)
    idx = Indexer(_mem_db(), window=10, stride=10)     # max_chunk_lines = 2*window = 20

    spans = idx._chunk_python_ast(lines, src)

    assert len(spans) > 1                              # 31-line def (> 20) is split
    assert spans[0][0] == 0                            # first sub-chunk still starts at the def line
    for s, e in spans:
        assert e - s <= idx.window                     # every chunk is embeddable
    _assert_gapless_cover(spans, len(lines))
    _assert_non_overlapping(spans)                     # window == stride here → non-overlapping


def test_default_config_oversized_def_reuses_overlapping_windows():
    # The DEFAULT deployment config (window=20, stride=10) — where the legacy windower overlaps.
    # A >40-line def splits into overlapping 20-line windows: coverage stays complete and every
    # chunk stays embeddable, but adjacent windows overlap (documented, intended, == legacy).
    src = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(45))  # 46 lines > 2*20
    lines = src.splitlines(keepends=True)
    idx = Indexer(_mem_db())                           # window=20, stride=10, max_chunk_lines=40
    spans = idx._chunk_python_ast(lines, src)

    _assert_gapless_cover(spans, len(lines))           # complete, collision-free (overlap allowed)
    assert len(spans) > 1
    assert spans[0][0] == 0                            # first sub-chunk still starts at the def line
    for s, e in spans:
        assert e - s <= idx.window
    assert any(c < b for (a, b), (c, d) in zip(spans, spans[1:])), \
        "default window>stride must produce overlapping windows (matches the legacy windower)"


def test_def_at_max_chunk_lines_is_not_split():
    idx = Indexer(_mem_db(), window=5, stride=5)       # max_chunk_lines = 10
    src = "def f():\n" + "".join(f"    a{i} = {i}\n" for i in range(9))  # exactly 10 lines
    lines = src.splitlines(keepends=True)
    spans = idx._chunk_python_ast(lines, src)
    assert spans == [(0, 10)]                          # 10 <= 10 → kept whole


# --------------------------------------------------------------------------- 4: parse-failure fallback

def test_syntax_error_file_falls_back_to_windowing():
    src = "def broken(:\n    pass\n"                   # invalid syntax
    lines = src.splitlines(keepends=True)
    idx = Indexer(_mem_db())

    # the raw AST chunker surfaces the error (documented contract)...
    with pytest.raises(SyntaxError):
        idx._chunk_python_ast(lines, src)
    # ...and the per-file chooser swallows it and windows instead — never raising.
    spans = idx._spans_for_file(Path("broken.py"), lines, "broken.py")
    assert spans == idx._window_spans(0, len(lines))
    assert len(spans) > 0


def test_nul_byte_source_falls_back_without_raising():
    src = "def f():\n    return '\x00'\n"               # NUL → ast.parse raises ValueError
    lines = src.splitlines(keepends=True)
    idx = Indexer(_mem_db())
    spans = idx._spans_for_file(Path("nul.py"), lines, "nul.py")  # must not raise
    assert spans == idx._window_spans(0, len(lines))


def test_non_code_exts_window_but_treesitter_langs_do_not():
    # .md / unmapped exts are never parsed → line windows (as of 0.8.0 tree-sitter handles the
    # code langs; see tests/test_treesitter.py for the def-aligned behavior)
    csv = "".join(f"col{i},val{i}\n" for i in range(30))
    csv_lines = csv.splitlines(keepends=True)
    idx = Indexer(_mem_db())
    for name in ("data.md", "notes.txt"):
        assert idx._spans_for_file(Path(name), csv_lines, name) == idx._window_spans(0, len(csv_lines))
    # a real .ts file with definitions is def-aligned, NOT windowed
    ts = "function a() {\n  return 1;\n}\nfunction b() {\n  return 2;\n}\n".splitlines(keepends=True)
    ts_spans = idx._spans_for_file(Path("app.ts"), ts, "app.ts")
    assert ts_spans != idx._window_spans(0, len(ts))
    assert (0, 3) in ts_spans and (3, 6) in ts_spans  # each function its own chunk


# --------------------------------------------------------------------------- 5: "lines" parity

def test_lines_strategy_matches_legacy_window_output():
    src = (
        "def a():\n"          # 0
        "    return 1\n"      # 1
        "\n"                   # 2
        "def b():\n"          # 3
        "    return 2\n"      # 4
    )
    lines = src.splitlines(keepends=True)
    n = len(lines)
    fp = Path("mod.py")

    # the pre-0.6 windower: range(0, n, stride) with lines[s:s+window]
    legacy = [(s, min(s + 20, n)) for s in range(0, n, 10)]

    assert Indexer(_mem_db(), chunk_strategy="lines")._spans_for_file(fp, lines, "mod.py") == legacy

    syntax_spans = Indexer(_mem_db(), chunk_strategy="syntax")._spans_for_file(fp, lines, "mod.py")
    assert syntax_spans != legacy                       # syntax genuinely changes chunking
    assert (0, 2) in syntax_spans and (3, 5) in syntax_spans


def _legacy_chunks(root: Path, key: str, window: int, stride: int, max_chunks: int) -> set:
    """The pre-0.6 windower, reproduced verbatim: range(0, n, stride) with lines[s:s+window],
    whitespace-skip counting toward the per-file cap. Returns {(chunk_id, rel, start)}."""
    idx = Indexer(_mem_db())  # only to borrow _walk_files (identical walk both eras)
    out = set()
    for fp in idx._walk_files(root):
        with open(fp, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        rel = str(fp.relative_to(root))
        count = 0
        for s in range(0, len(lines), stride):
            if count >= max_chunks:
                break
            chunk_lines = lines[s:s + window]
            if not chunk_lines:
                break
            if not "".join(chunk_lines).strip():
                count += 1
                continue
            out.add((f"{key}:{rel}:{s}", rel, s))
            count += 1
    return out


def test_lines_mode_collect_matches_legacy_formula(tmp_path):
    # acceptance #5, full-pipeline (not just spans): lines-mode _collect_new_chunks emits exactly
    # the legacy windower's chunk set. (Also confirmed byte-identical against the real 0.5.0 source
    # out-of-band; this pins it permanently without a git dependency.)
    (tmp_path / "a.py").write_text("import os\n\n\ndef f():\n    return 1\n\n\nclass C:\n    def m(self):\n        return 2\n")
    (tmp_path / "b.py").write_text("".join(f"v{i} = {i}\n" for i in range(33)))
    (tmp_path / "c.md").write_text("# h\n\n" + "".join(f"para {i}\n" for i in range(25)))

    real = os.path.realpath(str(tmp_path))
    key = _project_key(real)
    db = _mem_db()
    got = {(c[0], c[2], c[3]) for c in Indexer(db, chunk_strategy="lines")._collect_new_chunks(
        tmp_path, key, real)}
    expected = _legacy_chunks(tmp_path, key, window=20, stride=10, max_chunks=500)
    assert got == expected


def test_unknown_strategy_degrades_to_syntax():
    # a direct caller with a typo must not silently disable chunking
    idx = Indexer(_mem_db(), chunk_strategy="tree-sitter")
    assert idx.chunk_strategy == "syntax"


def test_strategy_guard_is_case_insensitive():
    # a direct caller's casing must be honored, not silently swapped to the default (matches
    # config._coerce, which lowercases enum values)
    assert Indexer(_mem_db(), chunk_strategy="LINES").chunk_strategy == "lines"
    assert Indexer(_mem_db(), chunk_strategy=" Syntax ").chunk_strategy == "syntax"


def test_bad_numeric_params_degrade_to_defaults():
    # a direct caller must not be able to set a stride/window that raises in range() or silently
    # drops regions — mirrors config._coerce's positive-int clamp
    assert Indexer(_mem_db(), stride=0).stride == 10
    assert Indexer(_mem_db(), stride=-5).stride == 10
    assert Indexer(_mem_db(), window=0).window == 20
    assert Indexer(_mem_db(), window="oops").window == 20
    assert Indexer(_mem_db(), max_chunks=-1).max_chunks == 500
    # and a degraded stride still yields a usable (non-raising) window pass
    idx = Indexer(_mem_db(), stride=0)
    assert idx._window_spans(0, 25) == [(0, 20), (10, 25), (20, 25)]  # default stride=10 in effect


# --------------------------------------------------------------------------- 6: orphan reconciliation

def test_reindex_after_removing_function_drops_orphan_chunk(tmp_path):
    mod = tmp_path / "m.py"
    mod.write_text("def keep():\n    return 1\n\ndef drop_me():\n    return 2\n")

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        db = _mem_db()
        idx = Indexer(db)
        assert idx.index(str(tmp_path)) > 0
        assert _chunk_starts(db, "m.py") == {0, 3}      # keep()@0, drop_me()@3

        mod.write_text("def keep():\n    return 1\n")   # drop_me() removed
        idx.index(str(tmp_path))

        assert _chunk_starts(db, "m.py") == {0}         # orphan reconciled away

        real = os.path.realpath(str(tmp_path))
        orphan_id = f"{_project_key(real)}:m.py:3"
        emb = db.conn().execute(
            "SELECT COUNT(*) FROM code_embeddings WHERE chunk_id = ?", (orphan_id,)
        ).fetchone()[0]
        assert emb == 0                                 # embedding row dropped from both tables


def test_orphan_reconcile_is_scoped_and_leaves_other_files_intact(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        assert _chunk_starts(db, "a.py") == {0}
        assert _chunk_starts(db, "b.py") == {0}

        # editing a.py must not disturb b.py's rows
        (tmp_path / "a.py").write_text("def a():\n    return 1\n\ndef a2():\n    return 3\n")
        Indexer(db).index(str(tmp_path))

    assert _chunk_starts(db, "a.py") == {0, 3}          # new def added
    assert _chunk_starts(db, "b.py") == {0}             # untouched


def test_switching_strategy_reconciles_stale_line_chunks(tmp_path):
    # a file first indexed with "lines" then re-indexed with "syntax": the old window chunk_ids
    # that syntax no longer produces must be reconciled away (mixed index converges on re-index).
    mod = tmp_path / "big.py"
    body = "".join(f"    v{i} = {i}\n" for i in range(24))
    mod.write_text("def wide():\n" + body)              # 25 lines → several 20-line windows

    with patch("fastembed.TextEmbedding", _FakeTextEmbedding):
        db = _mem_db()
        Indexer(db, chunk_strategy="lines").index(str(tmp_path))
        line_starts = _chunk_starts(db, "big.py")
        assert line_starts == {0, 10, 20}               # legacy stride-aligned windows

        Indexer(db, chunk_strategy="syntax").index(str(tmp_path))
        syntax_starts = _chunk_starts(db, "big.py")

    # 25-line def > max_chunk_lines(40)? no — 25 <= 40, so it becomes ONE def chunk at line 0.
    assert syntax_starts == {0}
