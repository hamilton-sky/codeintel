"""`codeintel serve` — the MCP server over stdio, which is what an agent host launches."""

from typing import Any


def run(args: Any) -> int:
    from codeintel.server import run as serve_stdio

    serve_stdio()
    return 0
