# Plan Architecture — F4: Unified Gateway (Engine Selector)

## Design Decisions

### D1: Typed provider slots over a dynamic list

**Decision:** `Gateway.__init__(graph, lsp, semantic)` — three named slots instead of the current `providers: list`.

**Why:** F4 needs to dispatch by engine name; a named slot makes the routing table unambiguous and removes the need to probe provider types at dispatch time. It also makes `code.status` trivial (read `.available` from each slot).

**Trade-off:** Less dynamic — a fourth engine (hypothetical v2) requires a new slot. Acceptable: the spec defines exactly three engines; the provider protocol remains the extensibility seam.

### D2: `concurrent.futures.ThreadPoolExecutor` for fan-out

**Decision:** Fan-out to multiple providers using a thread pool (max 3 workers), not `asyncio`.

**Why:** The providers' `build_result` methods are synchronous (graph uses subprocess; lsp uses `asyncio.run_coroutine_threadsafe`). Mixing sync and async correctly is error-prone. A thread pool is the straightforward, safe choice.

**Trade-off:** Thread overhead vs. async. For fan-out of at most 3 providers with bounded latency (budget_ms cap), the overhead is negligible. If providers become fully async in a future feature, revisit.

### D3: Content-hash cache keyed by (op, target, engine, project_root)

**Decision:** Cache key includes both the logical query params and the content hash of the target (computed separately). The content hash is the freshness signal — not a TTL.

**Why:** TTL-based caches require choosing an expiry window, which either misses edits (too long) or thrashes on stable files (too short). Content-hash is exact: a file that hasn't changed won't trigger a re-query, and one that has changed will always miss.

**Trade-off:** Requires reading the file on every query to compute the hash. For small source files this is cheap. If target is a function name (not a file path), the hash is `sha256(target)` — stable, so the entry is effectively permanent until the key changes.

### D4: TieringPolicy is immutable after construction

**Decision:** `TieringPolicy` copies `rules` at init; no runtime mutation.

**Why:** Thread safety without locks. The policy is a read-only lookup table. If rules need to change, construct a new policy and pass it to a new Gateway (v1 restart model).

**Trade-off:** Cannot hot-reload policy rules without a server restart. Acceptable for v1; the spec says tiering is "off by default" — it is an advanced feature used by harnesses, not live-mutated.

### D5: Tiering check before cache lookup

**Decision:** The gateway's `query()` checks `TieringPolicy.is_allowed()` before checking the cache.

**Why:** Prevents caching a policy-allowed response that a later policy change would block. If the check were after the cache, a cached result from a permissive configuration could be served after the policy is tightened. Since policy is immutable per-gateway-instance, this is defensive.

**Trade-off:** A policy-blocked op always pays the check cost (negligible), never serves from cache.

---

## Phase Mapping

### Phase 1 (SemanticProvider placeholder)
Adds `providers/semantic.py`. No gateway changes. Dependency for Phase 2.

### Phase 2 (Engine-aware gateway router)
Rewrites `gateway.py`: new `__init__` signature, `_AUTO_ENGINE` table, single-engine dispatch. Fan-out stubs fall through to NoneProvider (placeholder until Phase 4).

### Phase 3 (Wire providers into server)
Updates `server.py`: `_build_gateway()` replaces `_build_providers()`, all three providers registered, `engine` param threaded through.

### Phase 4 (Fan-out merge)
Extends `gateway.py`: `_fan_out()` and `_merge()` helpers; `engine=both/all` handled; `engine=auto` for `op=context` resolves to `both`.

### Phase 5 (Content-hash cache)
New `cache.py`: `ContentHashCache` class, thread-safe, file-content-hash freshness signal.

### Phase 6 (Wire cache)
Extends `gateway.py`: cache lookup before dispatch, cache store after dispatch.

### Phase 7 (TieringPolicy)
New `policy.py`: `TieringPolicy` class, immutable, `is_allowed()` lookup.

### Phase 8 (Wire tiering)
Extends `gateway.py` (early-exit check) and `server.py` (role param added to `code.query`).

### Phase 9 (Gateway tests)
New `tests/test_gateway.py`: 13 test cases covering all F4 behaviours.
