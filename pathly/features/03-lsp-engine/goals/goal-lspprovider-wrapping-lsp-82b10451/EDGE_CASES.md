# EDGE_CASES — LspProvider (F3 LSP Engine Adapter)

---

## Phase 1 — Core LspProvider

### EC1.1 — Backend binary absent

- **Trigger:** `shutil.which("uvx")` and `shutil.which("serena")` both return `None`.
- **Expected:** `build_result(...)` returns `ok=True, result=None, reason="engine-unavailable"`. No thread is started.
- **Guard:** `_detect_backend()` sets `self.available = False`; `build_result` short-circuits before touching sessions.

### EC1.2 — `None` or wrong-type arguments to `build_result`

- **Trigger:** Caller passes `None` for `op`, `target`, `project_root`; or passes an object instead of a string.
- **Expected:** `ok=True, result=None` (no crash, no AttributeError).
- **Guard:** Outermost `try/except Exception` in `build_result`; also coerce `str(op or "")` before any dispatch.

### EC1.3 — LSP bridge process dies during warm-up

- **Trigger:** `uvx serena` exits non-zero or the stdio pipe closes before the MCP handshake completes.
- **Expected:** Background thread catches the exception → session transitions to `FAILED`, `cooldown_until = time.monotonic() + 60`.
- **Guard:** The warm-up thread wraps its body in `try/except Exception`; all errors set FAILED + cooldown.

### EC1.4 — LSP bridge takes >5 s to warm up

- **Trigger:** Language server is slow (large repo, JVM warm-up, etc.).
- **Expected:** First N calls during warm-up return `reason="warming"` (never blocked). After warm-up completes, next call returns real data.
- **Guard:** No timeout on the warm-up thread itself — it waits as long as needed. Per-request timeout only applies when dispatching to a READY session.

### EC1.5 — Cooldown expiry and re-try

- **Trigger:** Session in FAILED state, `cooldown_until` has passed.
- **Expected:** `build_result` deletes the FAILED session entry; next call creates a fresh `_LspSession` → WARMING.
- **Guard:** Check `time.monotonic() > session.cooldown_until` before returning `boot-failed`; if expired, pop from `_sessions`.

### EC1.6 — Unsupported op when READY

- **Trigger:** Agent calls `build_result("callers", ...)` — an op LspProvider doesn't handle.
- **Expected:** `ok=True, result=None, reason="unsupported-op"`.
- **Guard:** `_dispatch` returns `None` for unknown ops; `build_result` maps `None` → safe-null with reason.

### EC1.7 — `asyncio.run_coroutine_threadsafe` timeout

- **Trigger:** MCP call to the language server hangs past the budget/5000 ms deadline.
- **Expected:** `concurrent.futures.TimeoutError` is caught; returns safe-null with `reason="timeout"`.
- **Guard:** `.result(timeout=timeout_s)` call wrapped in `try/except`; never blocks indefinitely.

### EC1.8 — Two concurrent calls for the same project_root during warm-up

- **Trigger:** Two threads call `build_result` with the same root before READY.
- **Expected:** Only one `_LspSession` is created; both callers get `reason="warming"`.
- **Guard:** Session creation uses `_sessions.setdefault(root, _LspSession(...))` inside a module-level lock, or the second `setdefault` is a no-op.

### EC1.9 — project_root switching

- **Trigger:** Agent switches `project_root` from `/repo-a` to `/repo-b`.
- **Expected:** A new `_LspSession` is created for `/repo-b`; the old session for `/repo-a` persists independently in the dict.
- **Guard:** `_sessions` is keyed by root string; switching root never modifies the old entry.

---

## Phase 2 — Server wiring

### EC2.1 — LspProvider import fails at server startup

- **Trigger:** `from codeintel.providers.lsp import LspProvider` raises (syntax error, missing dep).
- **Expected:** `_build_providers()` catches the exception and falls back to `[NoneProvider()]`; server still starts.
- **Guard:** Outer `try/except Exception` in `_build_providers()` wraps the `LspProvider` probe.

### EC2.2 — `code_status_handler` with broken LspProvider

- **Trigger:** `LspProvider()` raises in `code_status_handler`.
- **Expected:** `"lsp"` is absent from `engines`; status still returns `ok=True`.
- **Guard:** `try/except` around the `LspProvider()` probe inside `code_status_handler`.

---

## Phase 3 — Tests

### EC3.1 — Test isolation: no live subprocess

- **Trigger:** Any test that instantiates `LspProvider`.
- **Expected:** Tests never launch a real `uvx serena` subprocess (too slow/fragile in CI).
- **Guard:** Monkeypatch `shutil.which` → `None` in unavailable tests; for READY state tests, inject a mocked `_LspSession` directly rather than triggering real warm-up.
