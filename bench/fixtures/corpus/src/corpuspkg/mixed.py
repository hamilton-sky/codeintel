"""UNDECIDABLE wins over NOT-TARGET, within one enclosing symbol.

`_broadcast` is bound locally here (which alone would be a proven negative), but the same function
also reaches an attribute the syntax cannot resolve. A key with any doubt in it must not be scored as
a proven non-caller, or an engine gets charged a false positive for a claim that might be right.
"""


def handle(bus):
    _broadcast = bus.make()
    bus.thing._broadcast()
    return _broadcast("x")
