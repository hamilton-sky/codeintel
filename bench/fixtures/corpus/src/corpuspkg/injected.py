"""UNDECIDABLE — a bare name nothing in this file binds.

The `describe` shape exactly. In Python this is where the oracle STOPS: an unbound global could have
been installed by another module, so the syntax cannot rule the target out. In TypeScript the same
shape is decidable, because a module-scope symbol elsewhere is reachable only through an import.
"""


def suite():
    describe("a thing", lambda: None)
