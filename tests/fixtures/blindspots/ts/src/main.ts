import { runProcess, helper as h } from "./lib";
import * as im from "./lib/impl";

export function entry(): void {
  runProcess(1);
  h(2);
  im.process(3);
}
