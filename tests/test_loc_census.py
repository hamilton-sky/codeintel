"""The `loc()` census: a docstring claim turned into data a test decides.

`loc.py` used to state each backend's line base only in prose. Nothing enforced it, nothing
noticed a fourth provider, and a rule stated only in a docstring is not enforcement (project rule
4). `LINE_BASES` in `loc.py` is the enforced form; this file drives every provider and checks the
number it actually emits against that map.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import numpy as np

import codeintel.providers
from codeintel.graph_backend import BackendClient
from codeintel.loc import LINE_BASES
from codeintel.outcome import Ok
from codeintel.providers.graph import GraphProvider
from codeintel.providers.semantic import SemanticProvider

# Providers that render no path:line at all, recorded as DATA rather than silently excluded — the
# census must still see them and say WHY they are not in LINE_BASES.
_RENDERS_NO_POSITION = {
    "none": "the null provider returns no body and therefore no path:line",
}

TRUE_LINE = 7  # ground truth, 1-based — a human or an editor's own numbering.


def _backend_value(engine: str) -> int:
    """What *engine*'s backend would itself report for TRUE_LINE, given its measured line base."""
    return TRUE_LINE - (1 - LINE_BASES[engine])


# --------------------------------------------------------------------------------------------- #
# T1 — the domain: every provider file that renders a position must be censused.
# --------------------------------------------------------------------------------------------- #

def test_every_provider_that_renders_a_position_is_censused():
    stems = {p.stem for p in pathlib.Path(codeintel.providers.__file__).parent.glob("*.py")}
    stems -= {"__init__"}
    censused = set(LINE_BASES) | set(_RENDERS_NO_POSITION)
    missing = stems - censused
    assert not missing, (
        f"provider {sorted(missing)} renders positions but its line base was never measured — "
        f"probe the backend and add it to LINE_BASES"
    )
    assert stems == censused, f"censused providers no longer exist on disk: {censused - stems}"
    assert set(LINE_BASES).isdisjoint(_RENDERS_NO_POSITION)


# --------------------------------------------------------------------------------------------- #
# T2 — the behavioural core: each engine renders a known position as the same 1-based line.
# --------------------------------------------------------------------------------------------- #

class _FakeTextEmbedding:
    """Deterministic stub: yields 384-dim numpy arrays so no real model is loaded."""

    def __init__(self, model_name=None):
        pass

    def embed(self, texts):
        return [np.full(384, 0.1, dtype=np.float32) for _ in list(texts)]


def _lsp_provider():
    from codeintel.providers.lsp import LspProvider

    p = LspProvider.__new__(LspProvider)  # bypass backend detection
    p._last_backend_error = None
    p._pending_gaps = ()
    return p


def test_each_engine_renders_a_known_position_as_the_same_one_based_line(tmp_path, monkeypatch):
    # ---- semantic ----
    bv_semantic = _backend_value("semantic")
    widget = tmp_path / "widget.py"
    widget.write_text("def make_widget(name):\n    return {'name': name}\n")

    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    match = {
        "path": str(widget), "line": bv_semantic,
        "snippet": "def make_widget(name):", "score": 0.9,
    }
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding), \
         patch("codeintel.searcher.Searcher.search", return_value=[match]):
        semantic_result = SemanticProvider().build_result(
            "search", "widget factory", [], 30000, str(tmp_path)
        )
    semantic_body = semantic_result["result"]
    assert semantic_body is not None
    assert f":{TRUE_LINE}" in semantic_body, (
        f"semantic: expected the 1-based line {TRUE_LINE} in {semantic_body!r}"
    )
    if bv_semantic != TRUE_LINE:
        assert f":{bv_semantic}" not in semantic_body, (
            f"semantic: emitted its own backend value {bv_semantic} unconverted in {semantic_body!r}"
        )

    # ---- lsp ----
    bv_lsp = _backend_value("lsp")
    lsp = _lsp_provider()

    def _call_tool(session, tool, args, timeout_s):
        if tool == "find_symbol":
            return Ok(json.dumps([{
                "kind": "Function", "relative_path": "src/widget.py",
                "name_path": "make_widget",
                "body": "def make_widget(name):\n    return {'name': name}",
                "body_location": {"start_line": bv_lsp, "end_line": bv_lsp + 1},
            }]))
        return Ok(json.dumps({
            "src/widget.py": {"function": [{
                "name_path": "make_widget",
                "content_around_reference": f"  >   {bv_lsp}:    make_widget('x')",
            }]},
        }))

    lsp._call_tool = _call_tool  # type: ignore[method-assign]
    lsp_body = lsp._op_symbol(object(), "make_widget", "/repo", 5.0)
    assert lsp_body is not None
    assert f":{TRUE_LINE}" in lsp_body, f"lsp: expected {TRUE_LINE} in {lsp_body!r}"
    if bv_lsp != TRUE_LINE:
        assert f":{bv_lsp}" not in lsp_body, (
            f"lsp: emitted its own backend value {bv_lsp} unconverted in {lsp_body!r}"
        )

    # ---- graph ----
    bv_graph = _backend_value("graph")
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)  # type: ignore[attr-defined]
    gp._pending_gaps = ()  # type: ignore[attr-defined]

    def _run(method, payload, timeout_ms):
        return {"results": [{
            "node": "make_widget", "file": "src/widget.py", "start_line": bv_graph,
        }]}

    gp._run = _run  # type: ignore[method-assign]
    graph_body = gp._op_pattern("make_widget", "proj", 30000)
    assert graph_body is not None
    assert f":{TRUE_LINE}" in graph_body, f"graph: expected {TRUE_LINE} in {graph_body!r}"
    if bv_graph != TRUE_LINE:
        assert f":{bv_graph}" not in graph_body, (
            f"graph: emitted its own backend value {bv_graph} unconverted in {graph_body!r}"
        )


# --------------------------------------------------------------------------------------------- #
# T3 — no engine ever emits a position that does not exist.
# --------------------------------------------------------------------------------------------- #

def test_no_engine_ever_emits_a_position_that_does_not_exist(tmp_path, monkeypatch):
    # ---- semantic: degenerate 0-based value 0 ----
    widget = tmp_path / "widget.py"
    widget.write_text("def make_widget(name):\n    return {'name': name}\n")
    monkeypatch.setattr("codeintel.semantic_db._base_dir", lambda: tmp_path)
    monkeypatch.setattr("codeintel.providers.semantic._DEPS_OK", True)
    match = {"path": str(widget), "line": 0, "snippet": "def make_widget(name):", "score": 0.9}
    with patch("fastembed.TextEmbedding", _FakeTextEmbedding), \
         patch("codeintel.searcher.Searcher.search", return_value=[match]):
        semantic_result = SemanticProvider().build_result(
            "search", "widget factory", [], 30000, str(tmp_path)
        )
    semantic_body = semantic_result["result"]
    assert semantic_body is not None
    assert ":0" not in semantic_body
    assert ":-1" not in semantic_body
    assert ":1" in semantic_body

    # ---- lsp: degenerate 0-based value 0 ----
    lsp = _lsp_provider()

    def _call_tool(session, tool, args, timeout_s):
        if tool == "find_symbol":
            return Ok(json.dumps([{
                "kind": "Function", "relative_path": "src/widget.py",
                "name_path": "make_widget",
                "body": "def make_widget(name):\n    return {'name': name}",
                "body_location": {"start_line": 0, "end_line": 1},
            }]))
        return Ok(json.dumps({
            "src/widget.py": {"function": [{
                "name_path": "make_widget",
                "content_around_reference": "  >   0:    make_widget('x')",
            }]},
        }))

    lsp._call_tool = _call_tool  # type: ignore[method-assign]
    lsp_body = lsp._op_symbol(object(), "make_widget", "/repo", 5.0)
    assert lsp_body is not None
    assert ":0" not in lsp_body
    assert ":-1" not in lsp_body
    assert ":1" in lsp_body

    # ---- graph: malformed for a 1-based backend — 0 AND -1 ----
    for degenerate in (0, -1):
        gp = GraphProvider.__new__(GraphProvider)
        gp._backend = BackendClient.__new__(BackendClient)  # type: ignore[attr-defined]
        gp._pending_gaps = ()  # type: ignore[attr-defined]

        def _run(method, payload, timeout_ms, _line=degenerate):
            return {"results": [{
                "node": "make_widget", "file": "src/widget.py", "start_line": _line,
            }]}

        gp._run = _run  # type: ignore[method-assign]
        graph_body = gp._op_pattern("make_widget", "proj", 30000)
        assert graph_body is not None
        assert ":0" not in graph_body, f"graph rendered path:0 for start_line={degenerate}"
        assert ":-1" not in graph_body, f"graph rendered path:-1 for start_line={degenerate}"
