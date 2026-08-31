import { forwardReleasedItem } from "./facade";

export function relay(item: string): string {
  return forwardReleasedItem(item);
}
