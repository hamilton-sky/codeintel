import * as proxy from "./proxy";

export function viaNamespace(item: string): string {
  return proxy.forwardReleasedItem(item);
}
