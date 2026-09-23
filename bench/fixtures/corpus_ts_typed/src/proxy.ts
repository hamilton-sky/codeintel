// The targets. `forwardReleasedItem` is the shape that started this: only ever PASSED, never
// invoked, so an engine that reports "no callers" invites a deletion.
export function forwardReleasedItem(item: string): string {
  return item;
}

// Named after a framework global on purpose. The worst failure ever observed in this project was
// 32 fabricated callers for `describe`.
export function describe(name: string, fn: () => void): void {
  fn();
}

// Reachable through a global the repo installs itself, which is the one hole in ES module
// reasoning — see `globalSetup.ts`.
export function legacyHelper(): number {
  return 1;
}
