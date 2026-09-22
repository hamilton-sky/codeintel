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


# The ops that answer for the WHOLE repository and therefore ignore `target`. Every OTHER op in
# `QUERY_OPS` needs one, and asking without it is a caller mistake — not a fact about the code.
#
# Kept here, in the import-free module, because the check that uses it runs in `Gateway._query`:
# `providers/graph.py` holds the renderer-side copy (`_ROOT_SCOPED_OPS`) and importing it would
# make the gateway's argument check depend on the graph provider. That copy carries two names this
# one does not — `changes` (an alias) and `deadcode` (withdrawn) — neither of which is a `code.query`
# op, so the two sets agree exactly where they overlap. `tests/test_missing_target.py` guards the
# pair against drift the same way `test_mcp_server.py` already guards `QUERY_OPS` against
# `server._QueryOp`.
TARGETLESS_OPS: tuple[str, ...] = ("overview", "changed", "hotspots")

OPS_REQUIRING_A_TARGET: frozenset[str] = frozenset(QUERY_OPS) - frozenset(TARGETLESS_OPS)
