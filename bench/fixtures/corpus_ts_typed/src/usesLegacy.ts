// UNDECIDABLE for `legacyHelper` — same shape as `jestGlobals.ts`, but `globalSetup.ts` assigns
// this exact name onto `globalThis`, so the module-reachability argument no longer holds.
import { describe } from "./proxy";

export function total(): number {
  return legacyHelper() + 1;
}
