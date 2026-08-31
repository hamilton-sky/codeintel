"""CALL — reached through two stated re-exports, which is ordinary Python, not an edge case."""
from corpuspkg.facade import _broadcast


def relay():
    return _broadcast("y")
