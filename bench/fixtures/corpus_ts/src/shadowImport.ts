import { forwardReleasedItem } from "./other";

export function emit(item: string): string {
  return forwardReleasedItem(item);
}
