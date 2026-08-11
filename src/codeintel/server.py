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


def code_map_handler(args: dict) -> dict:
    try:
        from codeintel.mapper import MapGenerator
        from codeintel.injector import Injector

        project_root = str(args.get("project_root", "") or "")
        budget = int(args.get("budget", 32768) or 32768)
        inject = bool(args.get("inject", False))

        try:
            provider = GraphProvider()
        except Exception:
            provider = None

        gen = MapGenerator(provider)
        content = gen.generate(project_root, budget_bytes=budget)
        path = gen.write(project_root, content)
        size = len(content.encode("utf-8"))

        inject_result = None
        if inject:
            inj_path, inj_action = Injector().inject(project_root)
            inject_result = {"path": inj_path, "action": inj_action}

        return {"ok": True, "path": path, "size_bytes": size, "inject": inject_result}
    except Exception:
        return {"ok": True, "path": None, "size_bytes": 0, "note": "map-error"}


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

    async def _code_map(project_root: str = "", budget: int = 32768, inject: bool = False) -> dict:
        return code_map_handler({"project_root": project_root, "budget": budget, "inject": inject})

    mcp.add_tool(_code_query, name="code.query", description="Query the code intelligence engine")
    mcp.add_tool(_code_status, name="code.status", description="Return engine status")
    mcp.add_tool(_code_map, name="code.map", description="Generate or refresh CODE_INTEL.md orientation file")

    anyio.run(mcp.run_stdio_async)
