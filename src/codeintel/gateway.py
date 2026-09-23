from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from codeintel.cache import ContentHashCache
from codeintel.policy import TieringPolicy
from codeintel.provider import Result, attach_confidence, log_swallowed, safe_null_result
from codeintel.providers.none import NoneProvider
from codeintel.query_ops import OPS_REQUIRING_A_TARGET, TARGETLESS_OPS
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


def _unattached_hint(engine: str) -> str | None:
    """The remediation for an engine that is not attached to this gateway at all.

    ``server._build_gateway`` deliberately leaves a slot None when its backend is absent, so that
    ``adopt_provider`` can fill it later once ``doctor`` finds one — and that drops the provider
    instance carrying ``unavailable_hint`` with it. The text belongs to the provider module, so
    read it off the class rather than keeping a second copy here that would drift out of step with
    the one ``doctor`` prints. Lazy and never-raise: these imports pull in optional third-party
    dependencies, which is the very condition being reported on.
    """
    try:
        if engine == "graph":
            from codeintel.providers.graph import GraphProvider
            return GraphProvider.unavailable_hint
        if engine == "lsp":
            from codeintel.providers.lsp import LspProvider
            return LspProvider.unavailable_hint
        if engine == "semantic":
            from codeintel.providers.semantic import SemanticProvider
            return SemanticProvider.unavailable_hint
    except Exception as exc:  # pragma: no cover — an import failure here is itself the diagnosis
        log_swallowed("gateway._unattached_hint", exc)
    return None


def _is_retryable(result: Result) -> bool:
    return bool(result.get("retry_after_s")) or any(
        isinstance(gap, dict) and bool(gap.get("retry_after_s"))
        for gap in (result.get("gaps") or [])
    )


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
        """Ask the LSP when the graph's caller answer carries a known binding failure.

        This closes the routing gap that let the worst failure through. `_AUTO_ENGINE` is a static
        op→engine map, not a cascade: `callers` goes to the graph and, uniquely among the ops, has
        no fallback of any kind. So when the graph answered `callers describe` with 32 rows that were
        every vitest `describe()` call in the repository — bound to the project's own
        `domain.budget.describe` because it was the only indexed symbol with that name — the LSP,
        which had the correct answer sitting in its reference index, was never asked. One engine
        being confidently wrong is a bug; no second engine ever being consulted is the design that
        let it reach the caller.

        Deliberately narrow. It fires when every graph row was name-resolved, or when a caller used
        a file hint and the graph found same-named symbols but no edge for that exact definition.
        The latter catches property calls that the TypeScript graph resolver failed to bind, while
        avoiding an LSP call for ordinary healthy answers. It APPENDS rather than replaces: the LSP
        answers a related but different question (references, not call edges), so presenting its
        list as the graph's would substitute one over-claim for another. And it never fires for an
        explicitly pinned `--engine graph`, where the caller has said which engine they want."""
        try:
            if not was_auto or op not in self._CROSS_CHECKED_OPS:
                return result
            if result.get("result") is None or self.lsp is None:
                return result
            gaps = result.get("gaps") or []
            all_name_resolved = any(
                isinstance(gap, dict)
                and gap.get("kind") == "all-rows-name-resolved"
                and gap.get("section") == "callers"
                for gap in gaps
            )
            exact_target_unbound = any(
                isinstance(gap, dict)
                and gap.get("kind") == "target-hint-unmatched"
                and gap.get("section") == "callers"
                for gap in gaps
            )
            # A file-qualified target is supposed to identify one definition. If its
            # answer still contains low-confidence name matches, the backend has selected the
            # destination node but has not proven the incoming property-call bindings. This is the
            # mixed variant of ``all-rows-name-resolved``: a handful of structural rows can mask a
            # much larger suffix-match population, so the old all-or-nothing signature missed it.
            # Only auto mode reaches this method, and only an already-partial answer carrying an
            # exact file hint pays for the LSP check; healthy graph answers still make no extra
            # engine call. A dotted target without a file is intentionally not escalated because
            # module identity is represented differently across Serena language servers.
            caller_edges_unverified = any(
                isinstance(gap, dict)
                and gap.get("kind") == "low-confidence-edges"
                and gap.get("section") == "callers"
                for gap in gaps
            )
            _, has_at, hinted_path = target.rpartition("@")
            exact_file_hint = bool(has_at and "/" in hinted_path.replace("\\", "/"))
            narrowed_target_unverified = caller_edges_unverified and exact_file_hint
            if not (all_name_resolved or exact_target_unbound or narrowed_target_unverified):
                return result
            graph_problem = (
                "every graph row was resolved by name"
                if all_name_resolved
                else "the graph found no caller edge for the file-qualified symbol"
                if exact_target_unbound
                else "the qualified graph answer contains unverified name-resolved caller edges"
            )
            probe = self._dispatch_single(
                self.lsp, "symbol", target, budget, project_root, "lsp")
            body = probe.get("result")
            probe_gaps = probe.get("gaps") or []
            reference_gap = next((
                gap for gap in probe_gaps
                if isinstance(gap, dict) and gap.get("section") == "references"
            ), None)
            if not body or reference_gap is not None:
                # Silence from the LSP is not agreement. Say which check did not happen — and
                # separate "not yet booted" from "had nothing", because only the first is fixed by
                # asking again. A one-shot CLI process meets a cold serena on every invocation; the
                # long-lived MCP server keeps the session warm and takes this branch once at most.
                # Waiting here is deliberately NOT done: it would hold back a graph answer that is
                # already complete, to append a section that is only advisory.
                unavailable_reason = (
                    str(reference_gap.get("kind") or "not-asked")
                    if reference_gap is not None else str(probe.get("reason") or "no-result")
                )
                retry_after_s = probe.get("retry_after_s")
                retryable = unavailable_reason in {"warming", "timeout"} or bool(retry_after_s)
                why = (
                    "the language server had not finished booting"
                    if unavailable_reason == "warming"
                    else str(reference_gap.get("detail") or "the reference lookup did not answer")
                    if reference_gap is not None
                    else "the LSP engine reported nothing for this symbol"
                )
                nxt = (" Ask again once it is warm and this section will be filled in."
                       if retryable else
                       " Check it yourself with `--engine lsp --op symbol`.")
                return self._restamp(result, [*gaps, {
                    "section": op, "kind": "cross-check-unavailable",
                    "engine": "lsp",
                    "reason": unavailable_reason,
                    "detail": f"{graph_problem} and the LSP could not provide an independent "
                              f"reference check ({why}), so the graph answer remains unverified"
                              + (" — retry" if retryable else ""),
                    **({"retry_after_s": retry_after_s or 2} if retryable else {}),
                }], str(result["result"]) + (
                    f"\n\n> Cross-check unavailable: {graph_problem}, and "
                    f"{why}, so nothing here has been confirmed against a second engine.{nxt}"
                ))
            refs = self._reference_lines(str(body))
            listing = "\n".join(refs[: self._CROSS_CHECK_REF_CAP]) or "(no references reported)"
            more = (f"\n… (+{len(refs) - self._CROSS_CHECK_REF_CAP} more)"
                    if len(refs) > self._CROSS_CHECK_REF_CAP else "")
            merged = (
                f"{result['result']}\n\n## Cross-check — LSP references to `{target}` "
                f"({len(refs)})\n"
                f"_The graph answer has a known binding gap: {graph_problem}. These locations come "
                f"from the language server, which resolves the exact definition. A caller listed "
                f"above but absent here is likely a name collision; a location here but missing "
                f"above is a reference the graph could not bind._\n" + listing + more
            )
            return self._restamp(result, [*gaps, {
                "section": op, "kind": "cross-checked-with-lsp",
                "engine": "lsp",
                "detail": f"{graph_problem}, so the LSP was consulted "
                          f"independently and reported {len(refs)} reference location(s); the two "
                          f"lists answer related but different questions and are shown separately",
            }], merged)
        except Exception as exc:
            log_swallowed("Gateway._cross_check_name_resolved", exc)
            return result

    @staticmethod
    def _restamp(envelope: Result, gaps: list[dict[str, Any]], body: str) -> Result:
        """Rebuild an envelope whose gaps or body changed, through the one function that stamps it.

        `confidence`, `outcome` and `evidence_class` are derived from `gaps` — by
        `attach_confidence`, and nowhere else. The cross-check used to append its gap with
        `{**result, "gaps": [...]}`, which copies the old stamp beside the new gap list: correct
        only because every trigger today implies the graph answer was already `partial`. The first
        trigger that fires on a `complete` answer would have produced `confidence: complete` with a
        gap in the same envelope, and possibly `evidence_class: evidence` — the value an agent is
        told to require before deleting. Re-deriving costs nothing and removes the precondition."""
        return attach_confidence({**envelope, "result": body}, gaps)

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
            # Providers have already classified their reason into the public outcome taxonomy.
            # Repeating a private reason allow-list here guaranteed drift: the first new reason,
            # ``source-unreadable``, was correctly marked unavailable by LSP and then collapsed
            # back to ``not_found`` by this merge.  Failed and unavailable both mean no engine was
            # able to answer; an actual miss is the explicit ``not_found`` outcome.
            # Older/custom providers may not have adopted ``outcome`` yet. Route their reason
            # through the SAME central classifier rather than reintroducing a local allow-list.
            classified = [
                r.get("outcome") or safe_null_result(
                    op_str, target_str, engine=eng, reason=reasons[eng]
                ).get("outcome")
                for eng, r in results.items()
            ]
            all_unreachable = bool(classified) and all(
                outcome in ("unavailable", "failed") for outcome in classified
            )
            summary = "engines-unavailable" if all_unreachable else "no-result"
            detail = ", ".join(f"{eng}: {why}" for eng, why in sorted(reasons.items()))
            merged_null = safe_null_result(
                op_str, target_str, engine=engine_str, reason=summary,
                hint=(f"no engine produced an answer — {detail}"
                      + ("; this is NOT evidence the target does not exist"
                         if all_unreachable else "")),
            )
            retry_after = max(
                (float(r.get("retry_after_s") or 0) for r in results.values()), default=0
            )
            if retry_after:
                merged_null["retry_after_s"] = retry_after
            return merged_null

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
                    **({"retry_after_s": r["retry_after_s"]}
                       if r.get("retry_after_s") else {}),
                })
        # `rows` and `evidence` are deliberately NOT merged in, and this is the decision rather than
        # the oversight the paragraph above records having shipped once already. `evidence.returned`
        # means "the rows this body printed", and a fan-out body is two engines' bodies concatenated
        # — the graph half's rows under a heading the lsp half also prints `- ` lines beneath. A
        # merged `rows` would be a subset of the answer's rows presented as the answer's rows, which
        # is the aggregate defect this repository keeps finding, arriving through the field added to
        # prevent it. An agent that wants structured rows asks the op that produces them
        # (`callers`, `callees`, `impact`) rather than a fan-out that quotes it.
        # Pinned by test_a_fanout_answer_claims_no_structured_rows.
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
        # Both arms short-circuit BEFORE `build_result`, so the provider's own unavailable-hint is
        # never reached from here — which is exactly how the documented contract ("safe-nulls with
        # a reason AND a hint", README) came to be broken on the first call the Quickstart tells a
        # new user to make. The provider supplies the text; this stays generic so a fourth engine
        # inherits the behaviour by declaring the attribute.
        if provider is None:
            return safe_null_result(
                op_str, target_str, engine=engine_str, reason="engine-unavailable",
                hint=_unattached_hint(engine_str)
                or f"the {engine_str} engine is not attached to this gateway — run "
                   f"`codeintel doctor` to see which engines are available for this repo",
            )
        if not getattr(provider, "available", True):
            return safe_null_result(
                op_str, target_str, engine=engine_str, reason="engine-unavailable",
                hint=getattr(provider, "unavailable_hint", None)
                or f"the {engine_str} engine is not available — run `codeintel doctor` for why",
            )
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

            # A blank `target` is a question that was never asked, and answering it as though it
            # were one is how this tool told an evaluator their code was missing for a whole
            # session. Routed on into the graph lookup it came back `reason: "not-in-graph"` with
            # the hint "`` is not in the graph index for this project — if you just added or
            # renamed it, refresh with: codeintel index <root>": empty backticks, and a
            # remediation that cannot change the answer, because there was nothing to look up.
            #
            # How the target came to be blank is the part worth naming in the hint. The parameter
            # is `target`; they passed the symbol as `q`, and MCP builds its argument model from
            # the tool signature with pydantic's default `extra="ignore"`, so the unknown key was
            # dropped without a word and `target` took its empty default. Every query for the rest
            # of that session reported a symbol missing from a perfectly healthy index. They
            # re-indexed, concluded the index was broken, and finished the job by going around
            # this tool to the raw graph backend — which has none of the cross-language collision
            # filtering `graph_answer._drop_edge_collisions` applies here, and duly handed them
            # `.tsx` files "calling" a Python method. Losing `code.query` does not degrade to
            # slower; it degrades to confidently wrong. So the hint names the parameter.
            #
            # `unavailable`, never `not_found` (see `provider.safe_null_result`): "you did not name
            # a symbol" and "that symbol does not exist" license opposite next actions, and
            # collapsing them is the precise ambiguity `outcome` was added to remove. It sits
            # beside `no-project-root`, which is the same kind of miss on the other argument.
            #
            # Checked HERE rather than in `server.code_query_handler` because the CLI calls this
            # method directly (`commands/query.py`), so a server-layer guard would leave that
            # transport still answering "not in the graph index" for a missing argument. After the
            # policy check, so a denied role learns nothing about which arguments would have been
            # well-formed; before `maybe_reindex`, so a malformed call triggers no indexing work.
            if op_str in OPS_REQUIRING_A_TARGET and not target_str.strip():
                return safe_null_result(
                    op_str, target_str, reason="no-target",
                    hint=f"`{op_str}` needs a symbol to ask about and none was given — this is "
                         f"not a statement about your code, and re-indexing will not change it. "
                         f"Pass it as `target` (e.g. `target=\"my_function\"`). If you spelled "
                         f"that argument something else — `q`, `name`, `symbol`, `query` — it was "
                         f"silently dropped as unknown, which is why the answer came back empty. "
                         f"The ops that need no target: {', '.join(sorted(TARGETLESS_OPS))}.",
                )

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
                if not uncacheable and not _is_retryable(result):
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

            if not uncacheable and not _is_retryable(result):
                self._cache.put(op_str, target_str, cache_engine, root_str, result, freshness)
            return _mark_reindexing(result, reindexing)

        except Exception as exc:
            log_swallowed("Gateway.query", exc)
            return safe_null_result(op or "", target or "", reason="gateway-error")
