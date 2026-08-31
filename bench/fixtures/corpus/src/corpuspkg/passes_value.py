"""REFERENCE — the `forward_released_item` case: passed, never invoked. Zero calls, one reference."""
from corpuspkg.sse import _broadcast


def install(bus):
    bus.on_message = _broadcast
