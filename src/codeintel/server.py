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


_STATUS_FALLBACK: dict = {
    "ok": True,
    "engines": ["none"],
    "graph": False,
    "lsp": False,
    "semantic": False,
    "indexed": False,
    "model": None,
    "healthy": False,
    "readiness": {},
}


def code_status_handler(args: dict) -> dict:
    """Engine readiness for an agent.

    Reports the SAME tri-state doctor computes — installed / runnable / repo_indexed — against
    the SAME live providers a real query hits (the singleton gateway's, including the warmed
    LSP session). It previously constructed throwaway providers and reported a single flat
    boolean per engine, so `lsp: true` could mean "uvx is on PATH" for an engine that in fact
    never boots — an agent reading that would reason from a readiness claim nothing verified.

    The flat `graph`/`lsp`/`semantic`/`indexed`/`model` keys keep their original meaning for
    existing callers; `readiness` and `healthy` carry the full picture."""
    try:
        from codeintel import doctor as _doctor

        project_root = str(args.get("project_root", "") or "")
        gw = _get_gateway()
        report = _doctor.run_doctor(
            project_root, deep=False, graph=gw.graph, lsp=gw.lsp, semantic=gw.semantic
        )
        probes = report.get("engines", {}) if isinstance(report, dict) else {}

        def _probe(name: str) -> dict:
            p = probes.get(name)
            return p if isinstance(p, dict) else {}

        readiness = {
            name: {
                "installed": _probe(name).get("installed"),
                "runnable": _probe(name).get("runnable"),
                "repo_indexed": _probe(name).get("repo_indexed"),
                "status": _probe(name).get("status", "fail"),
                "detail": _probe(name).get("detail", ""),
                "remediation": _probe(name).get("remediation"),
            }
            for name in ("graph", "lsp", "semantic")
        }

        graph_available = readiness["graph"]["installed"] is True
        lsp_available = readiness["lsp"]["installed"] is True
        semantic_available = readiness["semantic"]["installed"] is True

        engines = [n for n in ("graph", "lsp", "semantic") if readiness[n]["installed"] is True]
        if not engines:
            engines = ["none"]

        # `indexed` stays semantic-scoped and back-compatible: for a given repo it means "this
        # repo has chunks"; with no project_root it falls back to "any index exists on this box".
        indexed = False
        model = None
        try:
            from codeintel.semantic_db import DEFAULT_MODEL, default_db_path
            if semantic_available:
                model = DEFAULT_MODEL
                if project_root:
                    try:  # report THIS repo's configured model, not the machine default
                        from codeintel.config import load_config
                        model = str(load_config(project_root).get("model") or DEFAULT_MODEL)
                    except Exception:
                        pass
                    indexed = readiness["semantic"]["repo_indexed"] is True
                else:
                    import glob
                    import os
                    # any per-model cache file (semantic.db / semantic-<hash>.db) counts as "indexed"
                    base = os.path.dirname(default_db_path())
                    indexed = bool(glob.glob(os.path.join(base, "semantic*.db")))
        except Exception:
            pass

        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        return {
            "ok": True,
            "engines": engines,
            "graph": graph_available,
            "lsp": lsp_available,
            "semantic": semantic_available,
            "indexed": indexed,
            "model": model,
            "healthy": bool(summary.get("healthy")),
            "readiness": readiness,
            "versions": report.get("versions", {}) if isinstance(report, dict) else {},
        }
    except Exception:
        return dict(_STATUS_FALLBACK)


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


# Surfaced by the MCP client to the model on connect (the standard `instructions` field): this is
# how codeintel becomes the *default* way an agent understands code after `codeintel install`,
# rather than just an available tool the agent may ignore in favor of grep/file-read.
_MCP_INSTRUCTIONS = (
    "codeintel unifies three code-intelligence engines — graph (call/import structure), LSP "
    "(precise symbols/references), and semantic (embedding search) — behind one never-raise "
    "`code.query` tool.\n\n"
    "Prefer `code.query` as your FIRST step for understanding an unfamiliar or large codebase, "
    "instead of grepping or reading files one by one: use it for who-calls-X (callers), what-X-calls "
    "(callees), impact of a change, where-a-symbol-is-defined, how a call chain flows, and "
    "natural-language 'find the code that does Y' search. It is graph-augmented and ranked, so it "
    "beats raw grep for locating and relating code.\n\n"
    "Before you edit, run `changed` to see which symbols your uncommitted edits ripple into; reach "
    "for `hotspots` (complexity/fan-in risk) and `deadcode` when planning a refactor.\n\n"
    "Orient on a new repo with `code.map` (ranked architecture: top symbols, entry points, routes). "
    "If results look empty, call `code.doctor` — it says exactly what to index or install. "
    "`code.status` reports engine health.\n\n"
    "Every result is a safe envelope: `ok` is always true; a null `result` with a `reason` means "
    "'nothing found / not indexed yet', NOT an error — read the `reason`/`hint` and, if it says the "
    "repo isn't indexed, that resolves on the first query or via `codeintel index`."
)


def run() -> None:
    from codeintel import __version__
    from codeintel.logconfig import configure_logging
    configure_logging()  # logs to stderr; stdout is the MCP protocol channel
    mcp = MCPServer(name="codeintel", version=__version__, instructions=_MCP_INSTRUCTIONS)

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

    mcp.add_tool(_code_query, name="code.query", description=(
        "Understand code across graph + LSP + semantic engines — prefer this over grep/file-read "
        "for locating and relating code. ops: `search` (natural-language or symbol semantic search), "
        "`symbol` (definition/signature), `callers`, `callees`, `impact`, `chain` (\"A->B\" call "
        "path, risk-labeled), `pattern` (graph-augmented grep), `overview` (architecture), "
        "`context` (fan-out), `changed` (impact of your uncommitted git edits — no target needed), "
        "`deadcode` (unreferenced non-test symbols), `hotspots` (highest fan-in/complexity symbols). "
        "`target` is the symbol/query; optional `engine` (auto|graph|lsp|semantic), `project_root`. "
        "Never raises: `ok` is always true; a null `result` + `reason` means not-found/not-indexed."
    ))
    mcp.add_tool(_code_status, name="code.status", description=(
        "Which engines (graph/LSP/semantic) are available and whether this repo is indexed. Check "
        "this first if code.query keeps returning nothing."
    ))
    mcp.add_tool(_code_doctor, name="code.doctor", description=(
        "Diagnose engine health + this repo's index status, with a concrete fix for each gap (what "
        "to install or index). Run when code.query results look empty or an engine seems missing."
    ))
    mcp.add_tool(_code_map, name="code.map", description=(
        "Generate/refresh CODE_INTEL.md — a ranked architecture overview (node/edge counts, top "
        "symbols by caller count, entry points, routes). A great first call to orient on an "
        "unfamiliar repo."
    ))

    anyio.run(mcp.run_stdio_async)
