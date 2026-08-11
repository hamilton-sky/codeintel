# Implementation Plan — F4: Unified Gateway (Engine Selector)

Start from `FEATURE_INDEX.md` for the full touchpoint list and conversation map.

---

## Conversation 1 — Engine Router + Provider Registration

**Scope:** Rewrite gateway to honour the `engine` param; add SemanticProvider placeholder; wire all three providers into the server.
**Depends on:** F1 skeleton, F2 graph adapter, F3 LSP adapter already merged.
**Verify:** `python -m pytest tests/test_never_raise.py -q` passes; `code.query` with `engine=graph` / `engine=lsp` / `engine=semantic` all return the safe-null envelope.
**Do NOT touch:** cache, fan-out merge, tiering policy — those come in later conversations.

---

### Phase 1 — SemanticProvider placeholder

**File:** `src/codeintel/providers/semantic.py`

**Purpose:** Give the gateway a real `SemanticProvider` import target so the engine router can reference it before F5 ships the actual implementation.

**Done when:** `SemanticProvider().build_result(...)` always returns `safe_null_result(..., engine="semantic", reason="engine-unavailable")` and the file imports cleanly.

**Depends on:** `src/codeintel/provider.py` (CodeProvider protocol + safe_null_result).

**Enables:** Phase 2 engine router can import and instantiate `SemanticProvider`.

**Verify:** `python -c "from codeintel.providers.semantic import SemanticProvider; r = SemanticProvider().build_result('search','foo',[],0,''); assert r['engine']=='semantic' and r['result'] is None"`.

**Implementation notes:**
- `class SemanticProvider` implements `CodeProvider` protocol.
- `available = False` always (until F5).
- `build_result` catches all exceptions and returns safe-null — no logic needed beyond this.

---

### Phase 2 — Engine-aware gateway router

**File:** `src/codeintel/gateway.py`

**Purpose:** Replace the sequential provider chain with an engine-aware router that selects provider(s) by the `engine` argument and implements the `auto` op-to-engine table.

**Done when:** `Gateway.query(op, target, engine="graph")` routes to GraphProvider only; `engine="lsp"` routes to LspProvider only; `engine="semantic"` routes to SemanticProvider; `engine="auto"` (or None) picks by op; each unavailable engine returns safe-null with `reason=engine-unavailable`.

**Depends on:** Phase 1 (SemanticProvider import); existing GraphProvider, LspProvider, NoneProvider.

**Enables:** Phase 4 fan-out (both/all) builds on top of the router.

**Verify:** Unit tests pass with a fake provider stub that confirms routing table is correct.

**Implementation notes:**
- `Gateway.__init__(self, graph, lsp, semantic)` — three typed slots instead of a list.
- `_AUTO_ENGINE`: `dict[str, str]` — op → engine mapping:
  ```
  impact/callers/callees/chain/pattern → "graph"
  symbol                               → "lsp"
  search                               → "semantic"
  overview                             → "graph"  (single engine; fallback handled separately)
  context                              → "both"    (fan-out; Phase 4 handles the split)
  ```
- For a single-engine request: resolve to the matching provider instance; if `not available`, return `safe_null_result(..., reason="engine-unavailable")`.
- `engine=None` defaults to `"auto"`.
- Fan-out (`both`/`all`) handled in Phase 4 — for now those values fall through to the NoneProvider.
- Keep every code path wrapped in `try/except` → `safe_null_result(..., reason="gateway-error")`.

---

### Phase 3 — Wire all providers into server

**File:** `src/codeintel/server.py`

**Purpose:** Register all three providers at server startup and thread the `engine` param through `code.query`.

**Done when:** `server.py` instantiates `GraphProvider`, `LspProvider`, and `SemanticProvider`; `code.query` accepts and passes `engine` to `gateway.query()`; `code.status` lists all three engines with their availability.

**Depends on:** Phase 1 (SemanticProvider), Phase 2 (updated Gateway signature).

**Enables:** Conversations 2 and 3 build on a fully-wired server.

**Verify:** `python -c "from codeintel.server import code_query_handler; r = code_query_handler({'op':'impact','target':'foo','engine':'graph'}); assert r['ok']"`.

**Implementation notes:**
- `_build_gateway()` replaces `_build_providers()`: returns a single `Gateway(graph=..., lsp=..., semantic=...)`.
- Each provider is instantiated inside `try/except`; if it throws, use `NoneProvider()` for that slot (but this shouldn't happen given never-raise).
- `code.query` handler: add `engine: str = ""` and `role: str = ""` parameters (role forwarded later in Conv 3).
- `code.status`: call `gw.graph.available`, `gw.lsp.available`, `gw.semantic.available` to build status.

**Recovery:** If verification fails and the fix requires changes to providers, stop and report. If fundamentally broken, rollback with `git checkout` on `server.py` and retry.

---

## Conversation 2 — Fan-Out Merge + Content-Hash Cache

**Scope:** Implement `both` and `all` fan-out merge; add content-hash cache; wire cache into gateway.
**Depends on:** Conversation 1 complete and verified.
**Verify:** `python -m pytest tests/ -q` passes; a repeated query on an unchanged target returns `cached: true`.
**Do NOT touch:** tiering policy — that is Conversation 3.

---

### Phase 4 — Fan-out merge in gateway

**File:** `src/codeintel/gateway.py`

**Purpose:** Make `engine=both` query graph+lsp in parallel (thread-pool) and merge their non-null results; `engine=all` includes semantic.

**Done when:** `gateway.query(op, target, engine="both")` returns a merged result with sections from both providers (or safe-null if both fail); `engine=all` adds semantic.

**Depends on:** Phase 2 (engine router with typed provider slots).

**Enables:** Phase 6 (cache wraps fan-out results).

**Verify:** Mock graph+lsp to return distinct strings; assert merged result contains both strings with engine-labelled headers.

**Implementation notes:**
- `_fan_out(providers: list, op, target, files, budget, project_root) -> list[Result]`: calls each provider's `build_result` concurrently using `concurrent.futures.ThreadPoolExecutor(max_workers=3)`; catches all exceptions per-provider.
- `_merge(results: list[Result], engine_label: str) -> Result`: collects non-null `result` strings; prepends `## [engine_name]` to each; joins with `\n\n`; if none, returns safe-null.
- `engine="both"` → fan-out to `[self.graph, self.lsp]`.
- `engine="all"` → fan-out to `[self.graph, self.lsp, self.semantic]`.
- `engine="auto"` for `op=context` → same as `both`.
- The `engine` field in the merged result = the requested engine string (`"both"` or `"all"`).

---

### Phase 5 — Content-hash cache module

**File:** `src/codeintel/cache.py`

**Purpose:** Provide a thread-safe, in-process content-hash-keyed result cache that busts on file edits.

**Done when:** `ContentHashCache.get(op, target, engine, project_root)` returns a cached `Result` if the target file's content hash matches, or `None` if stale/absent; `ContentHashCache.put(...)` stores a result only if its `result` field is non-null.

**Depends on:** `src/codeintel/provider.py` (Result type); no other new dependencies.

**Enables:** Phase 6 wires this into gateway.

**Verify:** Write a temp file, cache a result, assert hit; write a different content, assert miss.

**Implementation notes:**
- `ContentHashCache` stores `dict[tuple, (content_hash: str, Result)]` protected by `threading.Lock`.
- Cache key: `(op, target, engine, project_root)`.
- Content hash computation: if `os.path.join(project_root, target)` is a readable file, `hashlib.sha256` of its bytes; else `hashlib.sha256(target.encode())`.
- `get`: compute current hash; if key present and hashes match, return Result; else return None.
- `put`: skip if `result["result"] is None`.
- No TTL, no size cap in v1 (single-process, bounded by queries made in session).

---

### Phase 6 — Wire cache into gateway

**File:** `src/codeintel/gateway.py`

**Purpose:** Wrap every provider dispatch (single-engine and fan-out) with the content-hash cache so repeated queries on unchanged files are served from cache.

**Done when:** A second call to `gateway.query()` with an unchanged target returns `cached: True`; editing the target file (or changing its hash in tests) causes a cache miss and a fresh provider call.

**Depends on:** Phase 4 (fan-out), Phase 5 (ContentHashCache).

**Enables:** Conversation 3 can test caching behaviour in the full gateway test suite.

**Verify:** `gateway.query(op, target, engine="graph")` twice; assert second call has `cached=True`. Then modify the mock hash; assert third call has `cached=False`.

**Implementation notes:**
- `Gateway.__init__` creates `self._cache = ContentHashCache()`.
- In `query()`, before dispatching: call `self._cache.get(op, target, engine_str, project_root)`. If hit, return the cached result with `cached=True` set.
- After dispatching (single or fan-out): call `self._cache.put(op, target, engine_str, project_root, result)`.
- Set `result["cached"] = True` only when returning from cache; provider results keep `cached=False`.

**Recovery:** If verification fails and the fix requires changes to cache key design, stop and report. If fundamentally broken, rollback with `git checkout` on `gateway.py` and retry.

---

## Conversation 3 — Role/Op Tiering + Tests

**Scope:** Add TieringPolicy module; wire role param into gateway and server; write gateway test suite.
**Depends on:** Conversation 2 complete and verified.
**Verify:** `python -m pytest tests/ -q` all green including new `test_gateway.py`.
**Do NOT touch:** semantic engine implementation — that is F5.

---

### Phase 7 — Role/op tiering policy

**File:** `src/codeintel/policy.py`

**Purpose:** Provide an opt-in policy layer that restricts which ops a given role may call; disabled by default so the standalone default is fully permissive.

**Done when:** `TieringPolicy(enabled=False).is_allowed(role, op)` always returns `True`; `TieringPolicy(enabled=True, rules={"builder":["impact","callers"]}).is_allowed("builder","symbol")` returns `False`.

**Depends on:** No new dependencies (pure Python logic).

**Enables:** Phase 8 wires this into gateway.

**Verify:** `python -c "from codeintel.policy import TieringPolicy; p=TieringPolicy(enabled=True,rules={'r':['a']}); assert p.is_allowed('r','a'); assert not p.is_allowed('r','b'); assert p.is_allowed('other','b')"`.

**Implementation notes:**
- `class TieringPolicy`:
  - `__init__(self, enabled=False, rules: dict[str, list[str]] | None = None)`.
  - `rules` maps role → list of allowed ops; if a role is absent from the map, all ops are allowed.
  - `is_allowed(self, role: str, op: str) -> bool`: returns `True` if `not self.enabled` or `role not in self.rules` or `op in self.rules[role]`.
- No external dependencies; thread-safe (immutable after init).

---

### Phase 8 — Wire tiering into gateway + server

**File:** `src/codeintel/gateway.py`, `src/codeintel/server.py`

**Purpose:** Thread the `role` parameter from the MCP tool call through the gateway, where it is checked against the tiering policy before dispatch.

**Done when:** `gateway.query(..., role="restricted_role")` returns safe-null with `reason=op-not-allowed-for-role` when tiering is on and the role is not permitted; with tiering off (default), `role` is ignored.

**Depends on:** Phase 7 (TieringPolicy).

**Enables:** Phase 9 can test full gateway behaviour including tiering.

**Verify:** Instantiate gateway with `TieringPolicy(enabled=True, rules={"r":["impact"]})`; assert `query(op="symbol", role="r")` returns `result=None, reason="op-not-allowed-for-role"`.

**Implementation notes (gateway.py):**
- `Gateway.__init__` accepts `policy: TieringPolicy | None = None`; stores as `self._policy`.
- In `query()`, early-exit: if `self._policy and not self._policy.is_allowed(role or "", op or "")` → return `safe_null_result(..., reason="op-not-allowed-for-role")`.

**Implementation notes (server.py):**
- `code.query` handler signature: add `role: str = ""`.
- Pass `role` to `gw.query()`.
- `_build_gateway()` creates `TieringPolicy(enabled=False)` and passes it; a future harness can subclass or replace.

**Recovery:** If verification fails, `git checkout` on both files and report.

---

### Phase 9 — Gateway tests

**File:** `tests/test_gateway.py`

**Purpose:** Verify engine routing, fan-out merge, cache hit/miss, and tiering — the four new behaviours added in F4.

**Done when:** `python -m pytest tests/test_gateway.py -v` passes all cases; total test count increases; no regressions in `test_never_raise.py`.

**Depends on:** Phases 1–8 all complete.

**Enables:** F4 ship-readiness; provides regression guard for F5 (semantic engine) which will update SemanticProvider.

**Verify:** `python -m pytest tests/ -q` — all green, no existing tests broken.

**Test cases to cover:**
1. `engine="graph"` → GraphProvider called; LspProvider not called.
2. `engine="lsp"` → LspProvider called; GraphProvider not called.
3. `engine="semantic"` → SemanticProvider called (returns unavailable); safe-null returned.
4. `engine="both"` → both graph+lsp called; results merged with headers.
5. `engine="both"` when graph unavailable → lsp result returned alone (no empty header).
6. `engine="all"` → three providers called; merge includes non-null results only.
7. `engine="auto"` with `op="impact"` → graph called.
8. `engine="auto"` with `op="symbol"` → lsp called.
9. Cache hit: same query twice on same content → second call `cached=True`.
10. Cache miss: same query after content change → fresh call, `cached=False`.
11. Tiering off → role param ignored, all ops allowed.
12. Tiering on → disallowed op returns `reason=op-not-allowed-for-role`.
13. Provider exception → gateway catches, returns safe-null (never-raise).

**Implementation notes:**
- Use simple stub classes for provider fakes — no mocking library needed.
- Content-hash miss: pass a file path that doesn't exist on disk (hash falls back to target string hash), then change the target string.
- Keep each test self-contained; no shared state between tests.

**Recovery:** If a test fails due to an out-of-scope issue (e.g. provider interface mismatch), stop and report. Do not change provider implementations to make tests pass — report the gap instead.
