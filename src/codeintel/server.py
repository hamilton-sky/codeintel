from __future__ import annotations

import anyio
from mcp.server.mcpserver.server import MCPServer

from codeintel.gateway import Gateway
from codeintel.policy import TieringPolicy
from codeintel.provider import safe_null_result
from codeintel.providers.graph import GraphProvider
from codeintel.providers.lsp import LspProvider
from codeintel.providers.semantic import SemanticProvider
from codeintel.reindexer import Reindexer

_REINDEXER = Reindexer()


def _build_gateway() -> Gateway:
    graph = None
    lsp = None
    try:
        gp = GraphProvider()
        if gp.available:
            graph = gp
    except Exception:
        pass
    try:
        lp = LspProvider()
        if lp.available:
            lsp = lp
    except Exception:
        pass
    semantic = SemanticProvider()
    policy = TieringPolicy(enabled=False)
    return Gateway(graph=graph, lsp=lsp, semantic=semantic, policy=policy, reindexer=_REINDEXER)


def code_query_handler(args: dict) -> dict:
    try:
        op = args.get("op", "")
        target = args.get("target", "")
        project_root = args.get("project_root", "")
        engine = args.get("engine", None)
        role = args.get("role", "")
        gw = _build_gateway()
        return gw.query(op=op, target=target, engine=engine, role=role, project_root=project_root)
    except Exception:
        return safe_null_result("", "", reason="handler-error")


def code_status_handler(args: dict) -> dict:
    try:
        graph_available = False
        lsp_available = False
        semantic_available = False
        try:
            gp = GraphProvider()
            graph_available = bool(gp.available)
        except Exception:
            pass
        try:
            lp = LspProvider()
            lsp_available = bool(lp.available)
        except Exception:
            pass
        try:
            sp = SemanticProvider()
            semantic_available = bool(sp.available)
        except Exception:
            pass
        engines: list[str] = []
        if graph_available:
            engines.append("graph")
        if lsp_available:
            engines.append("lsp")
        if semantic_available:
            engines.append("semantic")
        if not engines:
            engines.append("none")
        return {
            "ok": True,
            "engines": engines,
            "graph": graph_available,
            "lsp": lsp_available,
            "semantic": semantic_available,
            "indexed": False,
            "model": None,
        }
    except Exception:
        return {
            "ok": True,
            "engines": ["none"],
            "graph": False,
            "lsp": False,
            "semantic": False,
            "indexed": False,
            "model": None,
        }


def run() -> None:
    mcp = MCPServer(name="codeintel")

    async def _code_query(
        op: str = "",
        target: str = "",
        project_root: str = "",
        engine: str = "",
        role: str = "",
    ) -> dict:
        return code_query_handler(
            {"op": op, "target": target, "project_root": project_root, "engine": engine, "role": role}
        )

    async def _code_status() -> dict:
        return code_status_handler({})

    mcp.add_tool(_code_query, name="code.query", description="Query the code intelligence engine")
    mcp.add_tool(_code_status, name="code.status", description="Return engine status")

    anyio.run(mcp.run_stdio_async)
