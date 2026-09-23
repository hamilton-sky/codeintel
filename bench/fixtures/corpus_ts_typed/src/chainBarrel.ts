// The barrel. `chainAgents.ts` imports `StrategyChain` through here rather than from the file that
// defines it, so the alias chain has to be followed through a stated re-export before the class a
// field is annotated with can be matched against the target — the same indirection `settleFacade.ts`
// puts in front of `settleQueue`, one level up, on the qualifier instead of on the symbol.
export { StrategyChain } from "./strategyChain";
