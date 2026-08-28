from __future__ import annotations

import os
import threading
import time
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from codeintel.gateway import Gateway
from codeintel.policy import TieringPolicy
from codeintel.provider import Result, safe_null_result
from codeintel.providers.graph import GraphProvider
from codeintel.providers.lsp import LspProvider
from codeintel.providers.semantic import SemanticProvider
from codeintel.redact import redact
from codeintel.reindexer import Reindexer

_REINDEXER = Reindexer()


def _build_gateway(oneshot: bool = False) -> Gateway:
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
    # `blocking_index=oneshot`: a one-shot CLI process has no other way to ever build a cold index
    # and can afford to wait, so it keeps today's synchronous inline pass. The long-lived MCP/HTTP
    # server must not block a request thread for a multi-minute cold index — see
    # SemanticProvider.build_result / SemanticProvider.__init__ for the full rationale.
    semantic = SemanticProvider(blocking_index=oneshot)
    # RBAC: when an auth config defines restricted roles, enforce role→op; otherwise the policy is
    # disabled (full access). The per-request role is authenticated server-side by the HTTP layer;
    # the local MCP agent passes role="" (unrestricted), so RBAC never affects the stdio transport.
    try:
        from codeintel.auth import load_auth
        auth = load_auth()
        policy = auth.build_policy() if auth.enabled else TieringPolicy(enabled=False)
    except Exception:
        policy = TieringPolicy(enabled=False)
    return Gateway(graph=graph, lsp=lsp, semantic=semantic, policy=policy, reindexer=_REINDEXER,
                   oneshot=oneshot)


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
    global _GATEWAY, _LAST_ENGINE_REFRESH
    with _GATEWAY_LOCK:
        _GATEWAY = None
    with _REFRESH_LOCK:
        _LAST_ENGINE_REFRESH = 0.0


# An engine missing at boot is re-probed at most this often on the query path.
_ENGINE_REFRESH_INTERVAL_S = 30.0
_LAST_ENGINE_REFRESH: float = 0.0
_REFRESH_LOCK = threading.Lock()


def _refresh_missing_engines(gw: Gateway) -> None:
    """Attach any engine whose backend was installed AFTER this process started.

    The gateway is built once per process, so without this an engine absent at boot stays absent
    for the entire agent session — including after the user follows doctor's remediation and
    installs the backend. Constructing a provider is a `shutil.which` and nothing more (the serena
    session and the graph project cache are built lazily, per call), so this costs one PATH scan
    per still-missing engine, throttled to once per 30s and skipped entirely once all three are
    present. Only fills empty slots, so nothing warmed is ever discarded. Never raises."""
    global _LAST_ENGINE_REFRESH
    try:
        missing = [n for n in ("graph", "lsp", "semantic") if getattr(gw, n, None) is None]
        if not missing:
            return
        now = time.monotonic()
        with _REFRESH_LOCK:
            if now - _LAST_ENGINE_REFRESH < _ENGINE_REFRESH_INTERVAL_S:
                return
            _LAST_ENGINE_REFRESH = now
        builders = {"graph": GraphProvider, "lsp": LspProvider, "semantic": SemanticProvider}
        for name in missing:
            try:
                provider = builders[name]()
                # Match the blocking behavior `_build_gateway` gave this gateway's OTHER engines —
                # a semantic engine adopted mid-session on a long-lived server must never block a
                # request thread for a cold index any more than one present at boot would. Attribute
                # set post-construction (rather than passed in) so a caller's own provider stand-in
                # — real or a test double — need not accept this constructor argument at all.
                if name == "semantic" and hasattr(provider, "_blocking_index"):
                    provider._blocking_index = gw.oneshot
                gw.adopt_provider(name, provider)
            except Exception:
                continue
    except Exception:
        pass


def code_query_handler(args: dict) -> Result:
    try:
        op = args.get("op", "")
        target = args.get("target", "")
        project_root = str(args.get("project_root", "") or "")
        engine = args.get("engine")
        role = args.get("role", "")
        gw = _get_gateway()
        _refresh_missing_engines(gw)
        # `project_root` is documented as optional (this tool's description below), but every
        # provider on the query path hard-fails without one — `semantic.py`'s `build_result`
        # returns `reason="no-project-root"`, and the graph/lsp providers fail project resolution
        # on a blank root the same way. There is no cwd default anywhere on this path, unlike
        # `code.status`/`code.doctor`, which already fall back to `os.getcwd()` inside
        # `doctor.run_doctor`. Left alone, an agent that trusts "optional" gets a null envelope
        # that this tool's own instructions teach it to read as "the code doesn't exist".
        #
        # RESOLUTION: fall back to the server's cwd, so the doc and the behavior finally agree, and
        # `code.query` matches `code.status`/`code.doctor` instead of being the one tool that
        # silently does nothing. The fallback is applied ONLY after `Gateway.allows_root` accepts
        # the RAW, un-substituted value — never before it. `TieringPolicy.is_root_allowed`
        # (policy.py) deliberately REJECTS a blank `project_root` rather than resolving it,
        # specifically because `os.path.realpath("")` is the server's own cwd: substituting cwd
        # BEFORE that check would reopen exactly the RBAC bypass that guard exists to close.
        # Checking first reproduces today's RBAC-enabled behavior unchanged (a restricted role with
        # a blank root still gets `root-not-allowed-for-role` from inside `gw.query`), and only
        # engages the fallback when RBAC is disabled/absent or the role is unconditionally allowed
        # (e.g. `roots = ["*"]`) — the same gate `code.status`/`code.doctor` already run before
        # their own cwd fallback.
        #
        # SCOPED TO STDIO. `allow_cwd_default` is set False by `http_server.py`, server-side, for
        # every request on that transport: there the cwd is an arbitrary server-side directory
        # rather than the caller's repo, and defaulting to it both answers the wrong question and
        # blocks the request behind a cold embedding-model load. Absent (stdio, and the CLI paths
        # that call this handler directly) it defaults to True, so the doc/behaviour agreement this
        # fallback exists for is kept exactly where cwd actually means something.
        if (not project_root
                and bool(args.get("allow_cwd_default", True))
                and gw.allows_root(role, project_root)):
            project_root = os.getcwd()
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
    "version_skew": None,
}


def _code_status_handler_inner(args: dict) -> dict:
    """Engine readiness for an agent.

    Reports the SAME tri-state doctor computes — installed / runnable / repo_indexed — against
    the SAME live providers a real query hits (the singleton gateway's, including the warmed
    LSP session). It previously constructed throwaway providers and reported a single flat
    boolean per engine, so `lsp: true` could mean "uvx is on PATH" for an engine that in fact
    never boots — an agent reading that would reason from a readiness claim nothing verified.

    The flat `graph`/`lsp`/`semantic`/`indexed`/`model` keys keep their original meaning for
    existing callers; `readiness` and `healthy` carry the full picture.

    `on_provider` closes the last gap between what this reports and what a query can do: for an
    engine the gateway lacks, doctor builds an ephemeral provider to probe — and any engine it
    finds installed is adopted onto the gateway, so a readiness claim made here is one the very
    next `code.query` can actually honor."""
    try:
        from codeintel import doctor as _doctor

        project_root = str(args.get("project_root", "") or "")
        gw = _get_gateway()
        # Same root scoping `query` enforces. Without it this endpoint answered "is THAT directory
        # indexed?" for any path, to any authenticated token, regardless of the role's [roots] —
        # cross-tenant disclosure through the door next to the one that got closed.
        role = str(args.get("role", "") or "")
        if not gw.allows_root(role, project_root):
            return dict(_STATUS_FALLBACK)
        report = _doctor.run_doctor(
            project_root, deep=False, graph=gw.graph, lsp=gw.lsp, semantic=gw.semantic,
            on_provider=gw.adopt_provider,
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
            # Null on the normal path. Non-null means every other field above describes the code
            # this process loaded at startup rather than the code that is installed — so it is
            # surfaced on `status`, not just in `doctor`, because `status` is what a caller checks
            # when an expected fix appears to be missing.
            "version_skew": report.get("version_skew") if isinstance(report, dict) else None,
        }
    except Exception:
        return dict(_STATUS_FALLBACK)


def _code_doctor_handler_inner(args: dict) -> dict:
    try:
        from codeintel import doctor as _doctor

        project_root = str(args.get("project_root", "") or "")
        deep = bool(args.get("deep", False))
        role = str(args.get("role", "") or "")
        # Reuse the singleton gateway's providers so the report reflects the LIVE warmed LSP
        # session state an agent's real queries hit (and the graph project cache). Engines the
        # gateway lacks are probed on a fresh provider and, when installed, adopted onto it — so
        # following this report's own remediation ("install X") converges on the next call rather
        # than needing the MCP host restarted.
        gw = _get_gateway()
        # RBAC: doctor is a privileged op (engine state + a deep LSP boot on an arbitrary path), so
        # it's gated behind the "doctor" scope — a restricted role must list it (or use "*").
        if not gw.allows(role, "doctor"):
            return {
                "ok": True, "project_root": project_root, "deep": deep,
                "summary": {"ready": 0, "total": 3, "healthy": False},
                "engines": {}, "reason": "op-not-allowed-for-role",
            }
        # An op gate alone leaves the TARGET unbounded: a role scoped to /srv/team-a could still
        # run doctor against /srv/team-b, and `deep: true` boots a live LSP session rooted there.
        if not gw.allows_root(role, project_root):
            return {
                "ok": True, "project_root": project_root, "deep": deep,
                "summary": {"ready": 0, "total": 3, "healthy": False},
                "engines": {}, "reason": "root-not-allowed-for-role",
            }
        return _doctor.run_doctor(
            project_root, deep=deep, graph=gw.graph, lsp=gw.lsp, semantic=gw.semantic,
            on_provider=gw.adopt_provider,
        )
    except Exception:
        return {
            "ok": True, "project_root": "", "deep": False,
            "summary": {"ready": 0, "total": 3, "healthy": False},
            "engines": {}, "note": "doctor-error",
        }


def _code_map_handler_inner(args: dict) -> dict:
    try:
        from codeintel.injector import Injector
        from codeintel.mapper import MapGenerator

        # Same "documented optional, actually required" defect as `code.query` — see
        # `code_query_handler`'s comment. `code.map` has no HTTP transport (only `code.query`/
        # `code.status`/`code.doctor` are reachable over HTTP; see http_server.py), so unlike
        # `code_query_handler` there is no `TieringPolicy.is_root_allowed` blank-rejection guard to
        # preserve here — the stdio MCP transport always runs unrestricted (role=""), so falling
        # back to cwd unconditionally is safe and keeps this tool consistent with
        # `code.status`/`code.doctor`.
        project_root = str(args.get("project_root", "") or "") or os.getcwd()
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
    "Before you edit, run `changed` to see which symbols your uncommitted edits ripple into; "
    "reach for `hotspots` (complexity/fan-in risk) when planning a refactor.\n\n"
    "Orient on a new repo with `code.map` (ranked architecture: top symbols, entry points, routes). "
    "If results look empty, call `code.doctor` — it says exactly what to index or install. "
    "`code.status` reports engine health.\n\n"
    "Every result is a safe envelope: `ok` is always true; a null `result` with a `reason` means "
    "'nothing found / not indexed yet', NOT an error — read the `reason`/`hint` and, if it says the "
    "repo isn't indexed, that resolves on the first query or via `codeintel index`.\n\n"
    "A NON-null `result` is not a promise that the answer is whole. Check `confidence`: when it is "
    "`partial`, a named part of the answer could not be retrieved — `gaps` says which section and "
    "why, and the body text says so too. Treat a partial reference list as 'unknown', never as "
    "'none': the difference decides whether deleting or changing a symbol is safe."
)


# The op vocabulary as a schema-level enum rather than prose. Prose in a tool DESCRIPTION is read
# once, at connect time, describing fields the model fills in later — the wrong place for the
# thing that most needs to be right. A
# `Literal` here means a mistyped/hallucinated op is rejected by MCP's own argument validation
# with the real choices listed, instead of round-tripping to `graph.py`'s `unsupported-op` — which
# a skimming agent can misread as "found nothing" — and it gives the description string above room
# to describe each op's call signature instead of just its name.
_QueryOp = Literal[
    "search", "symbol", "callers", "callees", "impact", "chain",
    "pattern", "overview", "context", "changed", "hotspots",
]
_QueryEngine = Literal["auto", "graph", "lsp", "semantic", "both", "all"]

_OP_FIELD_DESCRIPTION = (
    "search(target) — find code by meaning or name; use instead of grep. "
    "symbol(target) — definition/signature/docstring (LSP). "
    "callers(target) / callees(target) — direct in/out call edges. "
    "impact(target) — callers+callees together; run before changing a symbol. "
    "context(target) — impact PLUS the LSP definition, merged; the fullest single-symbol view. "
    "chain(target=\"A->B\") — call path between two symbols, risk-labeled. "
    "pattern(target) — literal/regex match ranked by graph importance (graph-augmented grep). "
    "overview() — this repo's architecture; `target` is IGNORED. "
    "changed() — impact of your uncommitted git edits; `target` is IGNORED. "
    "hotspots() — highest fan-in/complexity symbols; `target` is IGNORED."
)
_TARGET_FIELD_DESCRIPTION = (
    "The symbol name or natural-language query. Ignored by `overview`/`changed`/`hotspots` — "
    "those answer for the whole repo, scoped by `project_root` alone."
)
_PROJECT_ROOT_FIELD_DESCRIPTION = (
    "Absolute path to the repo root. Optional on this (stdio) transport: if omitted, it falls back "
    "to the server's current working directory — the same default `code.status`/`code.doctor` "
    "already use, and on stdio that is the repo the server was launched in. Over the HTTP "
    "transport there is no such default (the server's cwd is unrelated to the caller), and under "
    "role-based access control a blank value is rejected rather than defaulted — so pass it "
    "explicitly whenever you know it."
)
_ENGINE_FIELD_DESCRIPTION = "Which engine answers. Leave as `auto` — it already picks the right engine per op."
_ROLE_FIELD_DESCRIPTION = (
    "Reserved for the HTTP transport, which sets it server-side from the caller's auth token and "
    "overrides anything supplied here (no escalation is possible). Leave unset on this (stdio) "
    "transport — it has no effect."
)


def run() -> None:
    from codeintel import __version__
    from codeintel.logconfig import configure_logging
    configure_logging()  # logs to stderr; stdout is the MCP protocol channel
    mcp = MCPServer(name="codeintel", version=__version__, instructions=_MCP_INSTRUCTIONS)

    async def _code_query(
        op: Annotated[_QueryOp, Field(description=_OP_FIELD_DESCRIPTION)],
        target: Annotated[str, Field(description=_TARGET_FIELD_DESCRIPTION)] = "",
        project_root: Annotated[str, Field(description=_PROJECT_ROOT_FIELD_DESCRIPTION)] = "",
        engine: Annotated[_QueryEngine, Field(description=_ENGINE_FIELD_DESCRIPTION)] = "auto",
        role: Annotated[str, Field(description=_ROLE_FIELD_DESCRIPTION)] = "",
    ) -> dict:
        # MUST stay `dict`, not `Result`. FastMCP derives this tool's output schema from the return
        # annotation, and it validates a TypedDict's NotRequired keys (`reason`, `hint`) as
        # REQUIRED — so every successful query, which carries neither, comes back to the agent as
        # `isError: true`. Guarded by test_mcp_server.py::test_no_tool_advertises_the_optional_*.
        return dict(code_query_handler(
            {"op": op, "target": target, "project_root": project_root, "engine": engine, "role": role}
        ))

    async def _code_status(
        project_root: Annotated[str, Field(description=_PROJECT_ROOT_FIELD_DESCRIPTION)] = "",
    ) -> dict:
        return code_status_handler({"project_root": project_root})

    async def _code_doctor(
        project_root: Annotated[str, Field(description=_PROJECT_ROOT_FIELD_DESCRIPTION)] = "",
        deep: Annotated[bool, Field(description=(
            "Also boot a live LSP (serena) session to verify it actually runs, not just that it is "
            "installed on PATH. Read-only, but SLOW (first boot can take several seconds) — leave "
            "false for a quick check."
        ))] = False,
    ) -> dict:
        return code_doctor_handler({"project_root": project_root, "deep": deep})

    async def _code_map(
        project_root: Annotated[str, Field(description=_PROJECT_ROOT_FIELD_DESCRIPTION)] = "",
        budget: Annotated[int, Field(description=(
            "Maximum size of the generated CODE_INTEL.md, in BYTES (default 32768). Content beyond "
            "the budget is dropped, not truncated mid-line, and the file says so."
        ))] = 32768,
        inject: Annotated[bool, Field(description=(
            "When true, ALSO writes a second file: appends a reference block to this repo's "
            "CLAUDE.md or AGENTS.md, pointing at CODE_INTEL.md. That edits a file which shapes an "
            "agent's future behavior, so only set this if the user asked for it — default is "
            "false, which writes CODE_INTEL.md alone."
        ))] = False,
    ) -> dict:
        return code_map_handler({"project_root": project_root, "budget": budget, "inject": inject})

    mcp.add_tool(
        _code_query, name="code.query",
        annotations=ToolAnnotations(read_only_hint=True),
        description=(
            "Reach for this BEFORE grep or reading files to: find who calls X (callers), what X "
            "calls (callees), the impact of changing Y, where Z is defined, how call A reaches B "
            "(chain), or find the code that does W (search/pattern) — graph+LSP+semantic in one "
            "read-only call. Caveat: `callers`/`callees`/`impact`/`chain`/`pattern`/`overview`/"
            "`hotspots` answer from the last index snapshot, stale until the repo is re-indexed. "
            "See each parameter's own description below for the full op list, each op's call "
            "signature, and which ops ignore `target`. "
            "When several symbols share a name, `callers`/`callees`/`impact` report each one "
            "separately and say so — narrow to one with a qualified target (`core.Group.invoke`) or "
            "a file hint (`invoke@src/click/testing.py`). "
            "Never raises: `ok` is always true; a null `result` + `reason` means not-found/not-indexed. "
            "A non-null `result` may still be incomplete — check `confidence`/`gaps`."
        ),
    )
    mcp.add_tool(
        _code_status, name="code.status",
        annotations=ToolAnnotations(read_only_hint=True),
        description=(
            "Read-only. Which engines (graph/LSP/semantic) are available and whether this repo is "
            "indexed. Check this first if code.query keeps returning nothing."
        ),
    )
    mcp.add_tool(
        _code_doctor, name="code.doctor",
        annotations=ToolAnnotations(read_only_hint=True),
        description=(
            "Read-only (see `deep`'s own description — it boots a live session but writes nothing). "
            "Diagnose engine health + this repo's index status, with a concrete fix for each gap "
            "(what to install or index). Run when code.query results look empty or an engine seems "
            "missing."
        ),
    )
    mcp.add_tool(
        _code_map, name="code.map",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
        description=(
            "WRITES a file: generates/refreshes CODE_INTEL.md at the repo root — a ranked "
            "architecture overview (node/edge counts, top symbols by caller count, entry points, "
            "routes), stamped with the generation time and the index counts it was built from. Good "
            "first call to orient on an unfamiliar repo. To read the same information without "
            "writing anything, use `code.query` with `op=\"overview\"` instead. Never overwrites a "
            "populated map with a degraded one. See `inject`'s own description before setting it — "
            "it writes a SECOND file, your CLAUDE.md/AGENTS.md."
        ),
    )

    anyio.run(mcp.run_stdio_async)


# `redact` was placed on the Gateway.query seam alone, which covered `code.query` and nothing else.
# These three handlers reach the same callers over the same transports — `code.doctor` was measured
# emitting nine absolute home paths on a single call — so they route through the same function. One
# wrapper each, so a handler cannot be added later that quietly bypasses it: see
# tests/test_incompleteness.py::test_no_mcp_handler_bypasses_redaction, which enumerates them.
def code_status_handler(args: dict) -> dict:
    return redact(_code_status_handler_inner(args))  # type: ignore[return-value]


def code_doctor_handler(args: dict) -> dict:
    return redact(_code_doctor_handler_inner(args))  # type: ignore[return-value]


def code_map_handler(args: dict) -> dict:
    return redact(_code_map_handler_inner(args))  # type: ignore[return-value]
