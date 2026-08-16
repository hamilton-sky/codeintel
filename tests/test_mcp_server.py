"""MCP server advertisement (0.8.4).

`codeintel install` only makes the tools *available*; for an agent to actually reach for them
instead of grep/file-read, the server must advertise itself — the standard MCP `instructions`
field plus tool descriptions that say when to use each. These assert that wiring.
"""
from __future__ import annotations

import codeintel.server as srv


def test_server_advertises_instructions_and_rich_tool_descriptions(monkeypatch):
    captured: dict = {"kwargs": None, "tools": {}}

    class _FakeMCP:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def add_tool(self, fn, name=None, description=None):
            captured["tools"][name] = description or ""

        async def run_stdio_async(self):  # pragma: no cover - never awaited (anyio.run stubbed)
            pass

    monkeypatch.setattr(srv, "MCPServer", _FakeMCP)
    monkeypatch.setattr(srv.anyio, "run", lambda *a, **k: None)  # don't block on stdio

    srv.run()

    kw = captured["kwargs"]
    assert kw["name"] == "codeintel"
    assert kw.get("version")  # server reports its version
    instr = kw.get("instructions") or ""
    assert "code.query" in instr
    assert "grep" in instr.lower()          # explicitly steers the agent away from grep/file-read
    assert "reason" in instr                # explains the never-raise safe-null envelope

    tools = captured["tools"]
    assert set(tools) == {"code.query", "code.status", "code.doctor", "code.map"}
    # the primary tool sells when to use it, not the old throwaway one-liner
    q = tools["code.query"]
    assert "callers" in q and "grep" in q.lower() and len(q) > 100


def _registered_tools(monkeypatch) -> dict:
    """Run the real `srv.run()` wiring against a fake MCP server and return {name: function}."""
    tools: dict = {}

    class _FakeMCP:
        def __init__(self, **kwargs):
            pass

        def add_tool(self, fn, name=None, description=None):
            tools[name] = fn

        async def run_stdio_async(self):  # pragma: no cover - never awaited (anyio.run stubbed)
            pass

    monkeypatch.setattr(srv, "MCPServer", _FakeMCP)
    monkeypatch.setattr(srv.anyio, "run", lambda *a, **k: None)
    srv.run()
    return tools


def test_no_tool_advertises_the_optional_envelope_fields_as_required(monkeypatch):
    """A regression guard with a sharp edge: MCP derives each tool's OUTPUT schema from its return
    annotation, and it renders a TypedDict's `NotRequired` keys as REQUIRED. `reason` and `hint`
    are present only on a safe-null envelope, so annotating a tool `-> Result` makes the schema
    demand two fields every SUCCESSFUL result omits — and the agent gets `isError: true` for every
    working query. Annotate the MCP-facing coroutines `-> dict`; keep `Result` on the inner
    handlers, which is where it buys type safety without reaching the wire.
    """
    from mcp.server.mcpserver.tools import Tool

    for name, fn in _registered_tools(monkeypatch).items():
        schema = Tool.from_function(fn, name=name).output_schema or {}
        leaked = set(schema.get("required") or ()) & {"reason", "hint"}
        assert not leaked, f"{name} requires {sorted(leaked)}, which a successful result omits"
