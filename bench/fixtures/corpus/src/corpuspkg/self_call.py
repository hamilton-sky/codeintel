"""A module that calls the function it defines — the shape that needs no import at all.

Its own `def` binds the name at module scope, and `_accounted_by` reports exactly that: "this
module binds it". Read as "some OTHER `relay_self`", every call below becomes a PROVEN NON-CALLER —
the oracle asserting the opposite of the truth, in the one label that exists to charge an engine
for being wrong. Measured cost before this file existed: `snitch-simulator`'s `_strip_hop_by_hop` is
called three times in the file that defines it, all three were scored against the engine that found
them, and the arm read 38% direct precision as a result.

`shadowed` is the other half. The fix must stay scope-aware, or it trades one wrong label for
another: a parameter of the same name is a nearer binding and really is a different `relay_self`.
"""


def relay_self(msg):
    """Defined here, called here."""
    return msg


def fan(msg):
    # No import, and unambiguously the function above.
    return relay_self(msg)


def shadowed(relay_self):
    # A parameter of the same name. The nearest binding wins, and it is not the target.
    return relay_self()
