from __future__ import annotations

import threading

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
    # RBAC: when an auth config defines restricted roles, enforce role→op; otherwise the policy is
    # disabled (full access). The per-request role is authenticated server-side by the HTTP layer;
    # the local MCP agent passes role="" (unrestricted), so RBAC never affects the stdio transport.
    try:
        from codeintel.auth import load_auth
        auth = load_auth()
        policy = auth.build_policy() if auth.enabled else TieringPolicy(enabled=False)
    except Exception:
        policy = TieringPolicy(enabled=False)
    return Gateway(graph=graph, lsp=lsp, semantic=semantic, policy=policy, reindexer=_REINDEXER)


_GATEWAY: Gateway | None = None
_GATEWAY_LOCK = threading.Lock()


def _get_gateway() -> Gateway:
    """Build the gateway ONCE and reuse it across requests, so the content-hash cache and
    the async-warming LSP session persist between an agent's calls. Rebuilding it per
    request (the old behavior) left the cache permanently cold and the LSP engine stuck
    re-warming a fresh ``uvx serena`` subprocess on every single call."""
    global _GATEWAY
    if _GATEWAY is None:
        with _GATEWAY_LOCK:
            if _GATEWAY is None:
                _GATEWAY = _build_gateway()
    return _GATEWAY


def _reset_gateway() -> None:
    """Test hook: drop the cached gateway so the next call rebuilds it."""
    global _GATEWAY
    with _GATEWAY_LOCK:
        _GATEWAY = None


def code_query_handler(args: dict) -> dict:
    try:
        op = args.get("op", "")
        target = args.get("target", "")
        project_root = args.get("project_root", "")
        engine = args.get("engine", None)
        role = args.get("role", "")
        gw = _get_gateway()
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

        # Report real freshness/model instead of hardcoded nulls (SPEC §7). When a project_root is
        # supplied, `indexed` is scoped to THAT repo (does it have indexed chunks?) rather than the
        # misleading "any semantic db file exists on this machine".
        project_root = str(args.get("project_root", "") or "")
        indexed = False
        model = None
        try:
            from codeintel.semantic_db import DEFAULT_MODEL, default_db_path
            if semantic_available:
                model = DEFAULT_MODEL
                if project_root:
                    indexed = bool(SemanticProvider().probe(project_root).get("repo_indexed"))
                else:
                    import os
                    indexed = os.path.exists(default_db_path())
        except Exception:
            pass

        return {
            "ok": True,
            "engines": engines,
            "graph": graph_available,
            "lsp": lsp_available,
            "semantic": semantic_available,
            "indexed": indexed,
            "model": model,
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


def code_doctor_handler(args: dict) -> dict:
    try:
        from codeintel import doctor as _doctor

        project_root = str(args.get("project_root", "") or "")
        deep = bool(args.get("deep", False))
        role = str(args.get("role", "") or "")
        # Reuse the singleton gateway's providers so the report reflects the LIVE warmed LSP
        # session state an agent's real queries hit (and the graph project cache).
        gw = _get_gateway()
        # RBAC: doctor is a privileged op (engine state + a deep LSP boot on an arbitrary path), so
        # it's gated behind the "doctor" scope — a restricted role must list it (or use "*").
        if not gw.allows(role, "doctor"):
            return {
                "ok": True, "project_root": project_root, "deep": deep,
                "summary": {"ready": 0, "total": 3, "healthy": False},
                "engines": {}, "reason": "op-not-allowed-for-role",
            }
        return _doctor.run_doctor(
            project_root, deep=deep, graph=gw.graph, lsp=gw.lsp, semantic=gw.semantic
        )
    except Exception:
        return {
            "ok": True, "project_root": "", "deep": False,
            "summary": {"ready": 0, "total": 3, "healthy": False},
            "engines": {}, "note": "doctor-error",
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
        path, wrote = gen.write(project_root, content)
        # size_bytes = bytes actually written; 0 when an existing populated map was preserved
        # (the new content was a stub) — `wrote` disambiguates for the caller.
        size = len(content.encode("utf-8")) if wrote else 0

        inject_result = None
        if inject:
            inj_path, inj_action = Injector().inject(project_root)
            inject_result = {"path": inj_path, "action": inj_action}

        return {"ok": True, "path": path, "size_bytes": size, "wrote": wrote, "inject": inject_result}
    except Exception:
        return {"ok": True, "path": None, "size_bytes": 0, "note": "map-error"}


def run() -> None:
    from codeintel.logconfig import configure_logging
    configure_logging()  # logs to stderr; stdout is the MCP protocol channel
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

    async def _code_status(project_root: str = "") -> dict:
        return code_status_handler({"project_root": project_root})

    async def _code_doctor(project_root: str = "", deep: bool = False) -> dict:
        return code_doctor_handler({"project_root": project_root, "deep": deep})

    async def _code_map(project_root: str = "", budget: int = 32768, inject: bool = False) -> dict:
        return code_map_handler({"project_root": project_root, "budget": budget, "inject": inject})

    mcp.add_tool(_code_query, name="code.query", description="Query the code intelligence engine")
    mcp.add_tool(_code_status, name="code.status", description="Return engine status")
    mcp.add_tool(_code_doctor, name="code.doctor", description="Diagnose engine health + repo index status with remediation")
    mcp.add_tool(_code_map, name="code.map", description="Generate or refresh CODE_INTEL.md orientation file")

    anyio.run(mcp.run_stdio_async)
