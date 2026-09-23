import { describe } from "./proxy";

export function handler(bus: { make: () => (s: string) => string }): string {
  const forwardReleasedItem = bus.make();
  return forwardReleasedItem("y");
}
