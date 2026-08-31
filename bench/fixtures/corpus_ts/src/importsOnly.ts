// IMPORT — the binding site and nothing else. An import is not a caller; counting it as one is
// what `lsp_raw` does, and it is most of that arm's measured 74% precision.
import { forwardReleasedItem } from "./proxy";

export const LABEL = "forwarder";
