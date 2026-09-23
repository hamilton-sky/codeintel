import { describe } from "./proxy";

export function dispatch(forwardReleasedItem: (s: string) => string): string {
  return forwardReleasedItem("z");
}
