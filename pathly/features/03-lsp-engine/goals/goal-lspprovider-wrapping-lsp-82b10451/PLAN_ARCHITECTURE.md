# PLAN_ARCHITECTURE — LspProvider (F3 LSP Engine Adapter)

This file covers design decisions scoped to F3. For overall project architecture see `pathly/project/SPEC.md §5`.

---

## Key decisions

### D1 — Threading model: daemon thread + asyncio event loop per session

`build_result` is synchronous (the `CodeProvider` protocol is sync). The MCP SDK (`mcp.ClientSession`) is async. The bridge:

- Each `_LspSession` owns one daemon `threading.Thread` running `asyncio.new_event_loop()` for its lifetime.
- Warm-up runs on that loop. Once READY, query coroutines are submitted via `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)`.
- This is the only approach that keeps `build_result` non-blocking during warm-up and avoids nesting asyncio loops.

**Trade-off:** One thread per active project_root. Acceptable — typically one project at a time. Daemon threads are GC-collectible when the session is dropped.

### D2 — Backend detection: uvx primary, serena binary fallback

`shutil.which("uvx")` → launch `uvx serena`. If absent, `shutil.which("serena")` → launch directly. If neither → `available=False`. This mirrors how `GraphProvider` detects `codebase-memory-mcp`.

**Deferred:** If neither binary is available at init time, the provider is still instantiated (for testability) but `build_result` returns `engine-unavailable` immediately.

### D3 — Session key: str(project_root)

One `_LspSession` per `str(project_root)` — same string → same session. Switching root creates a new entry; old session is not torn down explicitly (language-server resource management is the bridge's responsibility). This matches the project SPEC: "Switching project_root tears down the old session" means the old root's session falls out of use, not that we send an explicit shutdown.

**If an explicit teardown is needed later:** add `session._loop.call_soon_threadsafe(session._mcp_session.aclose)` before popping. Deferred to F4/F7 if the language server leaks resources.

### D4 — op surface: symbol + overview only

`LspProvider` handles `op=symbol` (definition + references) and `op=overview` (symbols overview). All other ops return `reason="unsupported-op"`. This is intentional: the graph engine covers `impact/callers/callees/chain/pattern`; LSP covers precision/freshness. The gateway's op routing will evolve in F4.

### D5 — Cooldown: 60 seconds, hard-coded

On boot failure, the session is marked FAILED with `cooldown_until = time.monotonic() + 60`. This value is not yet configurable. F7 will expose it in `.codeintel.toml`. A 60-second default prevents thrashing while allowing recovery.

---

## Phase Mapping

### Phase 1 — lsp.py
Covers D1 (thread model), D2 (detection), D3 (session key), D4 (op surface), D5 (cooldown).
All decisions implemented here.

### Phase 2 — server.py
No new decisions. Wires Phase 1 output into the existing gateway + status handler patterns established in F1/F2.

### Phase 3 — tests
Validates D4 (op surface), D5 (cooldown), and the never-raise invariant. No architectural decisions here — tests verify the decisions above.
