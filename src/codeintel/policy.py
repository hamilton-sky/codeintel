from __future__ import annotations


class TieringPolicy:
    """Role/op access policy — disabled by default (all ops allowed).

    When enabled, a role present in *rules* is restricted to only the ops
    listed for it.  A role absent from *rules* gets full access regardless.
    Immutable after construction; safe to share across threads.
    """

    def __init__(
        self,
        enabled: bool = False,
        rules: dict[str, list[str]] | None = None,
    ) -> None:
        self._enabled = enabled
        self._rules: dict[str, list[str]] = (
            {role: list(ops) for role, ops in rules.items()} if rules else {}
        )

    def is_allowed(self, role: str, op: str) -> bool:
        if not self._enabled:
            return True
        allowed_ops = self._rules.get(role)
        if allowed_ops is None:
            return True
        return op in allowed_ops
