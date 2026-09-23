// The one hole in ES module reasoning: a repo can install its own global. While this assignment
// exists anywhere in the tree, a bare `legacyHelper` is no longer provably a non-reference, so the
// oracle abstains on that NAME repo-wide rather than claiming a negative it cannot support.
import { legacyHelper } from "./proxy";

globalThis.legacyHelper = legacyHelper;
