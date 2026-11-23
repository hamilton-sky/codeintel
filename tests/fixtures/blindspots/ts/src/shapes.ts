export interface Shape {
  area(): number;
}

export class Circle implements Shape {
  constructor(private r: number) {}

  area(): number {
    return 3.14159 * this.r * this.r;
  }
}

export class Square implements Shape {
  constructor(private side: number) {}

  area(): number {
    return this.side * this.side;
  }

  describe(): string {
    return `square of area ${this.area()}`;
  }
}

export function onlySquare(sq: Square): number {
  return sq.area();
}

export function totalArea(shapes: Shape[]): number {
  let sum = 0;
  for (const s of shapes) {
    sum += s.area();
  }
  return sum;
}

export function outer(): number {
  function inner(): number {
    return 1;
  }
  return inner();
}

export function inner(): number {
  return 2;
}
