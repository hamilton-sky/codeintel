"""Build a Gateway with whatever engines this machine can run.

Lives in the gateway layer so every front door (the MCP server, the HTTP server through it, and the
one-shot CLI) builds the gateway the same way without importing a transport to do it.
"""
from __future__ import annotations

from codeintel.engines.graph.provider import GraphProvider
from codeintel.engines.lsp.provider import LspProvider
from codeintel.engines.semantic.provider import SemanticProvider
from codeintel.gateway.gateway import Gateway
from codeintel.gateway.policy import TieringPolicy
from codeintel.gateway.reindexer import Reindexer

# One reindexer per process: its debounce and in-flight state must be shared by every gateway the
# process builds, or two gateways would each think no reindex is running.
REINDEXER = Reindexer()


def build_gateway(oneshot: bool = False) -> Gateway:
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
        from codeintel.gateway.auth import load_auth
        auth = load_auth()
        policy = auth.build_policy() if auth.enabled else TieringPolicy(enabled=False)
    except Exception:
        policy = TieringPolicy(enabled=False)
    return Gateway(graph=graph, lsp=lsp, semantic=semantic, policy=policy, reindexer=REINDEXER,
                   oneshot=oneshot)

