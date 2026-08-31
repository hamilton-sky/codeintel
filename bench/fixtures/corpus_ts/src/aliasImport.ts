// UNDECIDABLE — a path-aliased specifier with no tsconfig to resolve it. The name might be the
// target and might not; guessing either way is the failure this oracle exists to avoid.
import { forwardReleasedItem } from "@app/proxy";

export function viaAlias(item: string): string {
  return forwardReleasedItem(item);
}
