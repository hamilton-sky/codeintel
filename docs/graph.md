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

> ### Supported backend version: `0.9.x`
>
> ```bash
> pip install 'codebase-memory-mcp==0.9.*'
> ```
>
> **`0.10.x` does not work with this release.** It replaced the `{"columns": [...], "rows": [...]}`
> response that every renderer here parses with a compact human-readable text format. Crucially it
> kept `list_projects` as JSON — so project resolution and `codeintel doctor` still succeed, while
> **every other op returns nothing**: `callers`, `callees`, `impact`, `chain`, `pattern`,
> `overview`, `changed` and `hotspots` all come back empty against a fully indexed
> repository.
>
> As of this release `doctor` detects the mismatch by asking a real query rather than trusting
> `list_projects`, and reports `backend-incompatible` with the pin above. Queries report the same
> reason instead of `not-in-graph`, so the tool no longer makes a false claim about your index.
>
> **The backend self-updates.** `codebase-memory-mcp update` can therefore break the graph engine.
> If graph ops stop returning results, check the version first.
>
> Note also that the backend re-initialises a native runtime on every invocation — roughly **6
> seconds per call** on an ordinary machine. `codebase-memory-mcp daemon start` keeps one warm and
> removes that cost. If your machine is slower still, raise
> `CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS` (default 20000).

## Supported ops

| op | target | What it returns |
|---|---|---|
| `callers` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Up to 20 callers of the symbol (name + file path) |
| `callees` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Up to 20 functions called by the symbol |
| `impact` | symbol name, or a [disambiguated](#when-several-symbols-share-a-name) one | Combined callers + callees section |
| `chain` | `"A->B"` or symbol | Call path from A (trace_path), each hop risk-labeled when the backend classifies it |
| `pattern` | text pattern | search_code results for the pattern |
| `overview` | (ignored) | get_architecture output for the project |
| `changed` | (ignored) | Impact of the **uncommitted git worktree**: changed files → impacted symbols (via `detect_changes`) |
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

### The repo-scan ops

`changed` and `hotspots` key on the whole index / git state, not a symbol, so `target` is ignored. An
empty scan (a clean worktree, no ranked symbols) is a **true answer** and returns an informative
string, not safe-null; only a backend failure returns safe-null.

### `chain` detail

If `target` contains `"->"`, the part before `->` is used as the source for a `trace_path`
call in `calls` mode. Otherwise, `chain` falls back to `impact`. The call is made with
`risk_labels` on, so each hop carries a `[risk: …]` badge when the backend classifies it.

### `changed` detail

`changed` calls `detect_changes`, which drives a backend-side reindex of the changed files (so it
gets a higher timeout floor, 15 s). Its result is **never cached** — the content-hash cache key can't
see the live git worktree, so a cached answer would be stale. The backend returns duplicate
`changed_files` (staged + unstaged) and mixes file markers into `impacted_symbols`; the provider
dedupes the files and keeps the symbol list symbols-only.

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
| `'project-not-indexed'` | No project found for the given `project_root` |
| `'unsupported-op'` | `op` is not one of the nine ops above |
| `'op-withdrawn'` | `op` is `deadcode`, which is retired — see [above](#deadcode-is-retired) |
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

On failure `ok` is `false` and `result` is `null`.

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
