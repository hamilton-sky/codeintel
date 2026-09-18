// The fabrication bait, and the reason this case is worth scoring at all.
//
// Every function here binds `resolve` as a Promise executor's PARAMETER and calls it. None of them
// has anything to do with `StrategyChain`; none of these files mentions the class. On the repository
// this case is modelled from, functions of exactly this shape supplied 43 of 48 reported callers,
// each one badged `[?0.28]` or `[?0.55]` and each one wrong.
//
// The oracle proves them non-callers rather than abstaining: a bare `resolve` is bound right here by
// the arrow function's own parameter list, and a method is never reached as a bare name.
export function fetchLater(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!url) {
      reject(new Error("no url"));
      return;
    }
    resolve(url);
  });
}

export function settleSoon(ms: number): Promise<number> {
  return new Promise((resolve) => {
    resolve(ms);
  });
}

export function firstOf(values: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const head = values[0];
    if (head === undefined) {
      reject(new Error("empty"));
      return;
    }
    resolve(head);
  });
}
