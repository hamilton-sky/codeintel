// UNDECIDABLE — a property access on a value. The receiver's type is not a syntactic fact, and
// this is the abstention class that TypeScript shares with Python.
import { describe } from "./proxy";

export class Relay {
  constructor(private bus: { forwardReleasedItem: (s: string) => string }) {}

  go(): string {
    return this.bus.forwardReleasedItem("q");
  }
}
