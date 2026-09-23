// The target for the class-qualified case: `StrategyChain.resolve`.
//
// This is the one shape `bench/README.md` said this corpus had never scored — a method whose LEAF
// name collides across the tree, where the class is the only thing telling the two apart. It is
// modelled on the hand-checked `bright-sky` finding: 48 reported callers, two real ones, and the
// difference visible only to a reader who noticed the discriminator was `StrategyChain` and not
// `resolve`.
//
// `resolve` is deliberate. It is the single most collided method name in TypeScript, because every
// `new Promise((resolve, reject) => ...)` binds it as a parameter — see `promiseExecutors.ts`,
// which is what a bare-name resolver reports as callers of this method.
export class StrategyChain {
  resolve(input: string): string | null {
    return input.length > 0 ? input : null;
  }
}
