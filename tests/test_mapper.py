from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

import pytest

from codeintel.mapper import MapGenerator
from codeintel.injector import Injector


def _make_provider(ranked=None, entry=None, arch=None):
    """Return a mocked GraphProvider with configurable return values."""
    provider = MagicMock()
    provider.available = True
    provider._resolve_project.return_value = "test-project"

    # build_result for "overview"
    if arch is None:
        provider.build_result.return_value = {
            "ok": True,
            "op": "overview",
            "target": "",
            "result": "## Modules\n- src/codeintel/mapper.py",
            "engine": "graph",
            "cached": False,
        }
    else:
        provider.build_result.return_value = arch

    # _run for query_graph calls — returns ranked then entry
    ranked_rows = ranked if ranked is not None else []
    entry_rows = entry if entry is not None else []
    provider._run.side_effect = [ranked_rows, entry_rows]
    return provider


# ---------------------------------------------------------------------------
# MapGenerator tests
# ---------------------------------------------------------------------------

def test_generate_with_empty_graph():
    provider = _make_provider(ranked=[], entry=[])
    gen = MapGenerator(provider)
    result = gen.generate("/tmp/repo")
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_with_no_provider():
    gen = MapGenerator(None)
    result = gen.generate("/tmp/repo")
    assert isinstance(result, str)
    assert "not available" in result.lower() or "not" in result.lower()


def test_generate_byte_budget_enforced():
    ranked = [
        {"fn.name": f"func_{i}", "fn.file_path": f"src/module_{i}.py", "in_degree": 30 - i}
        for i in range(30)
    ]
    provider = _make_provider(ranked=ranked, entry=[])
    gen = MapGenerator(provider)
    budget = 500
    result = gen.generate("/tmp/repo", budget_bytes=budget)
    assert len(result.encode("utf-8")) <= budget + len("> Content truncated to fit".encode())
    assert "truncated" in result.lower()


def test_generate_deterministic():
    ranked = [
        {"fn.name": "alpha", "fn.file_path": "src/alpha.py", "in_degree": 10},
        {"fn.name": "beta", "fn.file_path": "src/beta.py", "in_degree": 5},
    ]

    def make_gen():
        p = _make_provider(ranked=ranked, entry=[])
        return MapGenerator(p)

    result1 = make_gen().generate("/tmp/repo")
    result2 = make_gen().generate("/tmp/repo")
    assert result1 == result2


def test_generate_reads_columns_rows_shape():
    """The real backend returns {columns, rows}; the old mapper only accepted a list and so
    always rendered an empty table. This locks the real shape + the builtin/project noise filter."""
    ranked = {
        "columns": ["fn.name", "fn.qualified_name", "fn.file_path", "in_degree"],
        "rows": [
            ["query", "codeintel.gateway.Gateway.query", "src/codeintel/gateway.py", "29"],
            ["str", "builtins.str", "<python-builtins>", "27"],       # noise → dropped
            ["codeintel", "codeintel.pyproject", "pyproject.toml", "30"],  # noise → dropped
            ["Result", "codeintel.provider.Result", "src/codeintel/provider.py", "22"],
        ],
    }
    entry = {"columns": ["fn.name", "fn.file_path"], "rows": [["main", "src/codeintel/__main__.py"]]}
    provider = MagicMock()
    provider.available = True
    provider._resolve_project.return_value = "proj"
    provider.build_result.return_value = {
        "ok": True, "op": "overview", "target": "", "result": "## Arch",
        "engine": "graph", "cached": False,
    }
    provider._run.side_effect = [ranked, entry]

    out = MapGenerator(provider).generate("/repo")
    assert "## Ranked Symbols (by caller count)" in out
    assert "`query`" in out and "29" in out
    assert "`Result`" in out
    assert "<python-builtins>" not in out and "`str`" not in out  # noise filtered
    assert "pyproject.toml" not in out
    assert "## Entry Points" in out and "`main`" in out


def test_write_creates_file():
    gen = MapGenerator(None)
    with tempfile.TemporaryDirectory() as d:
        path = gen.write(d, "# content")
        expected = os.path.join(d, "CODE_INTEL.md")
        assert path == expected
        assert os.path.exists(expected)
        assert open(expected).read() == "# content"


# ---------------------------------------------------------------------------
# Injector tests
# ---------------------------------------------------------------------------

def test_inject_appends_block():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        claude = os.path.join(d, "CLAUDE.md")
        open(claude, "w").write("# Project\n")
        path, action = inj.inject(d)
        assert action == "appended"
        assert path == claude
        content = open(claude).read()
        assert "<!-- codeintel-map-start -->" in content
        assert "<!-- codeintel-map-end -->" in content


def test_inject_is_idempotent():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        claude = os.path.join(d, "CLAUDE.md")
        open(claude, "w").write("# Project\n")
        inj.inject(d)
        inj.inject(d)
        content = open(claude).read()
        assert content.count("<!-- codeintel-map-start -->") == 1
        assert content.count("<!-- codeintel-map-end -->") == 1


def test_inject_no_context_file():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        result = inj.inject(d)
        assert result == (None, "no-context-file")


def test_inject_updates_existing_block():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        claude = os.path.join(d, "CLAUDE.md")
        initial = (
            "# Project\n\n"
            "<!-- codeintel-map-start -->\nold content\n<!-- codeintel-map-end -->\n"
        )
        open(claude, "w").write(initial)
        path, action = inj.inject(d)
        assert action == "updated"
        assert path == claude
        content = open(claude).read()
        assert content.count("<!-- codeintel-map-start -->") == 1
        assert content.count("<!-- codeintel-map-end -->") == 1
        assert "old content" not in content
