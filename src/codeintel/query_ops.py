"""The canonical `code.query` operation vocabulary, shared by the CLI parser and (in spirit) the
MCP server's tool schema.

Split out from `codeintel.server` because that module imports `mcp`, `anyio` and `pydantic` to
build the MCP tool schema — measured at ~4.4s to import — which is not a price `codeintel --help`
or `codeintel query --op <TAB>` should pay just to know the op vocabulary. This module has no
imports of its own.

`codeintel.server._QueryOp` (a `Literal`) is the authoritative definition; keep this tuple in sync
with it by hand. Moving `server.py` to import from here instead of retyping its `Literal` args is
the natural next step, but `server.py` is out of scope for the change that introduced this module.
"""

QUERY_OPS: tuple[str, ...] = (
    "search", "symbol", "callers", "callees", "impact", "chain",
    "pattern", "overview", "context", "changed", "hotspots",
)
