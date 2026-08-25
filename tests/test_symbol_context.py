"""Chunks that start inside a definition must say which definition.

A def longer than ``max_chunk_lines`` is window-split, so most of its chunks open mid-body and the
preview shows whatever line the window happened to start on. Real searches returned
``searcher.py:373 | continue`` and ``searcher.py:383 | except Exception as exc:`` — correctly
located, and useless: nothing tells the reader which function they are looking at. Measured with
``ast`` across the indexed repositories, 11-33% of Python chunks start strictly inside a definition.

The parser already knows the enclosing symbol at chunk time, so it is recorded
(``chunk_hashes.chunk_symbol``) and the preview leads with it.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from codeintel.indexer import Indexer
from codeintel.providers.semantic import _preview
from codeintel.searcher import Searcher
from codeintel.semantic_db import SemanticDb


class _Flat:
    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        v = np.zeros(384, dtype=np.float32)
        v[0] = 1.0
        return [v for _ in texts]


def _mem_db() -> SemanticDb:
    db = SemanticDb(":memory:")
    db.init()
    return db


def _long_def(name: str, body_lines: int, marker: str = "pass") -> str:
    body = "\n".join(f"    x{i} = {i}" for i in range(body_lines))
    return f"def {name}(arg):\n{body}\n    {marker}\n"


# --------------------------------------------------------------------- the reported symptom

def test_a_chunk_inside_a_split_def_records_the_enclosing_symbol(tmp_path):
    # 120 lines >> max_chunk_lines (2*window=40) → window-split, so most chunks open mid-body.
    (tmp_path / "m.py").write_text(_long_def("process_payment", 120))
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))

    rows = db.conn().execute(
        "SELECT chunk_start, chunk_symbol FROM chunk_hashes ORDER BY chunk_start"
    ).fetchall()
    assert len(rows) > 1, "a 120-line def must have been window-split"
    assert all(r[1] == "process_payment" for r in rows), \
        "every chunk of a split def belongs to that def"


def test_the_preview_leads_with_the_symbol_for_a_mid_body_chunk():
    """The whole point: `continue` alone cannot be judged; `process_payment() … continue` can."""
    assert _preview({"snippet": "    continue\n", "symbol": "process_payment"}) \
        == "process_payment() … continue"


def test_the_preview_does_not_repeat_a_name_the_line_already_carries():
    """A chunk that starts AT its def already names it — prefixing would read as `f() … def f(`."""
    assert _preview({"snippet": "def parse_config(path):\n", "symbol": "parse_config"}) \
        == "def parse_config(path):"


def test_a_module_level_chunk_gets_no_symbol_and_no_prefix(tmp_path):
    (tmp_path / "m.py").write_text("import os\nimport sys\n\nCONST = 1\n")
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
    rows = db.conn().execute("SELECT chunk_symbol FROM chunk_hashes").fetchall()
    assert rows and all(r[0] is None for r in rows), "module level is not inside any definition"
    assert _preview({"snippet": "import os\n", "symbol": None}) == "import os"


def test_the_innermost_definition_wins(tmp_path):
    """A method inside a class must report the METHOD. Reporting the class would be technically
    true and practically useless — it is the same answer for every method in the file."""
    src = (
        "class Gateway:\n"
        "    def outer(self):\n"
        + "".join(f"        y{i} = {i}\n" for i in range(60))
        + "        def inner_helper():\n"
        + "".join(f"            z{i} = {i}\n" for i in range(60))
        + "            return 1\n"
        "        return inner_helper()\n"
    )
    (tmp_path / "m.py").write_text(src)
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
    syms = {r[0] for r in db.conn().execute(
        "SELECT DISTINCT chunk_symbol FROM chunk_hashes").fetchall()}
    assert "inner_helper" in syms, "a chunk inside the nested def must report the nested def"
    assert "outer" in syms


def test_typescript_chunks_carry_their_symbol(tmp_path):
    """tree-sitter languages go through a different frontend; it must attribute symbols too."""
    body = "".join(f"  const v{i} = {i};\n" for i in range(120))
    (tmp_path / "app.ts").write_text(f"export function handleRequest(req: Request) {{\n{body}}}\n")
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
    rows = db.conn().execute("SELECT chunk_symbol FROM chunk_hashes").fetchall()
    assert rows, "the .ts file should have been indexed"
    assert any(r[0] == "handleRequest" for r in rows)


def test_an_arrow_function_component_is_named(tmp_path):
    """`const X = () => {}` is how most modern TS is written; the name is a level deeper."""
    body = "".join(f"  const v{i} = {i};\n" for i in range(120))
    (tmp_path / "c.tsx").write_text(f"export const UserPanel = () => {{\n{body}  return null;\n}};\n")
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
    rows = db.conn().execute("SELECT chunk_symbol FROM chunk_hashes").fetchall()
    assert any(r[0] == "UserPanel" for r in rows)


# --------------------------------------------------------------------- robustness / migration

def test_an_unparseable_file_reports_no_symbols_rather_than_guessing(tmp_path):
    """A file we could not parse is one whose symbols we do not know. Attributing a chunk to a
    function found by scanning backwards for `def ` would confidently name the wrong one."""
    (tmp_path / "broken.py").write_text("def f(:\n    this is not python\n" * 30)
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
    rows = db.conn().execute("SELECT chunk_symbol FROM chunk_hashes").fetchall()
    assert rows, "a broken file must still index (windowed fallback)"
    assert all(r[0] is None for r in rows)


def test_symbols_backfill_in_place_without_re_embedding(tmp_path):
    """Same migration contract as chunk_end: an existing cache gains symbols from one ordinary
    index pass, not a full re-embed of every project sharing the file."""
    (tmp_path / "m.py").write_text(_long_def("process_payment", 120))
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        db.conn().execute("UPDATE chunk_hashes SET chunk_symbol = NULL")
        db.conn().commit()

        idx = Indexer(db)
        embedded = idx.index(str(tmp_path))

    rows = db.conn().execute("SELECT chunk_symbol FROM chunk_hashes").fetchall()
    assert all(r[0] == "process_payment" for r in rows), "every symbol backfilled"
    assert embedded == 0, "unchanged content must not be re-embedded"
    assert idx._backfilled == len(rows)


def test_the_symbol_reaches_search_results(tmp_path):
    """End to end: indexer -> chunk_hashes -> Searcher result dict."""
    (tmp_path / "m.py").write_text(_long_def("process_payment", 120, marker="settle()"))
    with patch("fastembed.TextEmbedding", _Flat):
        db = _mem_db()
        Indexer(db).index(str(tmp_path))
        res = Searcher(db).search("settle", str(tmp_path), cosine_floor=-1.0)

    assert res
    assert any(r.get("symbol") == "process_payment" for r in res), \
        "search results must carry the symbol so the provider can render it"
