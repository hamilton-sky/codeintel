"""D3: one `changed` answer was too big for an MCP client.

A branch-wide `changed` came back at 113,352 bytes: the `result` markdown (35,070 chars) and a
`rows` list repeating the same findings (57,403 chars, 98 rows). Claude Code's MCP client refused it
as over its token limit and spilled it to a file, so the agent never read the answer at all.

Over MCP the envelope now stays under `MCP_ENVELOPE_BUDGET_BYTES`: `rows` are kept while they fit
and the rest are counted with a pointer to the CLI, which still prints every row with `--json`.
The fixture is a real `changed` answer from a throwaway repository, not a hand-built dict.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import codeintel.server as srv
from tests.test_changed_range import _ask, _branch, _edge, _Graph, _provider
from tests.test_mcp_server import _registered_tools

_SYMBOLS, _PER_SYMBOL = 12, 10
_CALLERS = _SYMBOLS * _PER_SYMBOL


@pytest.fixture
def big_answer(monkeypatch, tmp_path) -> dict:
    """Twelve functions re-signed, each with ten callers in files the diff did not touch: a
    `changed` answer the size of the one that was refused."""
    names = [f"build_plan_step_{i:02d}" for i in range(_SYMBOLS)]
    base = "".join(f"def {n}(a):\n    return a\n\n\n" for n in names)
    repo = _branch(tmp_path, "wide", {"m/__init__.py": "", "m/core.py": base},
                   {"m/core.py": base.replace("(a):", "(a, b):")})
    graph = _Graph()
    for n in names:
        graph.edges[n] = [
            _edge(n, "m/core.py", f"services.area_{i:02d}.handlers.request_handler_for_{n}",
                  f"services/area_{i:02d}/handlers.py")
            for i in range(_PER_SYMBOL)]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert len(env["rows"]) == _CALLERS, "the fixture must carry every caller as a row"
    assert len(json.dumps(env)) > srv.MCP_ENVELOPE_BUDGET_BYTES, "the fixture must be over budget"
    return env


def _over_mcp(monkeypatch, env: dict) -> dict:
    monkeypatch.setattr(srv, "code_query_handler", lambda args: dict(env))
    tool = _registered_tools(monkeypatch)["code.query"]
    return asyncio.run(tool(op="changed", target="main", project_root="/repo"))


def test_a_changed_answer_over_mcp_stays_under_the_byte_budget(monkeypatch, big_answer):
    out = _over_mcp(monkeypatch, big_answer)
    size = len(json.dumps(out).encode("utf-8"))
    assert size <= srv.MCP_ENVELOPE_BUDGET_BYTES, size
    assert out["result"] == big_answer["result"], "the answer text itself is never cut"


def test_rows_left_out_over_mcp_are_counted_and_say_where_the_rest_are(monkeypatch, big_answer):
    out = _over_mcp(monkeypatch, big_answer)
    kept = len(out.get("rows", []))
    assert out["rows_total"] == _CALLERS
    assert out["rows_omitted"] == _CALLERS - kept > 0
    assert out["rows"] == big_answer["rows"][:kept], "the rows kept are the first ones, in order"
    note = out["rows_note"]
    assert "codeintel query --op changed --target main --json" in note, note
    assert "`result` is not cut" in note, "it says the answer text itself is whole"


def test_an_answer_that_fits_is_passed_through_unchanged(monkeypatch, tmp_path):
    repo = _branch(tmp_path, "narrow", {"m/core.py": "def f(a):\n    return a\n"},
                   {"m/core.py": "def f(a, b):\n    return a\n"})
    graph = _Graph()
    graph.edges["f"] = [_edge("f", "m/core.py", "m.use.go", "m/use.py")]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert _over_mcp(monkeypatch, env) == env


def test_the_cli_json_still_prints_every_row(monkeypatch, capsys, big_answer):
    from codeintel.commands import query

    monkeypatch.setattr(query, "_query", lambda args: dict(big_answer))
    args = SimpleNamespace(op="changed", target="main", engine="auto", project_root=None, json=True)
    assert query.run(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert len(printed["rows"]) == _CALLERS and "rows_omitted" not in printed


def test_compacting_never_raises_and_falls_back_to_the_envelope_as_it_was():
    loop: dict = {}
    loop["self"] = loop                                  # json.dumps raises ValueError on a cycle
    env = {"ok": True, "result": "x", "rows": [loop]}
    assert srv.compact_for_mcp(env, "changed", "main") is env
