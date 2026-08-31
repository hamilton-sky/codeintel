from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from codeintel.cache import ContentHashCache
from codeintel.policy import TieringPolicy
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result
from codeintel.providers.none import NoneProvider
from codeintel.redact import redact
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
    "changed": "graph",
    "changes": "graph",
    "hotspots": "graph",
    "symbol": "lsp",
    "search": "semantic",
    "context": "both",  # fan-out; resolved in Phase 4
}

# Ops whose answer depends on live, unhashable state (the git worktree) rather than the indexed
# content the cache key is built from — caching them would serve a stale answer within a freshness
# generation. `changed` reads uncommitted edits; `hotspots` is a pure function of the index and
# stays cached (correctly keyed by the freshness generation). A retired/unknown op falls through
# to the graph engine (the `_AUTO_ENGINE.get(op, "graph")` default), which safe-nulls it.
_UNCACHED_OPS: frozenset[str] = frozenset({"changed", "changes"})

# Engines whose slot can be filled after construction (see `Gateway.adopt_provider`).
_ADOPTABLE_ENGINES: frozenset[str] = frozenset({"graph", "lsp", "semantic"})


def _mark_reindexing(result: Result, reindexing: bool) -> Result:
    """Flag an answer served while a reindex for its project is still running."""
    if not reindexing or result.get("result") is None:
        return result
    return {**result, "reindexing": True,
            "hint": "a reindex is in progress — this answer reflects the index as of the last "
                    "completed pass; re-ask shortly if you have just changed this code"}


class Gateway:
    def __init__(self, graph=None, lsp=None, semantic=None, policy: TieringPolicy | None = None,
                 reindexer: Reindexer | None = None, oneshot: bool = False):
        # A one-shot process (the `codeintel` CLI) must not run the long-lived server's background
        # machinery. It used to: every query called `maybe_reindex`, which in a fresh process always
        # passed the debounce (`_last_fired` starts empty) and submitted a pass to a DAEMON pool —
        # then the same query asked `reindex_pending` ten lines later and was told "yes", by itself.
        # That is why `reindexing: true` accompanied literally every answer this tool has ever
        # produced, and why the tree was re-walked on every query. Worse, a daemon thread is killed
        # wherever it happens to be when the process exits, so those passes never completed and wrote
        # torn state on the way out — the most plausible source of the `.corrupt` index files found
        # in the cache. A process that cannot finish a reindex must not start one.
        self._oneshot = bool(oneshot)
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
        self._adopt_lock = threading.Lock()

    @property
    def oneshot(self) -> bool:
        """Whether this gateway serves a single request then exits (the CLI) rather than a
        long-lived process (MCP stdio / HTTP). Exposed so a caller re-building a slot-filling
        provider — e.g. `server._refresh_missing_engines` — can match the blocking behavior the
        gateway was originally constructed with, instead of guessing or hardcoding it."""
        return self._oneshot

    def adopt_provider(self, engine: str, provider: Any) -> bool:
        """Fill an EMPTY engine slot with a provider that has just been proven installed.

        The server builds ONE gateway per process, so an engine whose backend was missing at boot
        stayed missing for the whole agent session — while `code.status`/`code.doctor`, which probe
        FRESH providers when the gateway's slot is None, reported that same engine healthy. Status
        therefore claimed an engine that `code.query` could never reach, and doctor's own
        remediation loop ("install codebase-memory-mcp, then re-check") never converged short of
        restarting the MCP host.

        Only ever fills a `None` slot — a live provider is never replaced, so the warmed serena
        session and the graph project cache that the singleton exists to preserve are untouched.
        Never raises. Returns True when a slot was actually filled.
        """
        try:
            if engine not in _ADOPTABLE_ENGINES or self._legacy_providers is not None:
                return False
            with self._adopt_lock:
                if getattr(self, engine, None) is not None:
                    return False  # live provider — keep its warmed state
                if provider is None or getattr(provider, "available", False) is not True:
                    return False
                setattr(self, engine, provider)
            # A newly reachable engine can change the answer to a query already cached under an
            # engine that could not serve it — e.g. `overview` auto-falls back to lsp when graph is
            # absent and caches that answer under the *graph* key. Neither the content hash nor the
            # freshness token can see this, so drop the cache. Cheap: adoption happens <=3x/process.
            try:
                self._cache.clear()
            except Exception:
                pass
            return True
        except Exception:
            return False

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

    # Ops whose whole purpose is "what breaks if I change this", and which therefore must not hand
    # back a list of fabricated callers unchallenged.
    _CROSS_CHECKED_OPS: frozenset[str] = frozenset({"callers", "impact"})
    _CROSS_CHECK_REF_CAP = 25

    def _cross_check_name_resolved(
        self, result: Result, op: str, target: str, budget: Any,
        project_root: Any, was_auto: bool,
    ) -> Result:
        """Ask the LSP when the graph's whole answer was resolved by name rather than by import.

        This closes the routing gap that let the worst failure through. `_AUTO_ENGINE` is a static
        op→engine map, not a cascade: `callers` goes to the graph and, uniquely among the ops, has
        no fallback of any kind. So when the graph answered `callers describe` with 32 rows that were
        every vitest `describe()` call in the repository — bound to the project's own
        `domain.budget.describe` because it was the only indexed symbol with that name — the LSP,
        which had the correct answer sitting in its reference index, was never asked. One engine
        being confidently wrong is a bug; no second engine ever being consulted is the design that
        let it reach the caller.

        Deliberately narrow. It fires only when the graph itself raised `all-rows-name-resolved` —
        every row a name guess, across enough rows for the pattern to mean something — which is the
        collision signature and not the ordinary case of one unverified row among several. It
        APPENDS rather than replaces: the LSP answers a related but different question (references,
        not call edges), so presenting its list as the graph's would substitute one over-claim for
        another. And it never fires for an explicitly pinned `--engine graph`, where the caller has
        said which engine they want."""
        try:
            if not was_auto or op not in self._CROSS_CHECKED_OPS:
                return result
            if result.get("result") is None or self.lsp is None:
                return result
            gaps = result.get("gaps") or []
            if not any(g.get("kind") == "all-rows-name-resolved"
                       for g in gaps if isinstance(g, dict)):
                return result
            probe = self._dispatch_single(
                self.lsp, "symbol", target, budget, project_root, "lsp")
            body = probe.get("result")
            if not body:
                # Silence from the LSP is not agreement. Say which check did not happen — and
                # separate "not yet booted" from "had nothing", because only the first is fixed by
                # asking again. A one-shot CLI process meets a cold serena on every invocation; the
                # long-lived MCP server keeps the session warm and takes this branch once at most.
                # Waiting here is deliberately NOT done: it would hold back a graph answer that is
                # already complete, to append a section that is only advisory.
                warming = probe.get("reason") == "warming"
                why = ("the language server had not finished booting"
                       if warming else "the LSP engine reported nothing for this symbol")
                nxt = (" Ask again once it is warm and this section will be filled in."
                       if warming else
                       " Check it yourself with `--engine lsp --op symbol`.")
                return {**result, "gaps": [*gaps, {
                    "section": op, "kind": "cross-check-unavailable",
                    "engine": "lsp",
                    "reason": probe.get("reason") or "no-result",
                    "detail": f"every graph row was name-resolved and the LSP could not confirm "
                              f"them ({why}), so they remain unverified"
                              + (" — retry" if warming else ""),
                    **({"retry_after_s": 2} if warming else {}),
                }], "result": str(result["result"]) + (
                    f"\n\n> Cross-check unavailable: every row above was resolved by name, and "
                    f"{why}, so nothing here has been confirmed against a second engine.{nxt}"
                )}
            refs = self._reference_lines(str(body))
            listing = "\n".join(refs[: self._CROSS_CHECK_REF_CAP]) or "(no references reported)"
            more = (f"\n… (+{len(refs) - self._CROSS_CHECK_REF_CAP} more)"
                    if len(refs) > self._CROSS_CHECK_REF_CAP else "")
            merged = (
                f"{result['result']}\n\n## Cross-check — LSP references to `{target}` "
                f"({len(refs)})\n"
                f"_The rows above were all resolved by NAME by the graph engine. These come from the "
                f"language server, which resolves through the file's imports. A caller listed above "
                f"but absent here is very likely a name collision; a location here but missing above "
                f"is a call the graph could not bind._\n" + listing + more
            )
            return {**result, "result": merged, "gaps": [*gaps, {
                "section": op, "kind": "cross-checked-with-lsp",
                "engine": "lsp",
                "detail": f"every graph row was name-resolved, so the LSP was consulted "
                          f"independently and reported {len(refs)} reference location(s); the two "
                          f"lists answer related but different questions and are shown separately",
            }]}
        except Exception as exc:
            log_swallowed("Gateway._cross_check_name_resolved", exc)
            return result

    @staticmethod
    def _reference_lines(lsp_body: str) -> list[str]:
        """The reference rows out of an LSP `symbol` answer, without its definition body.

        The definition is already one line above in the graph's own answer; repeating a whole
        function body inside a cross-check section would bury the thing the section is for."""
        out: list[str] = []
        in_refs = False
        for line in lsp_body.splitlines():
            if line.startswith("## References"):
                in_refs = True
                continue
            if in_refs:
                if line.startswith("## "):
                    break
                if line.startswith("- "):
                    out.append(line)
        return out

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
            # Every engine's own reason is discarded here unless we carry it out. "no-result" is
            # what an agent is told to read as "nothing found / not indexed yet" — so collapsing
            # "neither engine could even be asked" into it produces a confident "that symbol does
            # not exist" from a fan-out where both backends were simply missing. `context` is a
            # fan-out op by default, so this was the common path, and it is the one place the
            # codebase throws away the could-not-ask / asked-and-found-nothing distinction it is
            # otherwise careful to preserve per-provider.
            reasons = {eng: str(r.get("reason") or "no-result") for eng, r in results.items()}
            unreachable = {"engine-unavailable", "boot-failed", "warming", "project-not-indexed",
                           "project-not-indexed-standalone", "error", "timeout",
                           # An index pass that RAN and FAILED is a could-not-ask, not a
                           # found-nothing. `no-index` deliberately stays out: it means the pass
                           # completed and there was nothing to embed, which is an answer.
                           "index-failed"}
            all_unreachable = bool(reasons) and all(v in unreachable for v in reasons.values())
            summary = "engines-unavailable" if all_unreachable else "no-result"
            detail = ", ".join(f"{eng}: {why}" for eng, why in sorted(reasons.items()))
            return safe_null_result(
                op_str, target_str, engine=engine_str, reason=summary,
                hint=(f"no engine produced an answer — {detail}"
                      + ("; this is NOT evidence the target does not exist"
                         if all_unreachable else "")),
            )

        # A fan-out answer is only as whole as its parts. This used to hand-build a six-key envelope
        # and drop both `confidence` and `gaps` on the floor — so a `context` request (the DEFAULT
        # fan-out op) whose graph half timed out returned the lsp half alone, unqualified, and a
        # `partial` a provider had explicitly produced was destroyed on the way out. Worse, an engine
        # that answered NOTHING is silently absent from `parts`: the body simply does not mention it,
        # which reads as "that engine had nothing to add" rather than "that engine could not be asked".
        merged_gaps: list[dict] = []
        for eng, r in results.items():
            merged_gaps.extend({**g, "engine": eng}
                               for g in (r.get("gaps") or []) if isinstance(g, dict))
            if r.get("result") is None:
                merged_gaps.append({
                    "section": eng,
                    "kind": str(r.get("reason") or "no-result"),
                    "detail": f"the {eng} engine contributed nothing to this answer "
                              f"({r.get('reason') or 'no-result'})",
                    "engine": eng,
                })
        return attach_confidence({
            "ok": True,
            "op": op_str,
            "target": target_str,
            "result": "\n\n".join(parts),
            "engine": engine_str,
            "cached": False,
        }, merged_gaps)

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

    def allows_root(self, role: str, project_root: str) -> bool:
        """Whether *role* may target *project_root*. The companion to ``allows`` — an op gate alone
        leaves the TARGET unbounded, which is how `doctor` and `status` could still be pointed at
        any readable directory after `query` had been scoped."""
        try:
            return self._policy is None or self._policy.is_root_allowed(role, project_root)
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
        """Answer one question. Never raises.

        A thin wrapper over `_query`, existing so that redaction has exactly ONE seam to cover.
        Every leak found in the evaluation was in a field some renderer built and no one swept —
        the scope note inside `result`, the "index it standalone with:" command inside `hint`. Both
        are downstream of here, and so is anything added later."""
        result = self._query(op, target, engine, role, budget, project_root)
        try:
            return redact(result)  # type: ignore[return-value]
        except Exception as exc:
            log_swallowed("Gateway.query.redact", exc)
            return result

    def _query(
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

            # Policy check FIRST — a role denied here does NO work (no reindex, no dispatch, no
            # cache lookup, and critically no on-demand indexing walk). Applies to the modern
            # provider path; the legacy list path has none.
            if self._legacy_providers is None and self._policy is not None:
                if not self._policy.is_allowed(role, op_str):
                    return safe_null_result(op_str, target_str, reason="op-not-allowed-for-role")
                # `project_root` arrives in the request body. Without this check any role able to
                # call `search` could name ANY directory the server process can read, and the
                # semantic provider would walk, index, and return its contents — an op allowlist
                # never sees the target. Denied before maybe_reindex, so a rejected path is not
                # even touched.
                if not self._policy.is_root_allowed(role, str(project_root or "")):
                    return safe_null_result(op_str, target_str, reason="root-not-allowed-for-role",
                                            hint="this token's role is not scoped to that "
                                                 "project_root (see the [roots] table in auth.toml)")

            if not self._oneshot:
                try:
                    self._reindexer.maybe_reindex(str(project_root or ""))
                except Exception:
                    pass

            # If a reindex is running, this answer comes from the PREVIOUS index. Structural
            # answers (callers/impact/hotspots) hash a symbol name, not file bytes, so nothing
            # else in the envelope can reveal that — and an agent that just edited and asked
            # "what did I break?" lands precisely here. Busting the cache would not help: the
            # index itself is behind, so re-asking refetches the same stale data.
            # In one-shot mode this is always False and the flag is simply never emitted: no
            # reindex was started, so there is nothing for the answer to be behind. In server mode
            # the flag now means what it always claimed to — a pass this process did not start is
            # genuinely still running — which is why it is fixed at the cause rather than deleted.
            if self._oneshot:
                reindexing = False
            else:
                try:
                    reindexing = self._reindexer.reindex_pending(str(project_root or ""))
                except Exception:
                    reindexing = False

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

            # Cache under what was ASKED, not what auto resolved to. `auto` and an explicit
            # `graph` both resolved to "graph" and so shared one key — but they are different
            # questions: `auto` accepts the overview LSP fallback below, an explicit `graph`
            # does not. One `auto` miss therefore parked an LSP answer under the graph key, and
            # the next explicit `engine=graph` request got it back with `cached: true` and an
            # `engine: "lsp"` field contradicting its own request. Reachable on any cold start,
            # since "graph not indexed yet" is the normal first-query state.
            cache_engine = "auto" if was_auto else engine_str

            root_str = project_root or ""

            # Freshness token — bumps when a background reindex completes, so a cached
            # structural answer (a symbol/free-text target, whose content hash never
            # changes) is invalidated once the index actually moves. 0 when unavailable.
            try:
                freshness = self._reindexer.generation(root_str)
            except Exception:
                freshness = 0

            # Ops that read live, unhashable state (the git worktree) must never be served from the
            # content-hash cache — it can't see uncommitted edits. Computed ONCE here so it covers
            # BOTH the fan-out and the single-engine paths below (a miss on either serves a stale diff).
            uncacheable = op_str in _UNCACHED_OPS

            # Fan-out: dispatch to multiple engines concurrently and merge
            if engine_str in _FANOUT_ENGINES:
                cached_result = (
                    None if uncacheable
                    else self._cache.get(op_str, target_str, cache_engine, root_str, freshness)
                )
                if cached_result is not None:
                    return _mark_reindexing({**cached_result, "cached": True}, reindexing)
                # "both" is graph+lsp; "all" adds semantic.
                engines = ["graph", "lsp"] if engine_str == "both" else ["graph", "lsp", "semantic"]
                fan_results = self._fan_out(engines, op_str, target_str, budget, project_root)
                result = self._merge(fan_results, op_str, target_str, engine_str)
                if not uncacheable:
                    self._cache.put(op_str, target_str, cache_engine, root_str, result, freshness)
                return _mark_reindexing(result, reindexing)

            # Single-engine dispatch (`uncacheable`, computed above, also guards this path).
            cached_result = (
                None if uncacheable
                else self._cache.get(op_str, target_str, cache_engine, root_str, freshness)
            )
            if cached_result is not None:
                # The staleness marker belongs on EVERY exit, and a cache hit is the exit that
                # needs it most: the cache key's freshness generation only advances when a reindex
                # COMPLETES, so a hit taken while one is in flight is precisely the "answer from
                # the previous index" case the marker exists to disclose. It was applied on the
                # single fresh-dispatch path only, so the three paths that could actually serve a
                # stale answer were the three that stayed silent about it.
                return _mark_reindexing({**cached_result, "cached": True}, reindexing)
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

            result = self._cross_check_name_resolved(
                result, op_str, target_str, budget, project_root, was_auto)

            if not uncacheable:
                self._cache.put(op_str, target_str, cache_engine, root_str, result, freshness)
            return _mark_reindexing(result, reindexing)

        except Exception as exc:
            log_swallowed("Gateway.query", exc)
            return safe_null_result(op or "", target or "", reason="gateway-error")
