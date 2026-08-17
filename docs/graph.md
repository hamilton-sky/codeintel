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
> `overview`, `changed`, `deadcode` and `hotspots` all come back empty against a fully indexed
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
| `callers` | symbol name | Up to 20 callers of the symbol (name + file path) |
| `callees` | symbol name | Up to 20 functions called by the symbol |
| `impact` | symbol name | Combined callers + callees section |
| `chain` | `"A->B"` or symbol | Call path from A (trace_path), each hop risk-labeled when the backend classifies it |
| `pattern` | text pattern | search_code results for the pattern |
| `overview` | (ignored) | get_architecture output for the project |
| `changed` | (ignored) | Impact of the **uncommitted git worktree**: changed files → impacted symbols (via `detect_changes`) |
| `deadcode` | (ignored) | Unreferenced non-test symbols — dead-code candidates, biggest first (via `search_graph`, in-degree 0), then **verified against the source** (see below) |
| `hotspots` | (ignored) | Highest complexity / fan-in symbols — refactor-risk hotspots (via `search_graph`, client-sorted) |

### Why `deadcode` re-reads the source

In-degree 0 means "nothing *calls* this". A function passed as a **reference** — a React event
handler, an `addEventListener` argument, any framework callback — has in-degree 0 while being
entirely live. On a real TypeScript repo the raw graph answer was 181 candidates of which every
one sampled was live code, and an agent acting on that deletes working code.

So candidates are checked against the source with a bounded word-boundary scan; a name appearing
anywhere beyond its own definition drops out. The same repo returns 4. Generated output, vendored
trees and retired directories are excluded from both the scan and the ranking — a checked-in
minified bundle otherwise supplies enough occurrences to vouch for a genuinely dead name, and
ranks as the most complex "function" in the repo.

The result says which verification actually ran: a full source check, a missing `project_root`, or
a repo past the scan cap. It is a name-frequency heuristic and errs toward hiding real dead code
rather than reporting live code as dead — it cannot see a symbol reached only through a decorator
registry, `getattr` dispatch, an object-literal property a library calls, or a name in a template,
YAML or TOML. Nor can it see an entry point declared in packaging metadata, a plugin discovered at
runtime, or a caller in a language the graph does not parse.

**Treat the output as candidates — a ranked list of places worth looking, never a work order.**
Review each hit before deleting anything, and do not wire `deadcode` into an agent that deletes
without a human in the loop. The verification removes the common false positives; it does not make
the answer complete, and no reachability analysis on this shape of input could.

The three repo-scan ops (`changed`, `deadcode`, `hotspots`) key on the whole index / git state, not a
symbol, so `target` is ignored. An empty scan (clean tree, no dead code) is a **true answer** and
returns an informative string, not safe-null; only a backend failure returns safe-null.

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
