import { forwardReleasedItem } from "./proxy";

export function send(item: string): string {
  return forwardReleasedItem(item);
}
