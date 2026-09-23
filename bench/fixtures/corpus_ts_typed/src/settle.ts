// The target for the "every caller is a guess" case. Nothing imports this file directly except the
// re-export beside it, so every call site reaches the symbol through `settleFacade.ts` — and a
// re-export chain is where the backend's import cascade stops and bare-name matching takes over.
//
// The name is unique in this corpus ON PURPOSE. `describe` already covers a collision, and mixing
// the two would leave it unclear which mechanism produced the guess. Here the only variable is the
// indirection.
export function settleQueue(pending: string[]): number {
  return pending.length;
}
