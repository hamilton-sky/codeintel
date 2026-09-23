// The `forward_released_item` case exactly: passed, never invoked. Zero calls, one reference.
import { forwardReleasedItem } from "./proxy";

export function install(bus: { onMessage: unknown }): void {
  bus.onMessage = forwardReleasedItem;
}
