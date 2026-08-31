// A DIFFERENT `forwardReleasedItem`. Importing this one is not importing the target.
export function forwardReleasedItem(item: string): string {
  return item.toUpperCase();
}
