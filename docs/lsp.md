# LspProvider Reference

Wraps the LSP-over-MCP bridge (serena) in a background thread. Never raises — always returns
an envelope. The session warms up asynchronously; the first call(s) may return a safe null
with `reason: 'warming'` while the server is starting.

## Install prerequisite

A directly-installed `serena` or `uvx` (from [uv](https://github.com/astral-sh/uv)) must be on
`PATH` (detected via `shutil.which`, checked **serena first, then uvx**). If neither is found,
every call returns a **safe null** with `reason: 'engine-unavailable'` — `ok` is still `true`; the
contract never returns `ok: false`.

- Preferred: install `serena` on `PATH` directly — the provider runs
  `serena start-mcp-server --context ide-assistant --enable-web-dashboard false --project <root>`.
- Fallback: install `uvx` (`pip install uv`) — most users land here, since serena isn't
  pip-installable. The provider then fetches and runs serena from source:
  `uvx --from git+https://github.com/oraios/serena serena start-mcp-server --context ide-assistant --enable-web-dashboard false --project <root>`.
  The **first** launch pulls serena via `uvx` and can take tens of seconds; later launches are fast.

## Supported ops

| op | target | What it returns |
|---|---|---|
| `symbol` | symbol name | Definition (with body) + all references |
| `overview` | relative file path | Symbols overview for the file |
| `context` | symbol name | Alias for `symbol` — the LSP's contribution to the `context` fan-out |

Any other `op` value returns a safe null with `reason: 'unsupported-op'` (`ok` stays `true`).

### `symbol` detail

Two-step — serena's `find_referencing_symbols` requires the symbol's own `relative_path`, so it
cannot run until `find_symbol` has located the symbol:

1. `find_symbol` with `name_path_pattern: <target>`, `include_body: true` — locates the symbol
   and returns its definition + body.
2. `find_referencing_symbols` with the located `name_path` **and** `relative_path` — returns
   every reference, with the line each appears on.

```
## Symbol: <target>
**<kind>** — <relative_path>:<start>-<end>
```<body>```

## References (<n>)
- <file>:<line>  (<referencing symbol>)
```

### `overview` detail

Calls `get_symbols_overview` with `relative_path: target`. Pass an empty string for a
project-level overview.

## Session state machine

Each `project_root` gets its own `_LspSession`. State transitions:

```
IDLE ──start──► WARMING ──warmup ok──► READY
                    │
                    └──warmup fail──► FAILED ──60 s cooldown──► (retry on next call)
```

- **WARMING** — server is starting; calls return safe null with `reason: 'warming'`.
- **READY** — server is up; queries are dispatched.
- **FAILED** — warmup threw; calls return safe null with `reason: 'boot-failed'` until
  the 60-second cooldown expires, then the session is discarded and recreated on the next call.

The cooldown period is **60 seconds** (`_COOLDOWN_SECONDS = 60`).

## Budget / timeout

`budget` (milliseconds) is converted to seconds for the async call timeout.
If `budget` is 0 or absent, the timeout defaults to **30.0 s** (`_DEFAULT_TIMEOUT_S`).

## Safe-null reasons

| reason | When returned |
|---|---|
| `'engine-unavailable'` | Neither `serena` nor `uvx` is on PATH |
| `'warming'` | Session exists but has not finished starting |
| `'boot-failed'` | Session failed to start (cooldown active) |
| `'backend-error'` | The session is up, but the language server reported an error for this call |
| `'unsupported-op'` | `op` is not `symbol`, `overview`, or `context` |
| `'error'` | Unexpected exception during execution |

## Envelope shape

```json
{
  "ok": true,
  "op": "symbol",
  "target": "build_result",
  "result": "## Symbol: build_result\n...\n\n## References\n...",
  "engine": "lsp",
  "cached": false
}
```

On failure `ok` stays `true`; `result` is `null` and `reason` carries the failure.

## First-call behaviour

The first call for a new `project_root` always starts the session in `WARMING` state.
Expect a safe null on the first one or two calls; subsequent calls on the same root
hit the running session.
