"""CALL — the control. Direct import, direct call."""
from corpuspkg.sse import _broadcast


def send():
    return _broadcast("x")
