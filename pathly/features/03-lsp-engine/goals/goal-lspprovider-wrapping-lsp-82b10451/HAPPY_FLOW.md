# HAPPY_FLOW — LspProvider (F3 LSP Engine Adapter)

---

## Phase 1 — Core LspProvider creation

1. Builder creates `src/codeintel/providers/lsp.py`.
2. `LspProvider.__init__()` calls `_detect_backend()` → `shutil.which("uvx")` finds the binary → `self.available = True`.
3. Gateway calls `provider.build_result("symbol", "parse_result", [], 0, "/my/repo")`.
4. `_get_or_create_session("/my/repo")` finds no existing session → creates a new `_LspSession(state=WARMING)` → starts daemon thread that runs the asyncio loop.
5. `build_result` returns immediately: `{ok:True, result:None, engine:"lsp", reason:"warming"}`.
6. Background thread: asyncio loop connects to `uvx serena` via stdio → MCP handshake → session READY.
7. Agent retries `build_result("symbol", "parse_result", ...)`:
   - Session found, state=READY.
   - `asyncio.run_coroutine_threadsafe(call_tool("find_symbol", ...), loop).result(timeout=5)`.
   - Returns `{ok:True, result:"## Symbol: parse_result\ndef …\n\n## References\n…", engine:"lsp", cached:False}`.

## Phase 2 — Server wiring

1. Builder adds `LspProvider` import to `server.py`.
2. `_build_providers()` instantiates `LspProvider()`, checks `lp.available`, appends to chain.
3. `code_status_handler({})` now returns `{ok:True, engines:["graph","lsp","none"], ...}`.
4. Agent calls `code.status` → sees `"lsp"` → knows it can use `engine=lsp`.

## Phase 3 — Tests pass

1. `pytest tests/test_lsp_provider.py` discovers 10 test groups.
2. All monkeypatched paths exercise safe-null returns.
3. Mocked READY path exercises real dispatch (no live subprocess needed).
4. Exit 0 — feature complete.
