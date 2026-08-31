"""NOT-TARGET — a local assignment accounts for the name, so this call is a different function."""


def handler(bus):
    _broadcast = bus.make()
    return _broadcast("y")
