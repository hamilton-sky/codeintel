// The collision that makes the qualifier load-bearing: a DIFFERENT class with the same method name.
// A resolver matching on `resolve` alone cannot separate this from `StrategyChain.resolve`; a reader
// of the source can, because `FallbackAgent` states which class its field holds.
export class FallbackChain {
  resolve(input: string): string {
    return input.trim();
  }
}
