"""`codeintel serve-http` — the HTTP transport, loopback-only unless --allow-remote."""

import os
from typing import Any

from codeintel.commands._common import never_raise


# A startup failure (e.g. port in use) should print a friendly line, not a traceback. SystemExit
# from the non-loopback guard is BaseException, so it passes through the decorator untouched.
@never_raise("serve-http failed: {exc}", code=1, stderr=True)
def run(args: Any) -> int:
    from codeintel.http_server import run as serve_http

    token = args.token or os.environ.get("CODEINTEL_HTTP_TOKEN")
    try:
        serve_http(host=args.host, port=args.port, allow_remote=args.allow_remote, token=token)
    except KeyboardInterrupt:
        pass
    return 0
