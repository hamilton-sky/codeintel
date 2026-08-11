# F1 MCP Skeleton — Edge Cases

---

## Phase 1 — Package skeleton

### EC-1.1: `mcp` package not available under that name
- **Trigger**: `pip install mcp` fails because the package name changed or the index is stale
- **Current behavior**: `pip install -e .` fails with "no matching distribution found"
- **Expected behavior**: Builder tries `pip install "mcp[cli]"` or `pip install modelcontextprotocol`; finds the correct package name by checking the Anthropic MCP Python SDK docs
- **Handled in**: Phase 1 — Recovery note instructs the builder to try alternate package names

### EC-1.2: Python 3.11+ API used but interpreter is 3.10
- **Trigger**: `typing.NotRequired` or `|` union syntax fails on an older Python
- **Current behavior**: `ImportError` or `SyntaxError` on import
- **Expected behavior**: Use `from __future__ import annotations` at top of every file; use `typing.Optional` and `Union` fallbacks if `NotRequired` unavailable
- **Handled in**: Phase 2 — provider.py uses `from __future__ import annotations` and `typing_extensions` fallback if needed

### EC-1.3: `src/` layout not recognized by pip
- **Trigger**: `pip install -e .` installs but `import codeintel` fails (package not found)
- **Current behavior**: Python searches `sys.path` and doesn't find `codeintel` because `src/` is not on path
- **Expected behavior**: `pyproject.toml` includes `[tool.setuptools.packages.find] where = ["src"]` to tell setuptools where to look
- **Handled in**: Phase 1 — pyproject.toml explicitly sets the src layout

---

## Phase 2 — CodeProvider protocol

### EC-2.1: TypedDict `total=False` vs `NotRequired` confusion
- **Trigger**: Using `TypedDict(total=False)` makes ALL fields optional; intent is only `reason` is optional
- **Current behavior**: Type checkers complain; `ok` and `result` appear optional
- **Expected behavior**: Use `class Result(TypedDict)` with `total=True` for required fields; `reason: NotRequired[str]` for the optional one
- **Handled in**: Phase 2 — provider.py comment explains the split

### EC-2.2: `safe_null_result` called with `op=None`
- **Trigger**: Downstream code passes `None` where a string op is expected
- **Current behavior**: TypedDict field `op` receives `None`; JSON serialization works but type is wrong
- **Expected behavior**: `safe_null_result` coerces `op` and `target` to `str` via `str(op or "")`
- **Handled in**: Phase 2 — `safe_null_result` defensively coerces args

---

## Phase 3 — NoneProvider

### EC-3.1: `build_result` called with all `None` arguments
- **Trigger**: A caller passes `build_result(None, None, None, None, None)`
- **Current behavior**: Without coercion, `str(None)` produces the string `"None"` which is unexpected in the result
- **Expected behavior**: Returns `{ok: True, op: "", target: "", result: None, engine: "none", cached: False, reason: "no-engine"}` — coerce `None` to empty string
- **Handled in**: Phase 3 — NoneProvider coerces with `str(op or "")` and `str(target or "")`

### EC-3.2: Inner exception in `safe_null_result` itself
- **Trigger**: An attacker or test monkey-patches `safe_null_result` to raise
- **Current behavior**: Without a catch, `build_result` propagates the exception
- **Expected behavior**: NoneProvider wraps the entire body in `try/except Exception: return {"ok": True, "op": str(op or ""), "target": str(target or ""), "result": None, "engine": "none", "cached": False, "reason": "internal-error"}`
- **Handled in**: Phase 3 — NoneProvider has an outer try/except that inline-builds the fallback without calling any helper

---

## Phase 4 — Gateway

### EC-4.1: All providers raise exceptions
- **Trigger**: Every registered provider's `build_result` raises `RuntimeError`
- **Current behavior**: Without a catch, the exception propagates out of `gateway.query()`
- **Expected behavior**: Gateway's outer `try/except Exception` catches everything; returns `safe_null_result(op, target, reason="gateway-error")`
- **Handled in**: Phase 4 — Gateway has two try/except layers: per-provider inner, whole-method outer

### EC-4.2: Provider returns a partial Result (missing required keys)
- **Trigger**: A buggy provider returns `{"ok": True}` with no `op`, `target`, etc.
- **Current behavior**: Caller receives an incomplete dict; downstream code KeyErrors on `result["engine"]`
- **Expected behavior**: In F1, Gateway uses the first non-None result as-is. A full Result validator is F4 work. Document this as a known limitation.
- **Handled in**: Phase 4 — Known Limitation section; gateway validates presence of `ok` key only

### EC-4.3: `engine` routing parameter ignored in F1
- **Trigger**: Caller passes `engine="lsp"` but only `NoneProvider` is registered
- **Current behavior**: Gateway ignores the engine param; returns NoneProvider's safe-null result
- **Expected behavior**: Result includes `reason="engine-unavailable"` so the caller knows why it got null
- **Handled in**: Phase 4 — When engine param is set and no provider matches, override reason to `"engine-unavailable"` in the final safe_null_result

---

## Phase 5 — MCP server

### EC-5.1: MCP SDK API differs from expected pattern
- **Trigger**: The MCP Python SDK uses a different registration API than `@server.call_tool()`
- **Current behavior**: Import error or AttributeError at server startup
- **Expected behavior**: Builder checks `python -c "import mcp; help(mcp.server)"` to discover the real API; adapts registration code accordingly
- **Handled in**: Phase 5 — Recovery note instructs builder to inspect SDK before writing registration code

### EC-5.2: `tools/call code.query` called with missing required fields (`op` or `target`)
- **Trigger**: Host agent sends a malformed tool call with no `op` or `target` field
- **Current behavior**: Without a guard, the handler raises `KeyError`
- **Expected behavior**: Handler uses `.get("op", "")` and `.get("target", "")` with empty-string defaults; calls gateway with those defaults; returns a valid safe-null Result
- **Handled in**: Phase 5 — MCP handler uses `.get()` with defaults, never direct dict access

### EC-5.3: Server process receives SIGTERM while a tool call is in-flight
- **Trigger**: Claude Code kills the server while a request is being processed
- **Current behavior**: The stdio loop aborts; the in-flight request is lost
- **Expected behavior**: This is acceptable in F1 — the safe-null contract applies to completed responses; interrupted stdio is an MCP host concern. Document as known limitation.
- **Handled in**: Known Limitations

---

## Phase 6 — Fault-injection test suite

### EC-6.1: `monkeypatch` scope leaks between tests
- **Trigger**: A monkeypatch in one test is not torn down; next test sees the patched version
- **Current behavior**: Tests pass individually but fail in sequence
- **Expected behavior**: Use pytest `monkeypatch` fixture (function scope by default); each test gets a fresh patch context
- **Handled in**: Phase 6 — always use the `monkeypatch` fixture parameter, never global `patch`

### EC-6.2: MCP server handler is not directly importable for unit testing
- **Trigger**: `server.py` starts the stdio loop on import; tests hang waiting for stdin
- **Current behavior**: `import codeintel.server` blocks the test runner
- **Expected behavior**: `server.py` must separate handler functions (importable, pure) from the `run()` entrypoint (starts the loop). Tests import and call handler functions directly without starting the loop.
- **Handled in**: Phase 5 + Phase 6 — server.py keeps handlers as standalone functions; `run()` is the only place the MCP loop starts

---

## Known Limitations

- Partial Result validation (EC-4.2): Gateway does not validate that a provider's Result has all required keys in F1. This is deferred to F4 (unified gateway).
- In-flight interrupt (EC-5.3): A SIGTERM mid-call leaves the host with no response. The MCP host is responsible for retry; codeintel server does not attempt resumption in F1.
- Engine routing (EC-4.3): The `engine` parameter is captured but not used for real routing in F1. All queries go to NoneProvider. Real routing is F4 work.
