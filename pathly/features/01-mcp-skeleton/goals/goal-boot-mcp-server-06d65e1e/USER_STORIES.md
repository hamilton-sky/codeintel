# F1 MCP Skeleton — User Stories

## Context

The `codeintel` project is a greenfield MCP-native code-intelligence server. F1 is the root feature:
it boots the MCP server and establishes the `code.query` + `code.status` tool contract backed by a
`NoneProvider` that always returns a safe-null envelope. No real engine is wired yet — this is the
safety skeleton every subsequent feature builds on. The never-raise contract is a tested invariant
from day one.

---

## Stories

### Story S1.1: Package installs and imports cleanly
**As a** developer integrating the server, **I want** to install `codeintel` with `uvx` or `pip`,
**so that** I can import the package and see the version without errors.

**Acceptance Criteria:**
- [ ] `pyproject.toml` declares the package name `codeintel`, version `0.1.0`, and Python `>=3.11`.
- [ ] `mcp` is listed as a dependency (the Python MCP SDK).
- [ ] `python -c "import codeintel; print(codeintel.__version__)"` prints `0.1.0` after install.
- [ ] A `codeintel` entry-point script is declared (for `uvx codeintel serve`).

**Delivered by:** Phase 1 → Conversation 1

---

### Story S1.2: CodeProvider protocol is defined and documentable
**As a** future engine author, **I want** a clear `CodeProvider` protocol,
**so that** I know exactly what interface my engine must implement.

**Acceptance Criteria:**
- [ ] `codeintel.provider` exports `CodeProvider` (a `typing.Protocol`) with one method:
  `build_result(op, target, files, budget, project_root) -> Result | None`.
- [ ] `Result` is a `TypedDict` with fields: `ok`, `op`, `target`, `result`, `engine`, `cached`, and optional `reason`.
- [ ] Importing `from codeintel.provider import CodeProvider, Result` works with no runtime error.
- [ ] A docstring describes each field of `Result` and the never-raise expectation.

**Delivered by:** Phase 2 → Conversation 1

---

### Story S1.3: NoneProvider returns a safe-null envelope
**As a** host agent calling `code.query`, **I want** a well-formed response even when no engine is installed,
**so that** I can degrade gracefully to grep without a crash or exception.

**Acceptance Criteria:**
- [ ] `NoneProvider` implements `CodeProvider` and `build_result(...)` always returns a `Result` with `ok=True`, `result=None`, `engine="none"`, `cached=False`, `reason="no-engine"`.
- [ ] `NoneProvider.build_result` never raises, regardless of inputs (including `None`, empty strings, or objects of the wrong type).
- [ ] The returned envelope has all required fields: `{ok, op, target, result, engine, cached, reason}`.

**Delivered by:** Phase 3 → Conversation 1

---

### Story S1.4: Gateway routes queries and builds safe-null envelopes
**As a** developer, **I want** a `Gateway` class that holds a registry of providers and routes `code.query` calls,
**so that** the MCP server can delegate to any provider behind a uniform contract.

**Acceptance Criteria:**
- [ ] `Gateway(providers=[...])` accepts a list of `CodeProvider` instances; defaults to `[NoneProvider()]` when empty.
- [ ] `Gateway.query(op, target, engine=None, budget=None, project_root=None)` returns a `Result` dict.
- [ ] If all providers return `None`, the gateway wraps a safe-null `Result` itself (never returns `None` bare).
- [ ] Gateway never raises regardless of provider behaviour (providers are called inside a try/except).

**Delivered by:** Phase 4 → Conversation 2

---

### Story S1.5: MCP server exposes code.query and code.status tools
**As a** host agent (Claude/Codex), **I want** to call `code.query` and `code.status` over MCP stdio,
**so that** I can integrate the server into my tool chain without any special protocol knowledge.

**Acceptance Criteria:**
- [ ] The MCP server registers `code.query` and `code.status` as callable tools.
- [ ] `code.query({op, target, engine?, budget?, project_root?})` returns the safe-null envelope JSON.
- [ ] `code.status({project_root?})` returns `{ok: true, engines: ["none"], indexed: false, model: null}`.
- [ ] The server starts via `codeintel serve` (stdio mode) without error on a bare machine.
- [ ] A well-formed MCP `initialize` / `tools/list` / `tools/call` sequence completes without error.

**Delivered by:** Phase 5 → Conversation 2

---

### Story S1.6: Fault-injection tests prove the never-raise contract
**As a** maintainer, **I want** a test suite that injects exceptions at every code path,
**so that** I can guarantee the never-raise invariant before merging any code.

**Acceptance Criteria:**
- [ ] `pytest tests/test_never_raise.py` passes with zero failures.
- [ ] Tests cover: `NoneProvider.build_result` with `None` args, with wrong-typed args, with raised exceptions patched via `monkeypatch`.
- [ ] Tests cover: `Gateway.query` when the registered provider raises an exception — gateway still returns a valid `Result`.
- [ ] Tests cover: `Gateway.query` with no providers registered.
- [ ] Tests cover: MCP tool handler calling `code.query` with missing required fields (expects a safe-null, not an exception).

**Delivered by:** Phase 6 → Conversation 3
