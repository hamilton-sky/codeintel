"""Story: an agent following hints.

Hints name only tools and commands the agent really has. Run against the real codebase-memory-mcp
(0.9.x, installed in the repo venv) where the backend's own wording is the subject — its
"function not found" hint and its `type(c) AS kind` reply — and skipped when it is absent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from _fakegraph import Repo

from codeintel.engines.graph.provider import GraphProvider

_BIN = os.path.dirname(sys.executable)
_FOREIGN_TOOLS = ("index_repository", "search_graph", "trace_path")


def _backend() -> str | None:
    return shutil.which("codebase-memory-mcp", path=_BIN + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A small repository indexed by the real backend, and a provider that talks to it."""
    backend = _backend()
    if backend is None:
        pytest.skip("codebase-memory-mcp is not installed")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEBASE_MEMORY_HOME", raising=False)
    monkeypatch.delenv("CODEBASE_MEMORY_CACHE_DIR", raising=False)
    monkeypatch.setenv("PATH", _BIN + os.pathsep + "/usr/bin:/bin")
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "server.py").write_text(
        "def compact_for_mcp(env):\n    return env\n\n\n"
        "def code_query_handler(args):\n    return compact_for_mcp(args)\n")
    (root / "pkg" / "b.py").write_text(
        "def helper2():\n    return 2\n\n\ndef other():\n    return helper2()\n\n\n"
        "def leaf():\n    return 1\n")
    subprocess.run([backend, "cli", "index_repository"],
                   input=json.dumps({"repo_path": str(root)}).encode(),
                   capture_output=True, check=True, timeout=120)
    return str(root), GraphProvider()


def _said(env: dict) -> str:
    return f"{env.get('hint') or ''}\n{env.get('result') or ''}"


# Spec: No hint names `index_repository`, `search_graph` or `trace_path`. The not-in-graph hint
# names `codeintel index <root>` (or "ask the user to run it") and lists up to 5 close names, e.g.
# `compact_for_mcpp` -> `compact_for_mcp`.
def test_not_in_graph_hint_names_codeintel_index_and_close_names_not_backend_tools(live):
    root, p = live
    env = p.build_result("callers", "compact_for_mcpp", [], 30000, root)
    assert env.get("reason") == "not-in-graph", env
    hint = str(env.get("hint") or "")
    for tool in _FOREIGN_TOOLS:
        assert tool not in hint, hint
    assert "codeintel index" in hint or "ask the user to run it" in hint, hint
    assert "compact_for_mcp" in hint.replace("compact_for_mcpp", ""), hint


# Spec: No hint names `index_repository`, `search_graph` or `trace_path`.
# (The backend's own "function not found" hint for `chain` says "Use search_graph(name_pattern=...)
# ... then pass it to trace_path".)
def test_chain_on_an_unknown_name_never_passes_on_the_backends_tool_names(live):
    root, p = live
    env = p.build_result("chain", "other to helper2", [], 30000, root)
    said = json.dumps(env)
    for tool in _FOREIGN_TOOLS:
        assert tool not in said, said


# Spec: `--engine graph --op symbol` gives a hint saying `symbol` is answered by engine=lsp and to
# use op=context or op=pattern when LSP is down. It never says "did you mean symbol" and never lists
# `changes`.
def test_graph_engine_symbol_hint_points_at_lsp_and_context_or_pattern(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch)
    repo.graph.node("pkg.gateway.Gateway", "pkg/gateway.py", "Class")
    repo.fresh_index()
    env = repo.ask("symbol", "Gateway")
    hint = str(env.get("hint") or "")
    assert "lsp" in hint, hint
    assert "context" in hint and "pattern" in hint, hint
    assert "did you mean symbol" not in hint, hint
    assert not re.search(r"\bchanges\b", hint), hint


def _no_edges(p: GraphProvider, op: str, root: str) -> dict:
    env = p.build_result(op, "leaf", [], 30000, root)
    assert env.get("reason") == "no-edges", env
    return env


# Spec: The no-edges hint never prints a non-edge kind such as "1 1" or the structural kinds
# `DEFINES`/`CONTAINS`.
def test_no_edges_hint_never_prints_a_non_edge_or_structural_kind(live):
    root, p = live
    for op in ("callers", "callees"):
        hint = str(_no_edges(p, op, root).get("hint") or "")
        assert "1 1" not in hint, hint
        assert "DEFINES" not in hint and "CONTAINS" not in hint, hint


# Spec: For callees and impact it talks about outgoing calls and does not mention dead code.
def test_no_edges_hint_for_callees_and_impact_is_about_outgoing_calls_not_dead_code(live):
    root, p = live
    for op in ("callees", "impact"):
        env = p.build_result(op, "leaf", [], 30000, root)
        hint = str(env.get("hint") or "")
        if env.get("reason") != "no-edges":
            continue                      # impact may answer the callers half instead
        assert "outgoing" in hint.lower(), (op, hint)
        assert "dead code" not in hint.lower(), (op, hint)
    callees = _no_edges(p, "callees", root)
    assert "outgoing" in str(callees.get("hint") or "").lower()
