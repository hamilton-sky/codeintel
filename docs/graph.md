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
| `deadcode` | (ignored) | Unreferenced non-test symbols — dead-code candidates, biggest first (via `search_graph`, in-degree 0) |
| `hotspots` | (ignored) | Highest complexity / fan-in symbols — refactor-risk hotspots (via `search_graph`, client-sorted) |

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
