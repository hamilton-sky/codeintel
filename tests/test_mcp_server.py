"""MCP server advertisement (0.8.4).

`codeintel install` only makes the tools *available*; for an agent to actually reach for them
instead of grep/file-read, the server must advertise itself — the standard MCP `instructions`
field plus tool descriptions that say when to use each. These assert that wiring.
"""
from __future__ import annotations

import codeintel.server as srv


def test_server_advertises_instructions_and_rich_tool_descriptions(monkeypatch):
    captured: dict = {"kwargs": None, "tools": {}, "annotations": {}}

    class _FakeMCP:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        # `annotations` is a new call-time kwarg (0.19.0): `run()` now passes a `ToolAnnotations`
        # to every `add_tool` call (see test_tools_declare_whether_they_write below), which a fake
        # missing the parameter would reject with a TypeError before this test gets to assert
        # anything.
        def add_tool(self, fn, name=None, description=None, annotations=None):
            captured["tools"][name] = description or ""
            captured["annotations"][name] = annotations

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


def test_tools_declare_whether_they_write(monkeypatch):
    """An agent must be able to tell, before calling, that a tool writes to the user's files —
    `code.map` writes CODE_INTEL.md unconditionally (and, with `inject=True`, the user's own
    CLAUDE.md/AGENTS.md) while being advertised as a great first call on an unfamiliar repo. MCP's
    own `ToolAnnotations.read_only_hint` is the standard place to say so."""
    annotations = _registered_annotations(monkeypatch)

    assert annotations["code.query"].read_only_hint is True
    assert annotations["code.status"].read_only_hint is True
    assert annotations["code.doctor"].read_only_hint is True
    assert annotations["code.map"].read_only_hint is False


def _registered_annotations(monkeypatch) -> dict:
    """Run the real `srv.run()` wiring against a fake MCP server and return {name: ToolAnnotations}."""
    captured: dict = {}

    class _FakeMCP:
        def __init__(self, **kwargs):
            pass

        def add_tool(self, fn, name=None, description=None, annotations=None):
            captured[name] = annotations

        async def run_stdio_async(self):  # pragma: no cover - never awaited (anyio.run stubbed)
            pass

    monkeypatch.setattr(srv, "MCPServer", _FakeMCP)
    monkeypatch.setattr(srv.anyio, "run", lambda *a, **k: None)
    srv.run()
    return captured


def _registered_tools(monkeypatch) -> dict:
    """Run the real `srv.run()` wiring against a fake MCP server and return {name: function}."""
    tools: dict = {}

    class _FakeMCP:
        def __init__(self, **kwargs):
            pass

        def add_tool(self, fn, name=None, description=None, annotations=None):
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


def _query_input_schema(monkeypatch) -> dict:
    from mcp.server.mcpserver.tools import Tool

    tools = _registered_tools(monkeypatch)
    return Tool.from_function(tools["code.query"], name="code.query").parameters


def test_code_query_op_is_a_schema_enum_not_bare_prose(monkeypatch):
    """The highest-leverage fix: `op` as a JSON-schema enum, so a mistyped/hallucinated op is
    rejected by MCP's own argument validation with the real choices listed, instead of round-
    tripping to graph.py's `unsupported-op` — easily misread as 'found nothing'. Prose read once
    at connect time is the wrong place to document a field filled in later."""
    schema = _query_input_schema(monkeypatch)
    op_schema = schema["properties"]["op"]

    assert set(op_schema["enum"]) == {
        "search", "symbol", "callers", "callees", "impact", "chain",
        "pattern", "overview", "context", "changed", "hotspots",
    }
    # required — there is no sensible default "operation to run"
    assert "op" in (schema.get("required") or ())


def test_query_ops_module_matches_the_query_op_literal():
    """`codeintel.query_ops.QUERY_OPS` is a dependency-free copy of this same op vocabulary, split
    out because `server.py` pulls in `mcp`/`anyio`/`pydantic` (~4.4s to import) — too heavy for
    `codeintel --help` or the CLI's `--op` parser to pay for just to know the op names
    (`test_importing_the_cli_does_not_pull_in_any_engine`). Nothing else keeps the two definitions
    in sync, which is exactly the class of drift this whole pass exists to catch — this is that
    guard for `_QueryOp` itself.

    Compared as SETS, not sequences: `QUERY_OPS`'s declaration order drives the CLI's own `--op`
    choices/help presentation (see test_cli_help.py's op tests, which sort or set-compare rather
    than pin an order), and `_QueryOp`'s order drives the JSON-schema `enum` this tool exposes to
    the model — each order is meaningful only to its own surface, and neither is tested against
    the other's, so cross-module order is not load-bearing. Only membership is."""
    from typing import get_args

    from codeintel.query_ops import QUERY_OPS
    from codeintel.server import _QueryOp

    literal_ops = set(get_args(_QueryOp))
    tuple_ops = set(QUERY_OPS)
    assert literal_ops == tuple_ops, (
        "codeintel.server._QueryOp and codeintel.query_ops.QUERY_OPS have drifted apart — "
        f"only in server._QueryOp: {sorted(literal_ops - tuple_ops)}; "
        f"only in query_ops.QUERY_OPS: {sorted(tuple_ops - literal_ops)}. "
        "Update whichever one is missing the other's ops."
    )


def test_code_query_schema_documents_root_scoped_ops_ignore_target(monkeypatch):
    """`overview`/`changed`/`hotspots` ignore `target` entirely (`_ROOT_SCOPED_OPS`,
    providers/graph.py) — that must be visible to the model filling the field, not buried in
    prose it read once at connect time."""
    schema = _query_input_schema(monkeypatch)

    op_desc = schema["properties"]["op"]["description"]
    target_desc = schema["properties"]["target"]["description"]
    for op in ("overview", "changed", "hotspots"):
        assert op in op_desc
    assert "ignored" in target_desc.lower()


def test_code_query_schema_documents_project_root_fallback_and_rbac_caveat(monkeypatch):
    """`project_root` is genuinely optional now (server.py falls back to the server's cwd — see
    `code_query_handler`), but the schema must say so AND flag the one case where a caller must
    not rely on the fallback: a restricted RBAC role, where a blank value is rejected rather than
    defaulted (policy.py's `is_root_allowed`)."""
    schema = _query_input_schema(monkeypatch)
    desc = schema["properties"]["project_root"]["description"].lower()

    assert "optional" in desc
    assert "cwd" in desc or "current working directory" in desc
    assert "role" in desc  # the RBAC caveat


def test_code_map_schema_documents_inject_and_budget(monkeypatch):
    """`inject` writes to the user's CLAUDE.md/AGENTS.md and `budget`'s units were never stated —
    both must be visible in the schema, not just guessable from the parameter name."""
    from mcp.server.mcpserver.tools import Tool

    tools = _registered_tools(monkeypatch)
    schema = Tool.from_function(tools["code.map"], name="code.map").parameters

    inject_desc = schema["properties"]["inject"]["description"].lower()
    budget_desc = schema["properties"]["budget"]["description"].lower()
    assert "claude.md" in inject_desc or "agents.md" in inject_desc
    assert "bytes" in budget_desc


def test_code_doctor_schema_documents_deep(monkeypatch):
    """`deep` boots a live serena session and is slow — undocumented before this fix."""
    from mcp.server.mcpserver.tools import Tool

    tools = _registered_tools(monkeypatch)
    schema = Tool.from_function(tools["code.doctor"], name="code.doctor").parameters

    deep_desc = schema["properties"]["deep"]["description"].lower()
    assert "slow" in deep_desc or "seconds" in deep_desc
