"""`codeintel gen-token` — a secure random bearer token for serve-http / RBAC auth.toml."""

import secrets
from typing import Any


def run(args: Any) -> int:
    print(secrets.token_urlsafe(32))
    return 0
