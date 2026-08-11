# LspProvider Reference

Wraps the LSP-over-MCP bridge (serena) in a background thread. Never raises — always returns
an envelope. The session warms up asynchronously; the first call(s) may return a safe null
with `reason: 'warming'` while the server is starting.

## Install prerequisite

Either `uvx` or `serena` must be on `PATH` (detected via `shutil.which`, checked in that order).
If neither is found, every call returns `ok: false` with `reason: 'engine-unavailable'`.

- Preferred: install `uvx` (`pip install uv`) — the provider runs `uvx serena`.
- Fallback: install `serena` directly and ensure the binary is on PATH.

## Supported ops

| op | target | What it returns |
|---|---|---|
| `symbol` | symbol name | Definition location + all references |
| `overview` | relative file path (or empty) | Symbols overview for the file / project |

Any other `op` value returns `ok: false` with `reason: 'unsupported-op'`.

### `symbol` detail

Calls `find_symbol` and `find_referencing_symbols` in parallel, then merges the results:

```
## Symbol: <target>
<definition text or "(not found)">

## References
<referencing symbols or "(none)">
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
If `budget` is 0 or absent, the timeout defaults to **5.0 s** (`_DEFAULT_TIMEOUT_S`).

## Safe-null reasons

| reason | When returned |
|---|---|
| `'engine-unavailable'` | Neither `uvx` nor `serena` is on PATH |
| `'warming'` | Session exists but has not finished starting |
| `'boot-failed'` | Session failed to start (cooldown active) |
| `'unsupported-op'` | `op` is not `symbol` or `overview` |
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

On failure `ok` is `false` and `result` is `null`.

## First-call behaviour

The first call for a new `project_root` always starts the session in `WARMING` state.
Expect a safe null on the first one or two calls; subsequent calls on the same root
hit the running session.
