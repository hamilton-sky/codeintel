from __future__ import annotations

import os

_ALL = "*"


def _normalize_root(path: str) -> str:
    """An absolute, symlink-resolved root for containment comparison. Resolving BOTH sides is what
    makes `..` traversal and symlink escapes ineffective: `/srv/a/../../etc` normalizes to `/etc`
    and simply fails the prefix test."""
    try:
        return os.path.realpath(path).rstrip(os.sep) or os.sep
    except Exception:
        return ""


class TieringPolicy:
    """Role access policy — disabled by default (everything allowed).

    Two independent dimensions, and they are NOT interchangeable:

    * **ops** (*rules*) — which operations a role may call. A role absent from *rules* is
      unrestricted, which is why an op allowlist alone is fail-open by design.
    * **roots** — which project directories a role may point those operations AT. This one fails
      CLOSED: a role with no roots declared may target nothing.

    The asymmetry is deliberate. `project_root` arrives in the request body, so without a root
    allowlist any role that can call `search` can direct the server to walk, index, and return the
    contents of any directory the server process can read — the op allowlist never sees it. Scoping
    operations while leaving the target unbounded is not a partial restriction; it is none.

    Immutable after construction; safe to share across threads.
    """

    def __init__(
        self,
        enabled: bool = False,
        rules: dict[str, list[str]] | None = None,
        roots: dict[str, list[str]] | None = None,
    ) -> None:
        self._enabled = enabled
        self._rules: dict[str, list[str]] = (
            {role: list(ops) for role, ops in rules.items()} if rules else {}
        )
        self._roots: dict[str, list[str]] = {}
        for role, paths in (roots or {}).items():
            if _ALL in paths:
                self._roots[role] = [_ALL]
            else:
                # Drop blanks BEFORE normalizing. `_normalize_root("")` is the process cwd, not
                # "", so a stray empty string or trailing-comma artifact in an operator's roots
                # list silently added "wherever the server was launched" to the allowlist — the
                # one fail-OPEN case in an otherwise fail-closed model.
                self._roots[role] = [
                    r for r in (_normalize_root(str(p)) for p in paths if str(p).strip()) if r
                ]

    def is_allowed(self, role: str, op: str) -> bool:
        if not self._enabled:
            return True
        allowed_ops = self._rules.get(role)
        if allowed_ops is None:
            return True
        return op in allowed_ops

    def is_root_allowed(self, role: str, project_root: str) -> bool:
        """May *role* target *project_root*? Fails closed when RBAC is on.

        Containment is computed on realpaths of both sides, so `..` segments and a symlinked
        project_root cannot walk out of an allowed root. A blank project_root is rejected before
        resolution, because realpath("") is the server's cwd rather than an error.

        NOTE the boundary this does and does not draw: it bounds which root a caller may NAME. It
        does not bound what the indexer reads once inside — that containment lives in
        ``Indexer._walk_files``, which must skip symlinks escaping the tree, or a tenant who can
        write into their own root defeats this by planting one."""
        if not self._enabled:
            return True
        allowed = self._roots.get(role)
        if not allowed:                      # undeclared or explicitly empty → nothing permitted
            return False
        if _ALL in allowed:
            return True
        # Reject blank BEFORE resolving. `os.path.realpath("")` returns the process's cwd, so a
        # missing project_root silently became "wherever the server was launched" — and passed
        # whenever that happened to sit under an allowed root. The docstring claimed otherwise.
        if not project_root or not project_root.strip():
            return False
        target = _normalize_root(project_root)
        if not target:
            return False
        return any(target == root or target.startswith(root + os.sep) for root in allowed)
