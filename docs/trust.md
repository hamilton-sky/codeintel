# What an answer is worth, and how to check it in five minutes

> For someone who did not write this tool. Nothing below assumes you have read
> [architecture.md](architecture.md), and you should not need to.

codeintel answers questions about code by combining three engines. They are not equally reliable,
they fail in different ways, and — this is the part that matters — **several of those failures look
exactly like a correct answer**. This page tells you which answers to act on, which to check, and
how to check them without reading any source.

Every claim here is checked by `tests/test_docs_trust_claims.py`: the states are recognised by
conditions the code actually reports, and every command shown is parsed against the real CLI. A doc
about trust that could drift from the product would be a poor joke.

---

## 1. The trust model

One line per engine, and the line is about **what the answer is evidence of**, not how good the
engine is.

| Engine | Answers | Treat it as | Because |
|---|---|---|---|
| **LSP** | `symbol` — definitions, references | **Evidence.** Act on it. | A language server resolved a real binding. When it cannot, it now says so rather than returning an empty list. |
| **Graph**, rows marked `resolved` | `callers`, `callees`, `impact`, `chain` | **Evidence.** Act on it. | The edge was followed through an import or a language-server binding. |
| **Graph**, rows marked `name-matched` | the same ops | **A lead. Verify before acting.** | The backend matched a bare name. On a name the index does not own, that collects every call site in the repository that mentions it. |
| **Semantic** | `search` | **Discovery.** It finds candidates. | Similarity is not reachability. A high score means "reads like your query", never "calls this". |
| **Pattern** | `pattern` | **Discovery**, same as above. | It is a graph-augmented grep. |

**The same three words are on the envelope.** `evidence_class` is `evidence`, `discovery` or
`advisory` on every answered result, and it is decided by the answer rather than by the op: a
`callers` result is `evidence` only when every row followed a real binding and nothing is disclosed
missing, and `advisory` otherwise. Nothing `partial` is ever `evidence`. `impact`, `context` and
`chain` are always `advisory` — they are assembled from heuristics, and their per-row `verified`
flag is where the evidence-grade subset lives.

So the table above is now something you can branch on rather than something you have to have read:

```jsonc
// before deleting or renaming
if (env.evidence_class !== "evidence") { /* verify first — do not delete */ }
```

Two consequences worth stating on their own:

**A caller count is a count of ROWS, not of callers.** The heading tells you the split:

```text
## Callers of StrategyChain.resolve (48 direct, 2 other reference(s))
**2 resolved · 43 name-matched · 5 unstated.** The heading counts rows, not confirmed callers …
```

On that real repository the true answer is **two**. Both are in the list, and both are `resolved`.

**When name-matched rows dominate, the answer runs the check that settles them and tells you what
it found:**

```text
_Checked: **43 of 43** name-matched callers are in files that never write `StrategyChain`, the name
it is qualified by, and the part of the target the name match did not use. They are ranked last
below and carry `qualifier_seen: false`._
```

Each row carries the result as `rows[].qualifier_seen`, and `evidence.qualifier_absent` counts them.
`true` means the file does write the qualifier, `false` that it does not, and **`null` that nobody
looked** — which includes every `resolved` row, since a row that followed a real binding needs no
corroboration from a text search.

Read it as narrowing, not as a verdict, and note that the tool does not drop these rows for you.
A file can reach a method without ever naming its class — through an interface-typed field, a
subclass, or a renaming re-export — so `qualifier_seen: false` marks the rows to doubt first, not
rows proven false. The command is still printed so you can re-run it yourself.

If you want the filter, apply it: `rows.filter(r => r.qualifier_seen !== false)` is the middle
setting between taking every row and taking only `verified` ones, and `bench/README.md` measures
all three.

### Before you delete anything

`callers` returning nothing has two meanings and they are opposite: *nothing calls this*, and *the
lookup did not answer*. The envelope distinguishes them, and you must too:

- `confidence: complete` with an empty list — **asked, and there is nothing.**
- `confidence: partial` — a named part of the answer is missing. Read `gaps`. Treat the missing
  part as **unknown**, never as none.
- `result: null` with a `reason` — nothing was answered at all. `reason` says why and `hint` says
  what to do.
- `evidence.safe_for_destructive` — `true` only when there are rows, every one of them followed a
  real binding, the list is not truncated and nothing is disclosed missing. It is deliberately
  strict; if you want a looser rule, `evidence` gives you `verified` / `possible` / `unstated` and
  you can write it yourself.

If you read one thing from this page, read that list.

---

## 2. Five minutes: verify it on a symbol you already know

Do not start by asking a question you cannot check. Start with one you can.

**Pick a symbol whose callers you already know** — something you wrote, with two or three call
sites you can name from memory.

```bash
codeintel doctor --deep .
```

`--deep` puts one real query to each engine and requires an answer with content, so a green row
means the engine answered about *this* repository — not merely that a process started. Any row that
is not green prints its own fix. Fix those first; the rest of this is meaningless against a broken
engine.

```bash
codeintel query --op callers --target YourSymbol --project-root .
```

Now read it in this order:

1. **The `>` block above the heading**, if there is one. It is the whole verdict in four lines, and
   it is printed only when there is something to warn about — a clean answer starts at the heading.

   ```text
   > **Confidence: partial**
   > Verified callers: 2 · possible: 43 · unstated: 5
   > Safe for destructive decisions: **no**
   ```

2. **The heading.** Is the count what you expected? If it is much larger, look at the line below it.
3. **The `resolved · name-matched · unstated` split.** Are your known callers in the unbadged rows?
4. **The qualifier line** (`Checked: N of M …`, or `Settle it:` when the tool could not run the
   check itself). Rows marked `qualifier_seen: false` sit in files that never write the qualifier —
   doubt those first. It narrows; it does not decide.
5. **`confidence`** on the envelope (`--json` shows it). `partial` means read `gaps`.

You have verified the tool when **your known callers appear as `resolved` rows**. If they appear
only as `name-matched`, the graph is guessing about your code too, and you should prefer
`--engine lsp` for this repository.

```bash
codeintel query --op callers --target YourSymbol --engine lsp --project-root .
```

That is the whole workflow. It takes about five minutes and it is worth more than any number in
[../bench/README.md](../bench/README.md), because it is measured on your code.

---

## 3. The eight states your repository can be in

Recognise the state, then take the one command. `codeintel doctor` prints the first column for you.

### 3.1 Fully indexed and healthy

`3 / 3 engines ready`. Everything in the trust model applies as written.

### 3.2 Graph-only

`doctor` shows `lsp` and `semantic` not ready. You still get `callers`, `callees`, `impact`,
`chain`, `overview`, `hotspots`, `changed` — the whole structural surface. You lose precise
definitions/references and natural-language search. **Since LSP is the engine the trust model calls
evidence, verify more here, not less.**

```bash
codeintel setup --all .
```

### 3.3 LSP-only

`doctor` shows `graph` not ready. `symbol` works and is the most trustworthy answer this tool
produces. Every structural op safe-nulls with `reason: "engine-unavailable"`.

```bash
pip install 'codebase-memory-mcp==0.10.*' && codeintel index .
```

### 3.4 Semantic-only

Only `search` answers. This is the weakest state to reason from: search finds candidates and tells
you nothing about whether they call each other.

```bash
codeintel setup --all .
```

### 3.5 Reindexing, with a usable stale snapshot

Answers carry `reindexing: true`. They come from the last **completed** index, so they are real
answers about a slightly older tree — safe for orientation, not for "did my change land". `doctor`
reports `indexing in progress`.

```bash
codeintel index .    # indexes synchronously, with progress
```

### 3.6 Permission failure

`doctor --deep` reports `source_readable: false`, and queries safe-null with
`reason: "source-unreadable"`. Nothing here is trustworthy: the index may describe files the tool
can no longer read. On macOS this is usually the OS privacy prompt, not file modes.

```bash
codeintel doctor --deep .    # names the unreadable paths and the grant to make
```

### 3.7 Empty or unsupported repository

`doctor` shows `repo_indexed: false`, and after `codeintel index` the scan reports zero files.
codeintel **fails closed** here rather than reporting a successful index of nothing — a repository
of zero indexed files and a repository that could not be read used to be the same green row.

```bash
codeintel index .    # read its failure line; it names the cause
```

### 3.8 Partial parser coverage

The subtlest one, because every answer looks fine. Two independent causes:

- **The LSP serves the wrong language.** `.serena/project.yml` names a fixed list, and serena's own
  init writes ONE. A repository that is two-thirds TypeScript with `language_servers: [python]`
  returns empty bodies for every TypeScript symbol — not errors. `doctor` names it.
- **tree-sitter is absent.** `doctor` reports `treesitter: false`. Non-Python files fall back to
  line-window chunking, so semantic search still works but is blunter. Nothing else is affected.

```bash
codeintel setup --languages .    # adds every language this repo contains
```

---

## 4. Setting it up for someone else

```bash
pip install codecortex          # the distribution; the command is `codeintel`
codeintel setup --all .         # backends, deps, index, serena warm — idempotent
codeintel doctor --deep .       # confirm each engine ANSWERS, not just starts
codeintel install               # register with the agents on this machine
```

Then **restart the agent** — a running host does not reload its MCP config.

Two things to say to whoever you send it to, because neither is discoverable:

1. **Read `confidence` and `gaps`, and treat a caller count as a lead rather than an authority.**
   Only the `resolved` rows followed a real binding. An agent can skip the prose entirely:
   `evidence_class` says what the answer supports, and `rows[]` carries per-row `verified`,
   `strategy`, `confidence` and `why` — so it can filter instead of parse.
2. **Run §2 on a symbol you already know before trusting anything else.** Measured accuracy here is
   bimodal, and the discriminator is knowable in advance: a symbol whose leaf name is unique in the
   index resolves essentially exactly; a colliding method name in a large tree does not — and says
   so on its first line.

If they get stuck, `codeintel prompt` prints a paste-ready block of exactly the outstanding steps
for their machine, which is easier to send than a description of them.

---

## See also

- [providers-bringup.md](providers-bringup.md) — the failure mode behind each safe-null, per engine.
- [graph.md](graph.md#relationship-kind-and-how-an-edge-was-resolved) — what each relationship kind
  asserts, and what edge provenance means.
- [../bench/README.md](../bench/README.md) — the measured accuracy, and what the numbers do and do
  not cover.
