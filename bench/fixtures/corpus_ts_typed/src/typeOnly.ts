// REFERENCE — a type-space mention. Not a call, but a real dependency: rename the symbol and this
// breaks, so it belongs in change impact even though it belongs nowhere near `callers`.
import { forwardReleasedItem } from "./proxy";

export type Forwarder = typeof forwardReleasedItem;
