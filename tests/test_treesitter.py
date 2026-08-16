"""Tree-sitter chunking (0.8.0, Phase 3 of docs/roadmap-semantic.md).

Extends 0.6.0 syntax-aware chunking to non-Python languages (TS/JS/Go/Rust/Java/C/C++) via
tree-sitter, behind the same `_primary_spans → _cover` pipeline. `tree-sitter-language-pack` is a
normal dependency; these tests exercise real grammars, plus the graceful fallback to line-windowing
when a parser is unavailable or a file fails to yield defs.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codeintel.indexer import _INDEXED_EXTS, _TS_LANG_BY_EXT, Indexer
from codeintel.semantic_db import SemanticDb

# A normal dependency — but skip cleanly rather than error if a platform lacks a wheel.
pytest.importorskip("tree_sitter_language_pack")


def _idx(**kw) -> Indexer:
    db = SemanticDb(":memory:")
    db.init()
    return Indexer(db, **kw)


def _spans(name: str, src: str, **kw):
    lines = src.splitlines(keepends=True)
    return _idx(**kw)._spans_for_file(Path(name), lines, name), len(lines)


def _assert_gapless_cover(spans, n):
    assert spans and spans[0][0] == 0, f"must start at 0: {spans}"
    assert len({s for s, _ in spans}) == len(spans), "duplicate start → chunk_id collision"
    frontier = 0
    for s, e in sorted(spans):
        assert s < e, f"empty/negative span {(s, e)}"
        assert s <= frontier, f"gap before {(s, e)} (covered to {frontier})"
        frontier = max(frontier, e)
    assert frontier == n, f"must cover to {n}, reached {frontier}"


# --------------------------------------------------------------------------- per-language def-alignment

def test_typescript_class_and_function_are_def_aligned():
    src = (
        "export class Widget {\n"      # 0  class header
        "  greet(name: string) {\n"    # 1  method
        "    return name;\n"           # 2
        "  }\n"                         # 3
        "}\n"                           # 4
        "function top() {\n"           # 5  top-level function
        "  return 1;\n"                # 6
        "}\n"                           # 7
    )
    spans, n = _spans("a.ts", src)
    _assert_gapless_cover(spans, n)
    starts = {s for s, _ in spans}
    assert 0 in starts   # class header chunk begins on the class line
    assert 1 in starts   # the method is its own chunk (not shadowed by a whole-class chunk)
    assert 5 in starts   # the top-level function is its own chunk


def test_go_functions_and_methods():
    src = ("package p\n\n"
           "func Free() int {\n\treturn 1\n}\n\n"
           "func (t T) Method() int {\n\treturn 2\n}\n")
    spans, n = _spans("b.go", src)
    _assert_gapless_cover(spans, n)
    starts = {s for s, _ in spans}
    assert 2 in starts   # func Free at line 2 (0-based)
    assert 6 in starts   # method at line 6


def test_rust_impl_method_is_its_own_chunk():
    src = ("struct S { x: i32 }\n\n"
           "impl S {\n"
           "    fn method(&self) -> i32 {\n"
           "        self.x\n"
           "    }\n"
           "}\n")
    spans, n = _spans("c.rs", src)
    _assert_gapless_cover(spans, n)
    assert 3 in {s for s, _ in spans}   # fn method at line 3, carved out of the impl block


def test_java_class_method():
    src = ("class A {\n"
           "    void m() {\n"
           "        return;\n"
           "    }\n"
           "}\n")
    spans, n = _spans("d.java", src)
    _assert_gapless_cover(spans, n)
    starts = {s for s, _ in spans}
    assert 0 in starts and 1 in starts   # class header + method m


def test_arrow_function_consts_are_def_aligned():
    # React components / hooks / callbacks are `const X = () => {}`, not `function` declarations —
    # they must def-align too (the common modern-TS/React case); a plain data const must NOT.
    src = (
        "import x from 'y';\n"              # 0
        "\n"                                # 1
        "export const Header = () => {\n"   # 2  arrow component
        "  return null;\n"                  # 3
        "};\n"                              # 4
        "\n"                                # 5
        "const useThing = (id) => {\n"      # 6  arrow hook
        "  return id;\n"                    # 7
        "};\n"                              # 8
        "\n"                                # 9
        "const PLAIN = 42;\n"               # 10 plain data const — NOT a def
    )
    spans, n = _spans("c.tsx", src)
    _assert_gapless_cover(spans, n)
    starts = {s for s, _ in spans}
    assert 2 in starts and 6 in starts   # both arrow consts are their own chunks
    assert 10 not in starts              # a plain `const x = 42` is not a def chunk


def test_cpp_class_and_free_function():
    src = ("class C {\npublic:\n  int m() { return 1; }\n};\nint free_fn() { return 2; }\n")
    spans, n = _spans("e.cpp", src)
    _assert_gapless_cover(spans, n)


def test_multiple_functions_are_separate_chunks():
    src = "function a() {\n  return 1;\n}\nfunction b() {\n  return 2;\n}\n"
    spans, n = _spans("m.ts", src)
    _assert_gapless_cover(spans, n)
    assert (0, 3) in spans and (3, 6) in spans   # each function whole and distinct


# --------------------------------------------------------------------------- fallbacks / never-raise

def test_error_tolerant_source_still_covers_and_never_raises():
    spans, n = _spans("bad.js", "this is @#$ not valid javascript !!!\n")
    _assert_gapless_cover(spans, n)   # tree-sitter is error-tolerant → partial tree, no raise


def test_lines_strategy_bypasses_treesitter():
    src = "function foo() {\n  return 1;\n}\n"
    lines = src.splitlines(keepends=True)
    idx = _idx(chunk_strategy="lines")
    assert idx._spans_for_file(Path("x.ts"), lines, "x.ts") == idx._window_spans(0, len(lines))


def test_unmapped_exts_window():
    src = "".join(f"line {i}\n" for i in range(25))
    lines = src.splitlines(keepends=True)
    idx = _idx()
    for name in ("readme.md", "notes.txt"):
        assert idx._spans_for_file(Path(name), lines, name) == idx._window_spans(0, len(lines))


def test_falls_back_to_windowing_when_parser_unavailable():
    src = "function foo() {\n  return 1;\n}\n"
    lines = src.splitlines(keepends=True)
    idx = _idx()
    with patch.object(idx, "_get_ts_parser", return_value=None):   # simulate the dep not installed
        assert idx._spans_for_file(Path("x.ts"), lines, "x.ts") == idx._window_spans(0, len(lines))


def test_never_raises_on_parser_exception():
    src = "function foo() {\n  return 1;\n}\n"
    lines = src.splitlines(keepends=True)
    idx = _idx()

    class _Boom:
        def parse(self, *a, **k):
            raise RuntimeError("grammar exploded")

    with patch.object(idx, "_get_ts_parser", return_value=_Boom()):
        assert idx._spans_for_file(Path("x.ts"), lines, "x.ts") == idx._window_spans(0, len(lines))


def test_parser_cache_loads_each_language_once():
    idx = _idx()
    with patch("tree_sitter_language_pack.get_parser") as gp:
        gp.return_value = object()
        idx._get_ts_parser("go")
        idx._get_ts_parser("go")
        idx._get_ts_parser("rust")
    assert gp.call_count == 2   # one load per distinct language, then cached


def test_typescript_interface_decomposes_into_members():
    # interface method members are `method_signature` (distinct from abstract-class members); the
    # interface must decompose into a header + per-method chunks, not one whole-interface chunk
    src = ("interface Repo {\n"        # 0  header
           "  get(id: string): Item;\n"   # 1  method_signature
           "  put(item: Item): void;\n"   # 2  method_signature
           "}\n")                          # 3
    spans, n = _spans("r.ts", src)
    _assert_gapless_cover(spans, n)
    starts = {s for s, _ in spans}
    assert 1 in starts and 2 in starts, f"interface methods must be their own chunks: {spans}"


def test_lang_map_and_indexed_exts_agree_both_ways():
    code_exts = _INDEXED_EXTS - {".py", ".md"}
    # every walked code ext maps to a tree-sitter language...
    assert code_exts <= set(_TS_LANG_BY_EXT), f"walked but unmapped: {code_exts - set(_TS_LANG_BY_EXT)}"
    # ...AND every mapped ext is actually walked (else it's indexed by nothing — silent omission)
    assert set(_TS_LANG_BY_EXT) <= _INDEXED_EXTS, \
        f"mapped but never walked: {set(_TS_LANG_BY_EXT) - _INDEXED_EXTS}"
