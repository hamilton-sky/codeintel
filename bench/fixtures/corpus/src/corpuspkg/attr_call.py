"""UNDECIDABLE — an attribute call on a value. The receiver's type is not a syntactic fact."""


class Relay:
    def __init__(self, bus):
        self.bus = bus

    def go(self):
        return self.bus._broadcast("q")
