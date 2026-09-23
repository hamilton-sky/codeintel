// The two sides of the qualified case, in the shape `bright-sky` actually writes it: a field whose
// class is STATED — `private readonly chain: StrategyChain` — and then called as `this.chain.resolve`.
//
// `StrategyAgent.route` is a true caller. `FallbackAgent.route` is a PROVEN non-caller: same method
// name, same `this.chain.` spelling, different declared class. The second one is the point. Without
// it the arm could only reward finding callers, and an engine that answers "everything" would score
// the same as one that answers correctly.
import { StrategyChain } from "./chainBarrel";
import { FallbackChain } from "./fallbackChain";

export class StrategyAgent {
  private readonly chain: StrategyChain;

  constructor() {
    this.chain = new StrategyChain();
  }

  route(q: string): string | null {
    return this.chain.resolve(q);
  }
}

export class FallbackAgent {
  constructor(private readonly chain: FallbackChain) {}

  route(q: string): string {
    return this.chain.resolve(q);
  }
}
