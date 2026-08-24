"""codeintel skips files with a raw NUL byte (git's binary rule) and now says WHY.

A raw NUL in a source file is almost always a deliberate separator (`.join('\\0')`, a composite key)
saved as a byte instead of an escape — so the skip warning names the line and the fix rather than
just calling the file "binary"."""
from __future__ import annotations

import logging

from codeintel.indexer import Indexer, _looks_binary, _nul_byte_line
from codeintel.semantic_db import SemanticDb


def test_nul_byte_line_points_at_the_first_nul(tmp_path):
    raw = tmp_path / "raw.ts"
    raw.write_bytes(b"import x\nconst k = a.join('\x00')\nexport {}\n")   # raw NUL on line 2
    assert _nul_byte_line(raw) == 2
    assert _looks_binary(raw) is True

    escaped = tmp_path / "escaped.ts"
    escaped.write_text("const k = a.join('\\0')\n")                        # the escape, clean text
    assert _nul_byte_line(escaped) is None
    assert _looks_binary(escaped) is False


def test_walk_skips_a_nul_byte_source_file_with_an_actionable_warning(tmp_path, caplog):
    (tmp_path / "good.py").write_text("def f():\n    return 1\n")
    (tmp_path / "bad.py").write_bytes(b"KEY = 'a\x00b'\n")                 # raw NUL → reads as binary

    db = SemanticDb(str(tmp_path / "idx.sqlite"))
    db.init()
    try:
        with caplog.at_level(logging.WARNING):
            walked = {p.name for p in Indexer(db)._walk_files(tmp_path)}
    finally:
        db.close()

    assert "good.py" in walked
    assert "bad.py" not in walked                                          # skipped as binary
    msg = " ".join(r.getMessage() for r in caplog.records)
    # The message must be actionable: it names the file, says WHAT (a null byte), and the FIX (\0).
    assert "bad.py" in msg and "null byte" in msg and "\\0" in msg
