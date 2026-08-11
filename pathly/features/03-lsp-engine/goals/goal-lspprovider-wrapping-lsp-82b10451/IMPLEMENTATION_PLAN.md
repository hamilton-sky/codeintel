# IMPLEMENTATION_PLAN — LspProvider (F3 LSP Engine Adapter)

Goal: LspProvider wrapping the LSP-over-MCP bridge — always-fresh symbol/overview with async warm-up.

Rigor: standard · 3 phases · 2 conversations

---

## Conversation 1 — Core LspProvider

Scope: Create `src/codeintel/providers/lsp.py`. Do NOT touch `server.py` or any test files yet.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout src/codeintel/providers/lsp.py` and retry.

---

### Phase 1 — Create LspProvider with session state machine

**File:** `src/codeintel/providers/lsp.py`

**Purpose:** Implements the `CodeProvider` protocol for the LSP engine: detects the bridge binary, manages one `_LspSession` per `project_root`, starts async warm-up on first call, dispatches `op=symbol` and `op=overview` when READY, and never raises under any condition.

**Depends on:** `CodeProvider` protocol in `src/codeintel/provider.py` (already exists). F1 skeleton complete.

**Enables:** Gateway can pick up LspProvider as a drop-in provider (Story 1–6). Phase 2 wires it into the server.

**Architecture notes:**
- `_State` enum: `WARMING | READY | FAILED`
- `_LspSession(project_root)`: holds `state`, `cooldown_until`, `_lock` (threading.Lock), `_loop` (asyncio event loop running in daemon thread), `_mcp_session` (ClientSession or None)
- `LspProvider._sessions: dict[str, _LspSession]` — one entry per project_root
- `_detect_backend()`: `shutil.which("uvx")` primary; `shutil.which("serena")` fallback; sets `self.available`
- **Warm-up**: first `build_result` call for a root creates a `_LspSession`, sets state=WARMING, starts a daemon `threading.Thread` that runs `asyncio.new_event_loop()` + connects to `uvx serena --project_root <root>` via `mcp.client.stdio.stdio_client` + `mcp.ClientSession`. On success → state=READY; on exception → state=FAILED, `cooldown_until = time.monotonic() + 60`.
- **Cooldown re-try**: if FAILED and `time.monotonic() > cooldown_until`, delete the session entry so next call triggers a fresh warm-up.
- **op dispatch (sync bridge)**: use `asyncio.run_coroutine_threadsafe(coro, session._loop).result(timeout)` to call from sync `build_result`. Cap at `budget` ms or 5000 ms.
- `op=symbol`: call `session._mcp_session.call_tool("find_symbol", {"name": target, "project_root": root})` + `call_tool("find_referencing_symbols", {...})`, format result as `## Symbol: <target>\n<def>\n\n## References\n<refs>`.
- `op=overview`: call `session._mcp_session.call_tool("get_symbols_overview", {"relative_path": target or "", "project_root": root})`.
- Unsupported op → `safe_null_result(..., reason="unsupported-op")`.
- Every code path ends in `ok=True`; the outermost `try/except Exception` catches anything that slips through.

**Done when:** A unit test instantiating `LspProvider()` with a mocked unavailable backend returns `ok=True, result=None, reason="engine-unavailable"`. A second test mocking the session state as WARMING returns `ok=True, result=None, reason="warming"`.

**Verify:**
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "from codeintel.providers.lsp import LspProvider; p = LspProvider(); print(p.available)"
```

---

## Conversation 2 — Wire + Tests

Scope: Update `server.py` to include `LspProvider`, and write `tests/test_lsp_provider.py`. Reference Conversation 1 output: `src/codeintel/providers/lsp.py` must exist and import cleanly.

Recovery: If verification fails and the fix requires out-of-scope changes, stop and report.
If fundamentally broken, rollback with `git checkout src/codeintel/server.py tests/test_lsp_provider.py` and retry.

---

### Phase 2 — Wire LspProvider into server

**File:** `src/codeintel/server.py`

**Purpose:** Makes `LspProvider` a live provider in the gateway chain and surfaces LSP availability in `code.status`.

**Depends on:** Phase 1 complete (`src/codeintel/providers/lsp.py` importable).

**Enables:** The MCP `code.query` tool can now route `engine=lsp` to `LspProvider`. `code.status` tells agents when lsp is ready (Story 7, AC7.1–7.2).

**Changes:**
- Add `from codeintel.providers.lsp import LspProvider` import.
- In `_build_providers()`:
  ```python
  try:
      lp = LspProvider()
      if lp.available:
          providers.append(lp)
  except Exception:
      pass
  ```
  Insert before `NoneProvider` so the chain is `[GraphProvider?, LspProvider?, NoneProvider]`.
- In `code_status_handler()`: probe `LspProvider().available` (with try/except); add `"lsp"` to `engines` list when true.
- Do NOT touch any other part of `server.py`.

**Done when:** `from codeintel.server import code_status_handler; r = code_status_handler({})` runs without error and `r["ok"] is True`.

**Verify:**
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
python3 -c "from codeintel.server import code_status_handler; r = code_status_handler({}); print(r)"
```

---

### Phase 3 — Test suite for LspProvider

**File:** `tests/test_lsp_provider.py`

**Purpose:** Proves the never-raise invariant and state-machine correctness for `LspProvider`, mirroring the test structure of `test_graph_provider.py`.

**Depends on:** Phase 1 + Phase 2 complete.

**Enables:** CI can gate on `pytest tests/test_lsp_provider.py`. Story 5 (AC5.1–5.4), Story 2 (AC2.1–2.2), Story 3 (AC3.1–3.3), Story 7 (AC7.1–7.2) are verified.

**Test groups:**
1. Never-raise: `None` args → `ok=True`.
2. Never-raise: wrong types → `ok=True`.
3. Backend unavailable (monkeypatch `shutil.which` → `None`) → `reason="engine-unavailable"`.
4. Warming state (monkeypatch session state to WARMING before calling `build_result`) → `reason="warming"`, `result=None`.
5. Failed/cooldown state (monkeypatch session state to FAILED, `cooldown_until` in future) → `reason="boot-failed"`, `result=None`.
6. Cooldown expiry (FAILED + `cooldown_until` in the past): next call should trigger a new warm-up; session state resets to WARMING.
7. Ready state with mocked MCP call → `ok=True`, `engine="lsp"`, non-null result.
8. Unsupported op when READY → `reason="unsupported-op"`.
9. Server status with mocked available lsp → `"lsp"` in `engines`.
10. Server status with unavailable lsp → `"lsp"` NOT in `engines`.

**Done when:** `pytest tests/test_lsp_provider.py -v` exits 0 with all 10 groups green.

**Verify:**
```bash
cd /Users/shammaihamilton/Documents/project/codeintel
pytest tests/test_lsp_provider.py -v
```
