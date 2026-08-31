# GraphProvider Reference

Wraps the `codebase-memory-mcp` CLI binary. Never raises — always returns an envelope.

## Install prerequisite

`codebase-memory-mcp` must be on `PATH` (detected via `shutil.which`). If absent, every
call returns a **safe null** with `reason: 'engine-unavailable'` — `ok` is still `true`; the
contract never returns `ok: false`.

`codebase-memory-mcp` is a standalone, platform-specific binary distributed by its own project —
install the build for your OS/arch and ensure it is on `PATH` (it self-manages via
`codebase-memory-mcp install|update`). Run `codeintel doctor` to confirm it is detected and that
this repo is indexed.

> ### Supported backend versions: `0.9.x` and `0.10.x`
>
> Both are read. They speak **different wire formats** and `codeintel` translates between them at a
> single seam (`BackendClient._decode` → `codeintel/wire_text.py`), so no op above the transport
> knows there are two:
>
> | backend | `query_graph` reply | notes |
> |---|---|---|
> | `0.9.x` | `{"columns": [...], "rows": [...]}` JSON | the original dialect |
> | `0.10.x` | a compact human-readable text layout | `list_projects` alone stayed JSON |
>
> **Prefer `0.10.x`.** Measured over the same three repositories, the share of `CALLS` edges below
> the 0.85 confidence floor falls from **24% / 33% / 43%** to **9% / 18% / 30%**, and Python
> enclosing-function attribution improves from ~32% of production caller rows collapsing to
> `module scope of <file>` to **2.7%**.
>
> ```bash
> pip install 'codebase-memory-mcp==0.10.*'
> ```
>
> **The 0.10.x text layout is not a contract** — it is output meant for humans and can change in a
> patch release. Every parser in `wire_text.py` therefore refuses rather than guesses: an
> unrecognised shape returns `None` and the op safe-nulls with `backend-incompatible`. A wrong answer
> assembled from a format we no longer understand would be worse than that refusal, which is the only
> thing that made the original `0.9 → 0.10` break diagnosable instead of looking like an unindexed
> repository.
>
> **Two operational traps.**
>
> - `codebase-memory-mcp update` **deletes every project index before** checking it can proceed, and
>   on a non-TTY then fails with `variant selection requires a terminal` — destroying the indexes for
>   nothing. Pass `--standard`.
> - Indexes are **not portable across `0.9`/`0.10`**. After switching, delete the repo's `.db` under
>   `~/.cache/codebase-memory-mcp/` and re-index.
>
> The backend re-initialises a native runtime on every invocation — roughly **6 seconds per call**,
> and `0.10.x` spawns a temporary daemon per CLI call unless one is running.
> `codebase-memory-mcp daemon start` keeps one warm and removes that cost; without it a long test run
> can flake on contention. If your machine is slower still, raise
> `CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS` (default 20000).

## Supported ops

| op | target | What it returns |
|---|---|---|
| `callers` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Up to 20 callers of the symbol (name + file path) |
| `callees` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Up to 20 functions called by the symbol |
| `impact` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Combined callers + callees section |
| `context` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Alias for `impact` — the graph's contribution to the `context` fan-out |
| `chain` | `"A->B"` or symbol | Call path from A (trace_path). Each hop carries how it was **resolved** (`[lsp]`, `[import]`, `[?name-guess]`), and the walk follows `CALL_REFERENCE` as well as `CALLS` |
| `pattern` | text pattern | search_code results for the pattern |
| `overview` | (ignored) | get_architecture output for the project |
| `changed` | (ignored) | Impact of the **uncommitted git worktree**: changed files → impacted symbols (via `detect_changes`) |
| `changes` | (ignored) | Alias for `changed` |
| `deadcode` | (ignored) | **Retired.** Always safe-nulls with `reason: "op-withdrawn"` — see below. |
| `hotspots` | (ignored) | Highest complexity / fan-in symbols — refactor-risk hotspots (via `search_graph`, client-sorted) |

### When several symbols share a name

`callers`, `callees` and `impact` resolve the target by its **unqualified name**, so a repository
with four methods called `invoke` matches all four. Rows are reported **separately per matched
symbol**, under a heading naming it, with the count of same-named symbols stated — they are that many
separate answers, not one. Nothing is dropped for being ambiguous. `callees` groups by the symbol
doing the calling and `callers` by the symbol being called; in both cases the heading is the symbol
your target matched and the rows are the other end of the edge.

To ask about one of them, qualify the target with text the answer already printed:

| target | means |
|---|---|
| `invoke` | every symbol named `invoke` |
| `core.Group.invoke` | the one whose qualified name ends in those segments |
| `invoke@src/click/testing.py` | the one defined in that file (a bare `testing.py` works too) |

A qualified or file-hinted target that matches nothing says so and lists the symbols that **do**
carry the name. It never reports zero rows, because "I could not find the symbol you named" and
"that symbol calls nothing" are opposite answers and only one of them is about your code. Note what
that message does and does not claim: a symbol can be indexed and still be absent from one of these
lists — `Group.invoke` has callees but no callers on one real repository — so it says "no symbol
matching this has callers here", never "not in this index".

Two things still limit these ops, and both are disclosed in the result rather than assumed away:

* The extractor emits edges for bare local names, so a callee in a different language family than
  its caller, or in a file that cannot hold code at all, is dropped as a name collision — reported
  as a count in the body and a `name-collisions-dropped` gap.
* The query is capped at 50 rows. A result that came back at the cap is truncated, says so, and
  carries a `row-cap-reached` gap.

### `deadcode` is retired

`deadcode` is retired (`_WITHDRAWN_OPS` in `graph.py`): it always returns a safe-null with
`reason: "op-withdrawn"` and a hint naming the substitute, and **there is no implementation left to
enable** — the `CODEINTEL_ENABLE_UNVERIFIED_OPS` opt-in that used to run it has been removed with it.

It was withdrawn pending a labelled-corpus measurement of its precision and recall. That corpus is
`tests/test_corpus.py::test_deadcode_precision_and_recall_are_measured_not_assumed`, and the
measurement retired the op: **25% precision as shipped**, and on real code with the harness's own
canaries removed it named 18 candidates across two pinned Python repositories of which **every one was
live**. Repaired as far as this codebase's existing filters reach, it named exactly one candidate, and
that one was live too. The README carries the full numbers and the reasoning:
[`deadcode` is retired](../README.md#deadcode-is-retired).

**Use `callers` on a specific symbol instead.** It answers the same underlying question — "does
anything call this?" — accurately, one symbol at a time.

### Relationship kind, and how an edge was resolved

Two independent axes, and conflating them was this engine's most consequential defect. They are
reported separately because they license different actions.

**Kind — what the edge asserts.** `callers`/`callees` match `CALLS|USAGE|CALL_REFERENCE`:

| kind | what it means |
|---|---|
| `CALLS` | invoked directly |
| `USAGE` | referenced, not called — module scope, or a mention that is not a call site |
| `CALL_REFERENCE` | **passed as a value or registered as a callback** — never invoked here |

Every row is badged with its kind, direct calls sort first, and the heading splits them whenever an
answer mixes kinds (`Callers of X (0 direct, 2 other reference(s))`). An answer made entirely of
`CALLS` keeps its plain count, so the common case is unchanged.

Why it matters: `set_forward_fn(app.forward_released_item)` registers a method at two real sites. The
backend stored both correctly as `CALL_REFERENCE`; codeintel queried only `CALLS|USAGE` and answered
*"no callers"* — the reading that deletes a live method. No confidence threshold could have recovered
it, because the edge existed at full confidence under a kind nothing asked for.

**Provenance — how the target was resolved.** The backend stamps every edge with `c.strategy`, and
that, not a numeric score, decides how a row is presented:

| class | strategies | shown as |
|---|---|---|
| resolved | `lsp_*`, `import_map`, `same_module` | no badge |
| **name guess** | `unique_name`, `suffix_match`, `heuristic`, fuzzy | `[?0.75]` and counted in a note |

A guessed row is kept, never silently dropped — dropping it would trade a false positive for a false
negative, and "no callers" is the more dangerous of the two when the next action is a delete.

The strategy is read rather than inferred from `c.confidence` because the two do not line up: on a
real repository `unique_name` appears at **both 0.75 and 0.38**, so a numeric floor splits one
strategy across two tiers and describes the same evidence two different ways. The float survives only
as a fallback for a backend that reports no strategy.

**The collision signature.** When *every* row in an answer of five or more is a name guess, the op
raises the `all-rows-name-resolved` gap — that is the shape of a name the index does not own (a
library function, a test-runner global, a builtin method) collecting every call site that mentions
it. The gateway escalates on exactly that condition and appends the language server's reference list
for comparison. It is the difference between `callers describe` returning 32 fabricated rows at
`confidence: "complete"` and returning them badged, counted, and next to the one real caller the
graph could not bind.

Measured accuracy for these answers is in [../bench/README.md](../bench/README.md) — with the
caveat that its numbers are **Python**. The `describe` failure above is TypeScript, and while a
TypeScript arm now exists, it has not been pointed at a real TypeScript repository, so no
measurement in that table speaks to the case this section describes.

### The repo-scan ops

`changed` and `hotspots` key on the whole index / git state, not a symbol, so `target` is ignored. An
empty scan (a clean worktree, no ranked symbols) is a **true answer** and returns an informative
string, not safe-null; only a backend failure returns safe-null.

### `chain` detail

If `target` contains `"->"`, the part before `->` is used as the source for a `trace_path`
call in `calls` mode. Otherwise, `chain` falls back to `impact`.

Two arguments matter:

- **`edge_types: [CALLS, CALL_REFERENCE]`** — so a walk does not stop dead at the point a function is
  handed to something rather than invoked.
- **`include_evidence: true`**, which replaced `risk_labels`. The backend treats those two as
  mutually exclusive, and nothing was lost: `risk` was a restatement of hop distance
  (hop 1 = `CRITICAL`, hop 2 = `HIGH`, hop 3 = `MEDIUM`) that every row already prints as `[hop N]`,
  so it dressed a visible number as an assessment nobody made. Evidence is the fact `chain` could not
  report before — a `[?name-guess]` hop makes everything downstream of it suspect, and used to be
  indistinguishable from a resolved one.

### `changed` detail

`changed` calls `detect_changes`, which drives a backend-side reindex of the changed files (so it
gets a higher timeout floor, 15 s). Its result is **never cached** — the content-hash cache key can't
see the live git worktree, so a cached answer would be stale. The backend returns duplicate
`changed_files` (staged + unstaged) and mixes file markers into `impacted_symbols`; the provider
dedupes the files and keeps the symbol list symbols-only.

The answer has **three** sections, and the middle one is not what its old heading claimed:

1. **Changed files** — the uncommitted source files, non-source dropped but counted (a tree of only
   `.md` edits must not report as "clean").
2. **The backend's symbol list**, whose meaning differs by dialect: `0.9.x` returns the symbols
   *defined in* the changed files (containment), `0.10.x` returns a *transitive* impacted set stamped
   with a `hop`. Same field, two meanings — so the heading is derived from the data and prints the hop
   rather than asserting the older reading, which had filed symbols from three unrelated files under
   "defined in the changed files".
3. **Callers elsewhere that reach into them** — the actual blast radius, computed here rather than by
   the backend: symbols *outside* the changed files with a `CALLS`, `CALL_REFERENCE` or `USAGE` edge
   into them, one row per calling symbol at its strongest claim.

`changed` answers a **recall** question — "what should I look at before committing" — so the
asymmetry runs opposite to `callers`: indirect edges are included rather than withheld, ranked below
direct calls, and every row labelled. Under-reporting impact is how live code gets broken;
over-reporting costs a reader one line. An empty ripple says so out loud, because an absent section
and an empty one read identically to a model and only one of them is a claim.

## Project resolution

Before any query the provider calls `list_projects` to find a project whose `root_path` matches
or is a prefix of `project_root`. The result is cached per `project_root` for the lifetime of
the provider instance.

## Budget / timeout

`budget` (milliseconds) sets the subprocess timeout. If `budget` is 0 or absent, the timeout
defaults to **5000 ms**.

## Safe-null reasons

| reason | When returned |
|---|---|
| `'engine-unavailable'` | `codebase-memory-mcp` not on PATH |
| `'backend-unreachable'` | The backend did not respond in time while resolving the project |
| `'project-not-indexed'` | No project found for the given `project_root` |
| `'project-not-indexed-standalone'` | The repo isn't indexed on its own — it only resolves via a containing ancestor project, and the op (`overview`/`changed`/`changes`/`hotspots`) is scoped to the repo boundary, so it refuses rather than answer for the wrong tree |
| `'unsupported-op'` | `op` is not one of the ops listed above |
| `'op-withdrawn'` | `op` is `deadcode`, which is retired — see [above](#deadcode-is-retired) |
| `'not-in-graph'` | The op ran and the target genuinely is not in the index — a stale index, a typo, or a rename |
| `'no-edges'` | The target **is** indexed and simply has no edge of this kind. A different fact from the row above, and it licenses the opposite action: framework-dispatched handlers (routes, ASGI apps) look exactly like this, so it must not be read as dead code. The hint names where the symbol is defined and censuses the relationships that *do* point at it. Re-indexing will not change it |
| `'backend-incompatible'` | The reply matched **neither** supported dialect — most often a backend newer than this codeintel. Upgrade codeintel first; failing that, pin `codebase-memory-mcp==0.10.*` |
| `'timeout'` / `'backend-error'` / `'unparsable'` | Returned dynamically (as `reason: miss.kind`) when a backend call inside the op itself timed out, errored, or returned something unreadable — distinct from `not-in-graph`, which means the call succeeded and the target genuinely isn't there |
| `'error'` | Unexpected exception during execution |

## Envelope shape

```json
{
  "ok": true,
  "op": "callers",
  "target": "build_result",
  "result": "## Callers of build_result\n- gateway (src/codeintel/gateway.py)",
  "engine": "graph",
  "cached": false
}
```

On failure `ok` stays `true`; `result` is `null` and `reason` carries the failure.

## Example CLI call (direct, bypassing the gateway)

```bash
codebase-memory-mcp cli query_graph '{
  "project": "codeintel",
  "query": "MATCH (caller)-[:CALLS]->(fn) WHERE fn.name=\"build_result\" RETURN caller.name, caller.file_path LIMIT 20"
}'
```

```bash
codebase-memory-mcp cli search_code '{"project": "codeintel", "pattern": "safe_null_result"}'
```
