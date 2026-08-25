"""Token→role authentication and RBAC for the HTTP transport.

An optional auth config (``~/.codeintel/auth.toml`` or ``$CODEINTEL_AUTH_CONFIG``) maps bearer
tokens to roles and roles to the operations they may run. The role is derived **server-side from
the authenticated token** — a client can never escalate by claiming a role in the request body.

Example ``auth.toml``::

    [roles]
    admin    = ["*"]                       # ["*"] (or omitted) = every op
    reader   = ["search", "symbol", "overview", "callers", "callees", "impact", "chain", "context"]
    searcher = ["search", "context"]

    [tokens]                               # token = role  (store this file mode 0600)
    "s3cret-admin-value"      = "admin"
    "sha256:<hex-of-a-token>" = "reader"   # pre-hashed form, so plaintext need not be at rest

SSO: put an OIDC auth proxy (e.g. oauth2-proxy) in front and have it present a per-role bearer
token to codeintel — codeintel never speaks to the IdP itself (local-first, no per-query network).
"""
from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import tomllib  # stdlib on Python 3.11+ (the project's minimum)

from codeintel.paths import codeintel_home
from codeintel.policy import TieringPolicy

logger = logging.getLogger("codeintel")

_ALL = "*"
_SHA_PREFIX = "sha256:"


class TokenAuth:
    """Resolves a bearer token to a role and builds the matching op policy. Tokens are held as
    sha256 hashes; a presented token is hashed and looked up in O(1). Immutable; thread-safe."""

    def __init__(self, token_hash_to_role: dict[str, str], role_ops: dict[str, list[str]],
                 role_roots: dict[str, list[str]] | None = None) -> None:
        self._tokens = token_hash_to_role
        self._role_ops = role_ops
        self._role_roots = role_roots or {}

    @property
    def enabled(self) -> bool:
        return bool(self._tokens)

    def role_for(self, presented_token: str) -> str | None:
        """The role for a presented token, or None if it isn't a known token."""
        if not presented_token:
            return None
        h = hashlib.sha256(presented_token.encode("utf-8")).hexdigest()
        return self._tokens.get(h)

    def build_policy(self) -> TieringPolicy:
        """A role with ops ``["*"]`` (or none) is unrestricted → omit it from the rules, since
        TieringPolicy treats a role absent from its rules as full-access. Every other role maps to
        its explicit op allowlist (an empty list = deny all ops, a fail-safe default).

        ``enabled`` is True whenever RBAC is configured at all — NOT merely when some role has a
        restricted op list. It used to be ``bool(rules)``, which meant a config whose roles were
        all ``["*"]`` produced a disabled policy, and a disabled policy enforces no root scoping
        either. Op behavior is unchanged (a role absent from rules is still unrestricted); this
        only ensures the root allowlist is actually consulted."""
        rules: dict[str, list[str]] = {}
        for role, ops in self._role_ops.items():
            if _ALL in ops:
                continue  # unrestricted
            rules[role] = list(ops)
        return TieringPolicy(enabled=True, rules=rules, roots=dict(self._role_roots))


def _auth_config_path() -> pathlib.Path | None:
    env = os.environ.get("CODEINTEL_AUTH_CONFIG", "").strip()
    if env:
        p = pathlib.Path(env)
        return p if p.is_file() else None
    try:
        default = codeintel_home() / "auth.toml"
    except Exception:
        # No CODEINTEL_HOME and no resolvable home. `load_auth` promises never to raise, and its
        # whole contract is "absent config → auth disabled"; a host with nowhere to look is the
        # absent case, not a crash. (Previously this read `Path.home()` inline and raised.)
        return None
    return default if default.is_file() else None


def load_auth() -> TokenAuth:
    """Load the token→role config if present. Never raises — a missing or malformed file yields an
    empty (disabled) TokenAuth, and the caller falls back to single-token / no-auth."""
    path = _auth_config_path()
    if path is None:
        return TokenAuth({}, {})
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        logger.warning("auth: %s could not be parsed (%s) — RBAC is OFF", path, type(exc).__name__)
        return TokenAuth({}, {})

    roles_section = data.get("roles")
    raw_roles: dict = roles_section if isinstance(roles_section, dict) else {}
    role_ops: dict[str, list[str]] = {}
    for role, ops in raw_roles.items():
        if isinstance(ops, list):
            role_ops[str(role)] = [str(o) for o in ops]
        else:
            # Fail CLOSED: a non-list ops value (the missing-brackets typo `reader = "search"`) must
            # never silently become full access — deny all ops for that role instead.
            logger.warning("auth: role %r ops must be a list (got %r) — denying all ops for it", role, ops)
            role_ops[str(role)] = []

    roots_section = data.get("roots")
    raw_roots: dict = roots_section if isinstance(roots_section, dict) else {}
    role_roots: dict[str, list[str]] = {}
    for role, paths in raw_roots.items():
        if isinstance(paths, list):
            role_roots[str(role)] = [str(r) for r in paths]
        else:
            # Same fail-CLOSED rule as ops: a bare string (`reader = "/srv/repo"`) is a typo, and
            # guessing it meant a one-element list would hand out access the operator never wrote.
            logger.warning("auth: role %r roots must be a list (got %r) — denying all paths for it",
                           role, paths)
            role_roots[str(role)] = []

    tokens_section = data.get("tokens")
    raw_tokens: dict = tokens_section if isinstance(tokens_section, dict) else {}
    token_hash_to_role: dict[str, str] = {}
    for tok, role in raw_tokens.items():
        role = str(role)
        # A token mapped to an undefined role fails safe: define it as deny-all (empty op list).
        if role not in role_ops:
            role_ops[role] = []
        role_roots.setdefault(role, [])
        tok = str(tok)
        if tok[:len(_SHA_PREFIX)].lower() == _SHA_PREFIX:  # case-insensitive `sha256:` prefix
            h = tok[len(_SHA_PREFIX):].strip().lower()
        else:
            h = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        if h:
            token_hash_to_role[h] = role

    if not token_hash_to_role:
        logger.warning("auth: %s defines no usable [tokens] — RBAC is OFF (no token→role mapping)", path)

    # A configured-but-unscoped RBAC deployment is the exact shape of the hole this closes, so say
    # so at load time rather than letting the operator discover it as a wall of 403s.
    unscoped = sorted(r for r in role_ops if not role_roots.get(r))
    if token_hash_to_role and unscoped:
        logger.warning(
            "auth: roles %s have no [roots] entry and may target NO project — add a [roots] table "
            "(e.g. `%s = [\"/srv/repos/team-a\"]`, or `= [\"*\"]` for unrestricted)",
            ", ".join(unscoped), unscoped[0])

    return TokenAuth(token_hash_to_role, role_ops, role_roots)
