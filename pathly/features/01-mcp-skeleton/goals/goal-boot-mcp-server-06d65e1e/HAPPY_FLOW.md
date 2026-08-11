# F1 MCP Skeleton — Happy Flow

## Overview

A host agent (Claude Code) wants to query code intelligence on a repo. It calls the `codeintel`
MCP server with `code.query`. With no real engine installed (F1 state), every query returns a
clean safe-null envelope. The agent degrades gracefully to grep — no crash, no missing field,
no partial JSON. The happy flow ends with a fully-importable package, a running MCP server, and
a passing fault-injection test suite.

---

## Phase 1 — Package skeleton

### Step-by-Step

#### Step 1: Developer installs the package
- **User does**: `pip install -e .` in the repo root (or `uvx codeintel` for one-shot use)
- **System does**: Reads `pyproject.toml`, resolves `mcp` dependency, installs the `codeintel` package into the active Python environment
- **State after**: `python -c "import codeintel"` succeeds; `codeintel` command is on PATH

#### Step 2: Version check
- **User does**: `python -m codeintel --version`
- **System does**: `__main__.py` parses `--version`, reads `codeintel.__version__`, prints it
- **State after**: Terminal prints `codeintel 0.1.0`; exit code 0

## Phase 2 — CodeProvider protocol

### Step-by-Step

#### Step 1: Engine author inspects the protocol
- **User does**: `python -c "from codeintel.provider import CodeProvider, Result, safe_null_result; help(CodeProvider)"`
- **System does**: Python imports `provider.py`, prints the Protocol docstring and method signature
- **State after**: Engine author sees the exact interface contract (`build_result` signature + never-raise expectation)

#### Step 2: Result factory produces a well-formed envelope
- **User does**: `safe_null_result(op="symbol", target="parse_result")`
- **System does**: Returns `{"ok": True, "op": "symbol", "target": "parse_result", "result": None, "engine": "none", "cached": False, "reason": "no-engine"}`
- **State after**: All required fields present, JSON-serializable, no missing keys

## Phase 3 — NoneProvider

### Step-by-Step

#### Step 1: Provider is instantiated and queried
- **User does**: `NoneProvider().build_result("impact", "parse_result", None, None, None)`
- **System does**: Calls `safe_null_result("impact", "parse_result")` wrapped in try/except; returns the envelope
- **State after**: Caller receives `{ok: True, result: None, engine: "none", cached: False, reason: "no-engine"}`; no exception thrown

#### Step 2: Malformed input is tolerated
- **User does**: `NoneProvider().build_result(None, None, "wrong-type", -1, 12345)`
- **System does**: Coerces args to str; inner try/except catches any conversion error; returns safe-null
- **State after**: Caller receives a valid Result; provider never raises

## Phase 4 — Gateway

### Step-by-Step

#### Step 1: Gateway is created with default providers
- **User does**: `Gateway()` (no arguments)
- **System does**: Detects empty provider list, falls back to `[NoneProvider()]`
- **State after**: `gateway._providers` = `[NoneProvider()]`

#### Step 2: Query is routed and wrapped
- **User does**: `gateway.query(op="impact", target="parse_result")`
- **System does**: Loops providers; NoneProvider returns safe-null Result; Gateway returns it immediately
- **State after**: Caller receives `{ok: True, result: None, engine: "none", ...}`; no exception

#### Step 3: Crashing provider is silently handled
- **User does**: (in a test) register a provider that raises `RuntimeError`
- **System does**: Gateway catches exception in the per-provider try/except; continues to next provider; if none succeed, returns `safe_null_result(op, target, reason="no-result")`
- **State after**: Caller still receives a valid Result; crashing provider is isolated

## Phase 5 — MCP server

### Step-by-Step

#### Step 1: Server starts in stdio mode
- **User does**: `python -m codeintel serve` (or Claude Code auto-starts it via MCP config)
- **System does**: `__main__.py` routes to `server.run()`; MCP SDK starts the stdio JSON-RPC loop; server registers `code.query` and `code.status` tools
- **State after**: Server is listening on stdio; MCP `initialize` handshake can complete

#### Step 2: Host agent lists available tools
- **User does**: MCP `tools/list` call
- **System does**: Server returns `[{"name": "code.query", ...}, {"name": "code.status", ...}]`
- **State after**: Agent sees both tools with their input schemas

#### Step 3: Agent calls code.query
- **User does**: `tools/call code.query {op: "symbol", target: "parse_result"}`
- **System does**: Handler passes args to `gateway.query()`; gateway returns safe-null Result; handler serializes to JSON and responds
- **State after**: Agent receives `{ok: true, op: "symbol", target: "parse_result", result: null, engine: "none", cached: false, reason: "no-engine"}`

#### Step 4: Agent calls code.status
- **User does**: `tools/call code.status {}`
- **System does**: Handler returns status dict: `{ok: true, engines: ["none"], indexed: false, model: null}`
- **State after**: Agent knows no engines are installed yet; can plan to install them later

## Phase 6 — Fault-injection test suite

### Step-by-Step

#### Step 1: Developer runs the test suite
- **User does**: `pytest tests/test_never_raise.py -v`
- **System does**: pytest discovers all test functions; injects exceptions via `monkeypatch` and mock; asserts that every code path returns a valid Result
- **State after**: All tests green; never-raise contract is proven

---

## End State

- `codeintel` is installable and importable.
- `code.query` and `code.status` are registered MCP tools.
- Every query returns a well-formed `{ok, op, target, result, engine, cached, reason?}` envelope.
- No code path raises an exception (proven by fault-injection tests).
- The `CodeProvider` protocol is documented and ready for F2 (GraphProvider) to implement.

## Success Indicators

- [ ] `python -m codeintel --version` prints `codeintel 0.1.0`
- [ ] `from codeintel.provider import CodeProvider, Result` imports cleanly
- [ ] `NoneProvider().build_result(None, None, None, None, None)['ok'] == True`
- [ ] `Gateway().query("symbol", "foo")['result'] is None`
- [ ] MCP `tools/call code.query` returns a valid envelope JSON
- [ ] `pytest tests/test_never_raise.py` exits 0
