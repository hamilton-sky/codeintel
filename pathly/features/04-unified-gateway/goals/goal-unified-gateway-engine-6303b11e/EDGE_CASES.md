# Edge Cases — F4: Unified Gateway (Engine Selector)

## Phase 1 — SemanticProvider placeholder

- **Import fails:** If `providers/semantic.py` has a syntax error, `server.py` import blows up. Guard: run `python -c "from codeintel.providers.semantic import SemanticProvider"` before moving on.
- **Protocol mismatch:** If `SemanticProvider` doesn't implement `build_result` with the exact `CodeProvider` signature, runtime `isinstance` check will fail. Guard: add a `@runtime_checkable` check in the test.

## Phase 2 — Engine-aware gateway router

- **Unknown engine string:** `engine="foo"` — gateway must not raise; return safe-null with `reason="unknown-engine"`.
- **`engine=""` (empty string):** Treat as `auto`; same as `None`.
- **`engine="auto"` with unknown op:** No entry in `_AUTO_ENGINE` — fall through to NoneProvider safe-null with `reason="unsupported-op"`.
- **`engine="overview"` auto-selects graph, graph unavailable:** The auto-fallback to lsp for `overview` must be silent — no exception, just try lsp next.
- **Provider raises despite never-raise contract:** Outer try/except in `query()` must catch and return `reason="gateway-error"`.
- **`engine="graph"` but GraphProvider.available = False:** Return safe-null with `reason="engine-unavailable"`, not a generic error.

## Phase 3 — Wire all providers into server

- **`_build_gateway()` called when no backends installed:** All three providers created; all have `available=False`; gateway still returns safe-null — never breaks the server startup.
- **Provider constructor raises:** `try/except` in `_build_gateway()` falls back to `NoneProvider` for that slot — the server still starts.
- **`engine` not in `code.query` call:** Defaults to `""` (treated as auto) — backward compatible.

## Phase 4 — Fan-out merge in gateway

- **Both providers return null:** Merged result is safe-null — no empty sections included.
- **Thread pool timeout:** If a provider hangs beyond its deadline, the thread's result is null; the other thread's result is still used. Use `concurrent.futures` `as_completed` with per-future timeout.
- **Provider raises in thread:** Exception caught inside the thread wrapper; that slot becomes null; merge proceeds.
- **`engine="both"`, both providers unavailable:** Merged result = safe-null with `reason="engine-unavailable"`.
- **`engine="all"` with semantic unavailable:** Only graph+lsp results appear in merge (semantic null excluded from output).
- **Large merged result:** No size cap in v1 — document and leave for F6 freshness work.

## Phase 5 — Content-hash cache

- **Target is not a file path:** Hash falls back to `sha256(target.encode())` — always a stable key; no exception.
- **Target file is unreadable (permissions error):** `try/except` around `open()`; fall back to `sha256(target.encode())` — cache misses on every call until the file is readable (acceptable).
- **Target path is a directory:** `os.path.isfile()` check before `open()`; fall back to string hash.
- **Null result passed to `put`:** Silently ignored — prevents caching "no answer" as if it were valid.
- **Concurrent cache access:** `threading.Lock` around all read/write operations.
- **Cache unbounded:** Acknowledged v1 limitation; bounded by number of distinct (op, target, engine) triples queried in a session — typically small.

## Phase 6 — Wire cache into gateway

- **Cache returns stale result for a different project_root:** `project_root` is part of the cache key — separate repos never share entries.
- **Cache hit for a fan-out result:** A `both` or `all` result is cached under `engine="both"/"all"` key — a follow-up `engine="graph"` call does NOT hit that cache entry (different key).
- **Fan-out result partially cached:** Not attempted — fan-out results are stored/retrieved as a single merged unit keyed by the fan-out engine name.

## Phase 7 — Role/op tiering policy

- **`role=None` or `role=""`:** Treated as "no role" — not in rules map → all ops allowed (permissive default for unknown callers).
- **`op=None` or `op=""`:** With tiering on and a rule for the role, an empty op will fail the allowlist check → safe-null. Acceptable: empty ops are already unsupported at the gateway level.
- **rules dict mutated after init:** `TieringPolicy` copies `rules` on init to avoid external mutation.
- **enabled=True but rules={}:** All roles pass (empty map → no restrictions for any role).

## Phase 8 — Wire tiering into gateway + server

- **Tiering check happens before cache lookup:** Prevents caching the fact that an op was disallowed (which could mask a later policy change).
- **`role` not passed in `code.query` call:** Defaults to `""` → permissive — backward compatible.
- **Policy object replaced at runtime:** Not supported in v1; policy is set at `Gateway` construction time.

## Phase 9 — Gateway tests

- **Test isolation:** Each test must create its own `Gateway` instance — no shared singleton.
- **Fake providers must implement the full `CodeProvider` protocol:** Otherwise the runtime_checkable Protocol check may warn.
- **Never-raise regression:** `test_never_raise.py` fault-injection tests must still pass after gateway rewrite — run them first before the new suite.
- **Thread safety:** Fan-out tests must not rely on thread execution order; assert only on final merged content, not on per-provider call sequence.
