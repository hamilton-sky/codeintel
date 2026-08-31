import { forwardReleasedItem as fwd } from "./proxy";

export const dispatch = (item: string): string => fwd(item);
