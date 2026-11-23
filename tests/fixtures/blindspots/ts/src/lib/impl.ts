export function helper(x: number): number {
  return x + 1;
}

export function process(x: number): number {
  return helper(x) * 2;
}
