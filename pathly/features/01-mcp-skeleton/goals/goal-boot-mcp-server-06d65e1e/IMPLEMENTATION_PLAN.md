# F1 MCP Skeleton — Implementation Plan

## Overview

Boots the `codeintel` MCP server with a safe-null `NoneProvider`. Establishes the
`CodeProvider` protocol, the `{ok, op, target, result, engine, cached, reason?}` envelope
contract, and the `code.query` + `code.status` MCP tool surface. No real engine is wired;
all queries return a well-formed safe-null response. The never-raise invariant is a
fault-injection tested guarantee from day one.

## Layer Architecture

```
MCP host (Claude/Codex)
       │ stdio JSON-RPC
       ▼
  server.py  (MCP tool registration + stdio loop)
       │
       ▼
  gateway.py (provider registry, routing, safe-null wrapping)
       │ CodeProvider protocol
       ▼
  providers/none.py  (NoneProvider — always safe-null)
       │
       ▼
  provider.py  (Protocol definition + Result TypedDict)
```

---

## Phase 1 — Package skeleton

**File:** `pyproject.toml` — CREATE (project definition + dependencies)
**Also:** `src/codeintel/__init__.py`, `src/codeintel/providers/__init__.py`
**Done when:** `python -m codeintel --version` exits 0 and prints `codeintel 0.1.0`.
**Delivers stories:** S1.1
**Depends on:** nothing — root phase
**Enables:** all subsequent phases (package importable)
**Details:**
- `[project]` name=`codeintel`, version=`0.1.0`, requires-python=`>=3.11`
- `dependencies = ["mcp>=1.0"]`
- `[project.scripts]` entry: `codeintel = "codeintel.__main__:main"`
- `src/codeintel/__init__.py`: exports `__version__ = "0.1.0"`
- `src/codeintel/providers/__init__.py`: empty init
- `src/codeintel/__main__.py`: `main()` stub that accepts `serve` subcommand and prints version for `--version`
- Use `src/` layout — add `[tool.setuptools.packages.find] where = ["src"]`
**Verify:** `cd /path/to/codeintel && pip install -e . && python -m codeintel --version`
**Recovery:** If pip install fails due to missing mcp package, install it first with `pip install mcp`. If mcp is not yet on PyPI under that name, use `pip install mcp[cli]` or `pip install modelcontextprotocol`. If fundamentally broken, rollback with `git checkout` on affected files and retry.

---

## Phase 2 — CodeProvider protocol

**File:** `src/codeintel/provider.py` — CREATE (protocol + result type)
**Done when:** `python -c "from codeintel.provider import CodeProvider, Result; print('OK')"` prints `OK`.
**Delivers stories:** S1.2
**Depends on:** Phase 1 (package importable)
**Enables:** Phase 3 (NoneProvider implements this protocol)
**Details:**
- `Result` as `TypedDict` with fields:
  - `ok: bool`
  - `op: str`
  - `target: str`
  - `result: Any | None`  (use `Optional[Any]`)
  - `engine: str`
  - `cached: bool`
  - `reason: str` (optional — use `total=False` for this field or `NotRequired`)
- `CodeProvider` as `typing.Protocol`:
  - method: `build_result(self, op: str, target: str, files: list[str] | None, budget: int | None, project_root: str | None) -> Result | None`
  - Docstring: "Implementors MUST never raise. Return None to signal unavailability; the gateway wraps it."
- Helper: `safe_null_result(op: str, target: str, engine: str = "none", reason: str = "no-engine") -> Result`
  - Returns a complete, well-formed Result with `ok=True`, `result=None`, `cached=False`
  - This is the shared factory used by NoneProvider and Gateway
- Keep the module under 100 lines
**Verify:** `python -c "from codeintel.provider import CodeProvider, Result, safe_null_result; r = safe_null_result('symbol', 'foo'); assert r['ok'] and r['result'] is None; print('OK')"`
**Recovery:** If `NotRequired` is not available in older Python, fall back to `total=False` TypedDict. If fundamentally broken, rollback with git checkout on `src/codeintel/provider.py` and retry.

---

## Phase 3 — NoneProvider

**File:** `src/codeintel/providers/none.py` — CREATE (NoneProvider implementation)
**Done when:** `python -c "from codeintel.providers.none import NoneProvider; p = NoneProvider(); r = p.build_result('symbol','x',None,None,None); assert r['engine']=='none'; print('OK')"` prints `OK`.
**Delivers stories:** S1.3
**Depends on:** Phase 2 (CodeProvider protocol and safe_null_result available)
**Enables:** Phase 4 (Gateway registers NoneProvider as default)
**Details:**
- `class NoneProvider` with no `__init__` params needed
- `build_result(...)` wraps entire body in `try/except Exception: return safe_null_result(op, str(target))` as inner guard
- Normal path: call `safe_null_result(op, str(target), engine="none", reason="no-engine")`
- Coerce `op` and `target` to `str` safely (handle `None` inputs via `str(op or "")`)
- Keep under 40 lines — this module is intentionally trivial
**Verify:** `python -c "from codeintel.providers.none import NoneProvider; p=NoneProvider(); assert p.build_result(None,None,None,None,None)['ok']; print('OK')"`
**Recovery:** If the import path conflicts, check `src/codeintel/providers/__init__.py` is present. Rollback with git checkout on `src/codeintel/providers/none.py` if fundamentally broken.

---

## Phase 4 — Gateway

**File:** `src/codeintel/gateway.py` — CREATE (provider registry and safe-null routing)
**Done when:** `python -c "from codeintel.gateway import Gateway; g=Gateway(); r=g.query('symbol','main'); assert r['ok'] and r['result'] is None; print('OK')"` prints `OK`.
**Delivers stories:** S1.4
**Depends on:** Phase 3 (NoneProvider exists; CodeProvider protocol defined)
**Enables:** Phase 5 (MCP server delegates to Gateway)
**Details:**
- `class Gateway`:
  - `__init__(self, providers: list[CodeProvider] | None = None)`:
    - If `providers` is None or empty, set `self._providers = [NoneProvider()]`
  - `query(self, op: str, target: str, engine: str | None = None, budget: int | None = None, project_root: str | None = None) -> Result`:
    - Entire method wrapped in outer `try/except Exception` → return `safe_null_result(op, target, reason="gateway-error")`
    - Loop over `self._providers`: call each in its own `try/except`; if one returns a non-None `Result`, return it immediately
    - If none return a result, return `safe_null_result(op, target, reason="no-result")`
    - Engine routing: in this skeleton, the engine param is logged/stored on the result but routing to specific providers by engine is a stub (all providers are tried in order)
- Keep under 80 lines — engine routing logic belongs in F4
**Verify:** `python -c "from codeintel.gateway import Gateway; from codeintel.providers.none import NoneProvider; g=Gateway([NoneProvider()]); r=g.query('impact','parse_result'); assert r['ok']; print('OK')"`
**Recovery:** If circular import between gateway and provider, check that `provider.py` does not import from gateway. Rollback with git checkout if fundamentally broken.

---

## Phase 5 — MCP server

**File:** `src/codeintel/server.py` — CREATE (MCP tool registration + stdio runner)
**Also:** update `src/codeintel/__main__.py` to wire `serve` subcommand to `server.run()`
**Done when:** `echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | python -m codeintel serve` returns a valid JSON-RPC response without error.
**Delivers stories:** S1.5
**Depends on:** Phase 4 (Gateway available)
**Enables:** Phase 6 (test suite can import and call the MCP tool handlers directly)
**Details:**
- Import `mcp.server.stdio` and `mcp.types` (or equivalent from the MCP Python SDK)
- Instantiate a `Gateway()` at module level (or lazily in `run()`)
- Register tool `code_query` (wire name `code.query` — confirm exact naming from MCP SDK):
  - Input schema: `{op: str, target: str, engine?: str, budget?: int, project_root?: str}`
  - Handler: calls `gateway.query(...)` and returns the `Result` dict as JSON
  - Handler is wrapped in try/except — any failure returns `safe_null_result(op, target, reason="server-error")`
- Register tool `code_status` (wire name `code.status`):
  - Input schema: `{project_root?: str}`
  - Handler: returns `{"ok": True, "engines": ["none"], "indexed": False, "model": None}`
  - Always safe-null: never raises
- `run()` function: calls `mcp.server.stdio.run_server(server)` (or SDK equivalent)
- Respect the MCP SDK's actual API — check the SDK's README/docs for the correct registration pattern (`@server.call_tool()`, `@server.list_tools()`, etc.)
- Keep server.py under 150 lines
**Verify:** `echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' | timeout 3 python -m codeintel serve 2>/dev/null | head -1 | python -m json.tool`
**Recovery:** If the MCP SDK API differs from expected, read `python -c "import mcp; help(mcp.server)"` to discover the correct registration pattern. Do NOT touch gateway.py or provider.py. Rollback with git checkout on server.py if fundamentally broken.

---

## Phase 6 — Fault-injection test suite

**File:** `tests/test_never_raise.py` — CREATE (never-raise invariant tests)
**Also:** `tests/__init__.py` — CREATE (empty)
**Done when:** `pytest tests/test_never_raise.py -v` exits 0 with all tests passing.
**Delivers stories:** S1.6
**Depends on:** Phases 1–5 (all modules exist and are importable)
**Enables:** feature completion — this is the acceptance gate for the never-raise contract
**Details:**
- Use `pytest` + `unittest.mock.patch` / `monkeypatch`
- Test groups:
  1. **NoneProvider tests**: call `build_result` with `None` for every arg; with strings; with ints; with wrong types; confirm always returns a `Result` with `ok=True`
  2. **NoneProvider raised-exception test**: `monkeypatch` `safe_null_result` to raise `RuntimeError`; confirm `build_result` still returns a `Result` (the inner try/except catches it)
  3. **Gateway — empty providers**: `Gateway([])` — `.query(...)` returns valid `Result`
  4. **Gateway — provider raises**: register a mock provider whose `build_result` raises; confirm `gateway.query(...)` still returns valid `Result`
  5. **Gateway — provider returns None**: provider returns `None`; gateway wraps to safe-null `Result`
  6. **MCP handler — missing fields**: call the `code_query` handler directly with `{}` (no `op` or `target`); confirm returns valid `Result`, not exception
- Add a `conftest.py` if shared fixtures are needed
- Do NOT touch any implementation files — only add tests
**Verify:** `pytest tests/test_never_raise.py -v --tb=short`
**Recovery:** If import paths are wrong, check that `pip install -e .` was run. If a test itself raises, the test is wrong — fix the test, not the implementation. Do not skip tests; all must pass.

---

## Prerequisites

- Python 3.11+ (3.14 confirmed available)
- `uvx` available for running `codeintel` without a global install
- `mcp` Python SDK available via pip (install with `pip install mcp` or `pip install "mcp[cli]"`)
- `pytest` available (`pip install pytest`)

## Key Decisions

- **src/ layout**: keeps the package importable only after install/editable-install, preventing accidental bare-directory imports.
- **TypedDict for Result**: avoids a dataclass to keep JSON serialization trivial and mypy-compatible without extra deps.
- **Gateway as the only never-raise boundary**: providers are allowed to raise; the gateway catches them. This is cleaner than requiring every provider to be safe.
- **NoneProvider as default**: Gateway defaults to `[NoneProvider()]` so a bare `Gateway()` always works — no "no providers configured" crash path.
- **MCP Python SDK**: the official Python SDK (`mcp`) is the only MCP dependency; no hand-rolled JSON-RPC.
