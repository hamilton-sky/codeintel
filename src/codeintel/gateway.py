from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from codeintel.cache import ContentHashCache
from codeintel.policy import TieringPolicy
from codeintel.provider import Result, log_swallowed, safe_null_result
from codeintel.providers.none import NoneProvider
from codeintel.reindexer import Reindexer

_KNOWN_ENGINES: frozenset[str] = frozenset({"graph", "lsp", "semantic", "auto", "both", "all"})
_FANOUT_ENGINES: frozenset[str] = frozenset({"both", "all"})

# op → preferred single engine for auto-dispatch
_AUTO_ENGINE: dict[str, str] = {
    "impact": "graph",
    "callers": "graph",
    "callees": "graph",
    "chain": "graph",
    "pattern": "graph",
    "overview": "graph",
    "symbol": "lsp",
    "search": "semantic",
    "context": "both",  # fan-out; resolved in Phase 4
}


class Gateway:
    def __init__(self, graph=None, lsp=None, semantic=None, policy: TieringPolicy | None = None, reindexer: Reindexer | None = None):
        # Backward-compat: old tests pass a list as the first positional arg.
        if isinstance(graph, list):
            self._legacy_providers: list | None = graph
            self.graph = None
            self.lsp = None
            self.semantic = None
        else:
            self._legacy_providers = None
            self.graph = graph
            self.lsp = lsp
            self.semantic = semantic
        self._none = NoneProvider()
        self._cache = ContentHashCache()
        self._policy = policy
        self._reindexer = reindexer or Reindexer()

    def _provider_for(self, engine_str: str):
        if engine_str == "graph":
            return self.graph
        if engine_str == "lsp":
            return self.lsp
        if engine_str == "semantic":
            return self.semantic
        return None

    def _fan_out(
        self,
        engines: list[str],
        op_str: str,
        target_str: str,
        budget: Any,
        project_root: Any,
    ) -> dict[str, Result]:
        def _call(engine_str: str) -> tuple[str, Result]:
            provider = self._provider_for(engine_str)
            return engine_str, self._dispatch_single(
                provider, op_str, target_str, budget, project_root, engine_str
            )

        results: dict[str, Result] = {}
        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(_call, e): e for e in engines}
                for future in as_completed(futures):
                    try:
                        engine_str, result = future.result()
                        results[engine_str] = result
                    except Exception:
                        engine_str = futures[future]
                        results[engine_str] = safe_null_result(
                            op_str, target_str, engine=engine_str, reason="provider-error"
                        )
        except Exception:
            for e in engines:
                if e not in results:
                    results[e] = safe_null_result(op_str, target_str, engine=e, reason="provider-error")
        return results

    def _merge(
        self,
        results: dict[str, Result],
        op_str: str,
        target_str: str,
        engine_str: str = "merged",
    ) -> Result:
        parts: list[str] = []
        for eng, r in results.items():
            if r.get("result") is not None:
                parts.append(f"## [{eng}]\n{r['result']}")

        if not parts:
            return safe_null_result(op_str, target_str, engine=engine_str, reason="no-result")

        return {
            "ok": True,
            "op": op_str,
            "target": target_str,
            "result": "\n\n".join(parts),
            "engine": engine_str,
            "cached": False,
        }

    def _dispatch_single(
        self,
        provider,
        op_str: str,
        target_str: str,
        budget,
        project_root,
        engine_str: str,
    ) -> Result:
        if provider is None:
            return safe_null_result(op_str, target_str, engine=engine_str, reason="engine-unavailable")
        if not getattr(provider, "available", True):
            return safe_null_result(op_str, target_str, engine=engine_str, reason="engine-unavailable")
        try:
            r = provider.build_result(op_str, target_str, [], budget or 0, project_root or "")
            if r is not None:
                return r
            return safe_null_result(op_str, target_str, engine=engine_str, reason="no-result")
        except Exception as exc:
            log_swallowed(f"Gateway._dispatch_single[{engine_str}.{op_str}]", exc)
            return safe_null_result(op_str, target_str, engine=engine_str, reason="provider-error")

    def allows(self, role: str, op: str) -> bool:
        """Whether *role* may run *op* under the current policy (True when no policy is configured).
        Lets non-query handlers (e.g. doctor) share the same RBAC gate as query()."""
        try:
            return self._policy is None or self._policy.is_allowed(role, op)
        except Exception:
            return True

    def query(
        self,
        op=None,
        target=None,
        engine=None,
        role: str = "",
        budget=None,
        project_root=None,
    ) -> Result:
        try:
            op_str = str(op or "")
            target_str = str(target or "")
            engine_str = str(engine or "").strip() or "auto"
            was_auto = engine_str == "auto"

            # Policy check FIRST — a role denied for this op does NO work (no reindex, no dispatch,
            # no cache lookup). Applies to the modern provider path; the legacy list path has none.
            if (
                self._legacy_providers is None
                and self._policy is not None
                and not self._policy.is_allowed(role, op_str)
            ):
                return safe_null_result(op_str, target_str, reason="op-not-allowed-for-role")

            try:
                self._reindexer.maybe_reindex(str(project_root or ""))
            except Exception:
                pass

            # Legacy list-based path (backward compat with pre-Phase-2 tests)
            if self._legacy_providers is not None:
                for p in self._legacy_providers:
                    try:
                        r = p.build_result(op_str, target_str, [], budget or 0, project_root or "")
                        if r is not None:
                            return r
                    except Exception:
                        continue
                reason = "engine-unavailable" if engine is not None else "no-result"
                return safe_null_result(op_str, target_str, reason=reason)

            # Unknown engine — reject immediately
            if engine_str not in _KNOWN_ENGINES:
                return safe_null_result(op_str, target_str, reason="unknown-engine")

            # Auto: resolve by op
            if engine_str == "auto":
                engine_str = _AUTO_ENGINE.get(op_str, "graph")

            root_str = project_root or ""

            # Freshness token — bumps when a background reindex completes, so a cached
            # structural answer (a symbol/free-text target, whose content hash never
            # changes) is invalidated once the index actually moves. 0 when unavailable.
            try:
                freshness = self._reindexer.generation(root_str)
            except Exception:
                freshness = 0

            # Fan-out: dispatch to multiple engines concurrently and merge
            if engine_str in _FANOUT_ENGINES:
                cached_result = self._cache.get(op_str, target_str, engine_str, root_str, freshness)
                if cached_result is not None:
                    return {**cached_result, "cached": True}
                if engine_str == "both":
                    engines = ["graph", "lsp"]
                else:  # "all"
                    engines = ["graph", "lsp", "semantic"]
                fan_results = self._fan_out(engines, op_str, target_str, budget, project_root)
                result = self._merge(fan_results, op_str, target_str, engine_str)
                self._cache.put(op_str, target_str, engine_str, root_str, result, freshness)
                return result

            # Single-engine dispatch
            cached_result = self._cache.get(op_str, target_str, engine_str, root_str, freshness)
            if cached_result is not None:
                return {**cached_result, "cached": True}
            provider = self._provider_for(engine_str)
            result = self._dispatch_single(provider, op_str, target_str, budget, project_root, engine_str)

            # overview auto-fallback (F4 Story 2): when auto-routed to graph but graph can't serve
            # it — the backend is unavailable, OR this repo simply isn't in the graph — try lsp,
            # which can produce a file/symbol overview without the graph index.
            if (
                was_auto
                and op_str == "overview"
                and engine_str == "graph"
                and result.get("result") is None
                and result.get("reason") in ("engine-unavailable", "project-not-indexed")
            ):
                lsp_result = self._dispatch_single(
                    self.lsp, op_str, target_str, budget, project_root, "lsp"
                )
                if lsp_result.get("result") is not None:
                    result = lsp_result

            self._cache.put(op_str, target_str, engine_str, root_str, result, freshness)
            return result

        except Exception as exc:
            log_swallowed("Gateway.query", exc)
            return safe_null_result(op or "", target or "", reason="gateway-error")
