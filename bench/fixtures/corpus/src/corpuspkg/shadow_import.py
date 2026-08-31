"""NOT-TARGET — an import of a DIFFERENT symbol that happens to share the name."""
from corpuspkg.other import _broadcast


def emit():
    return _broadcast("w")
