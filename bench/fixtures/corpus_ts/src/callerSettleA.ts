import { settleQueue } from "./settleFacade";

export function drainA(pending: string[]): number {
  return settleQueue(pending);
}
