# F1 MCP Skeleton — Plan Architecture

## Design Decisions

### Decision 1: `typing.Protocol` for CodeProvider (not ABC)
`CodeProvider` is a structural protocol, not an abstract base class. This means engines (F2, F3, F5)
can be registered without inheriting from a base class — they only need to implement the
`build_result` method signature. This keeps the provider seam lightweight and avoids import coupling
between the gateway and concrete engine packages.

**Why not ABC**: ABCs require explicit `super().__init__()` calls and inheritance — unnecessary for a
pure interface in Python. Protocols enable duck typing and are the idiomatic Python 3.8+ pattern.

### Decision 2: `TypedDict` for Result (not dataclass)
`Result` is a `TypedDict` rather than a dataclass or Pydantic model. This makes JSON serialization
trivial (`json.dumps(result)` works directly) and keeps the module dependency-free. The MCP tool
handler can return the dict without any conversion step.

**Tradeoff**: No automatic validation of field types at runtime. A Result with the wrong field types
will pass Python type checking but fail silently. This is intentional in F1 — the gateway's
never-raise contract is more important than strict validation. F4 can add a validator if needed.

### Decision 3: Gateway as the sole never-raise boundary
Providers (F2, F3, F5) are allowed to raise internally — the Gateway catches them. This is cleaner
than requiring every provider to be safe, because:
- It centralizes the safety invariant in one place (easier to audit)
- Providers can use normal exception handling without ceremony
- NoneProvider is still safe internally (as a correctness example), but the Gateway's catch is the
  real safety net

### Decision 4: NoneProvider as Gateway default
`Gateway()` with no arguments falls back to `[NoneProvider()]`. This means:
- A bare `Gateway()` always has at least one safe provider
- No "no providers configured" crash path exists
- F2 and F3 will register alongside NoneProvider, not replace it — NoneProvider becomes the
  ultimate fallback

### Decision 5: Handlers separate from `run()`
`server.py` defines tool handlers as standalone functions; `run()` is the only place that starts the
MCP stdio loop. This makes the handlers unit-testable without starting the loop. The fault-injection
tests import and call handlers directly.

---

## Phase Mapping

## Phase 1 — Package skeleton
Sets up the `src/` layout and entry point. No logic lives here — just package metadata and the
`__main__.py` CLI stub. Every subsequent phase imports from `src/codeintel/`.

## Phase 2 — CodeProvider protocol
Defines the shared type language: `CodeProvider`, `Result`, `safe_null_result`. All other modules
import from `provider.py`. This is the only module with no imports from the rest of the package
(dependency graph root).

## Phase 3 — NoneProvider
Depends only on `provider.py`. Kept trivially small (~40 lines) because its job is to be obviously
correct — it is the trust anchor for the never-raise tests.

## Phase 4 — Gateway
Depends on `provider.py` and `providers/none.py`. Owns the safety boundary. The engine routing
stub in F1 is intentionally minimal — it tries providers in order and returns the first non-None
result. F4 adds real engine selection.

## Phase 5 — MCP server
Depends on `gateway.py`. The only module that imports the MCP SDK. Kept thin: registration +
delegation to Gateway. All intelligence is in the gateway and providers.

## Phase 6 — Tests
Depends on everything above. Tests import handlers, providers, and gateway directly. No mocking of
the MCP SDK transport — tests are unit-level (handler functions), not integration (full stdio loop).

---

## Module dependency graph

```
__main__.py ──► server.py ──► gateway.py ──► providers/none.py ──► provider.py
                                         └──► provider.py
```

`provider.py` has NO imports from the rest of the package (safe import root).
`providers/none.py` imports only from `provider.py`.
`gateway.py` imports from `provider.py` and `providers/none.py`.
`server.py` imports from `gateway.py` and the `mcp` SDK.
`__main__.py` imports from `server.py`.

---

## File size targets

| File | Target (lines) | Rationale |
|---|---|---|
| `pyproject.toml` | ~30 | Just metadata and deps |
| `src/codeintel/__init__.py` | ~5 | Version + re-exports only |
| `src/codeintel/provider.py` | ~60 | Protocol + TypedDict + factory |
| `src/codeintel/providers/none.py` | ~40 | Intentionally trivial |
| `src/codeintel/gateway.py` | ~70 | Registry + routing + safety |
| `src/codeintel/server.py` | ~120 | Tool registration + handlers |
| `tests/test_never_raise.py` | ~100 | 6 test groups × ~15 lines |
