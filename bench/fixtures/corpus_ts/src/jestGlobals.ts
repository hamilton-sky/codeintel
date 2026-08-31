// THE CASE. A test file calling the framework's injected `describe`, which it never imports.
//
// This is what produced 32 fabricated callers. In Python the equivalent is an abstention: an
// unbound global could have been installed by another module. In an ES module it is a PROVEN
// NEGATIVE — a module-scope symbol in another file is reachable only through an import, and this
// file imports no `describe` and declares none.
import { forwardReleasedItem } from "./proxy";

describe("the proxy", () => {
  forwardReleasedItem("x");
});
