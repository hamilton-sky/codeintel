import { settleQueue } from "./settleFacade";

export function drainB(items: string[]): number {
  return settleQueue(items) + 1;
}
