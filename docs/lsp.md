# LspProvider Reference

Wraps the LSP-over-MCP bridge (serena) in a background thread. Never raises — always returns
an envelope. The session warms up asynchronously, and a call that arrives mid-boot **waits** for
it — bounded, and only for as long as the boot plausibly needs (see [Warming](#warming)). A boot
that outlasts the bound still returns a safe null with `reason: 'warming'`.

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

## A booted server is not a serving server

serena takes **one config per project**, and it names a fixed list of language servers. On a polyglot
repository that list is routinely narrower than the tree:

```yaml
# .serena/project.yml
language_servers:
  - typescript        # …beside 69 Python files
```

Every Python `symbol` query against that repo returns an empty body with a
`references / not-asked` gap — not an error, just nothing — while the process itself booted fine. The
doctor used to report `lsp: ok / reached READY`, which was true about the process and false about
every answer it would give. Green while the thing it certifies serves nothing is the worst shape a
health check can take.

`probe()` now compares the configured list against a census of the repo's own files (vendored
directories excluded, and a floor of 5 files so one stray `setup.py` beside a TypeScript app does not
turn the engine red). An unserved language makes the engine **not runnable** and names itself, its
file count, the symptom and the fix:

```
└─ lsp: serena booted via `uvx` and reached READY — but .serena/project.yml serves only
   typescript, so python (69 files) get NO answer from this engine (empty `symbol` results,
   not errors)
   fix: add the missing language(s) to `language_servers:` in .serena/project.yml and re-run,
   or use `--engine graph` for python symbols
```

Only the config is authoritative about what is served, so with no `.serena/project.yml` this check
stays silent rather than guessing.

### Fixing it: `codeintel setup --languages`

The check above knows the census, the config path and the exact list to write — so it can do the fix
rather than describe it. serena runs **several language servers in parallel** (*"the first language
server that supports a given file will be used"*, from the config's own comments), so the whole defect
is one missing line of YAML.

```bash
codeintel setup .              # reports what it WOULD add, writes nothing
codeintel setup . --languages  # adds them, then verifies by re-reading the file
```

Measured on a real repo: `pathly-adapters` had `language_servers: [python]` against **771 TypeScript
files** and 418 Python. One flag, and the 771 become answerable.

Five rules it follows, each of which is a way this could go wrong:

| rule | why |
|---|---|
| Runs only as a `setup` step, never at query time | Mutating a user's config as a side effect of asking a question is wrong, and a language server needing an extra install would then fail a query that used to work. `--all` includes it; without the flag it reports and names the flag. |
| `c` is written as **`cpp`** | serena's accepted ids are `cpp`/`cpp_ccls` with **no bare `c`**, while codeintel's census language for `.c`/`.h` is `c`. Writing the census value through would emit `- c` and break serena's startup for a repo that had been working. Anything not in the map is skipped and reported — serena says its id list "may be outdated", so drift fails closed. |
| Existing entries are never reordered | The first entry is the default and the fallback, so re-sorting by file count could change which server answers for an ambiguous file. Missing languages are appended, most-populous first. |
| A language under the 5-file floor is recorded, not added | Every entry is a server serena will **boot**. One stray `.ts` file should not cost a whole TypeScript server's startup on every session. |
| No config, or an unparseable one, is refused | A bare file written by us would skip serena's own comments and setup notes; editing a file we could not parse is how a config gets corrupted. |

The write is atomic (temp file, then `os.replace`) and confirmed by re-reading through the same parser
the check uses — a half-written `project.yml` would take this engine from one language to none, which
is worse than the defect being fixed. It is also idempotent, so running it again does nothing.

## References are not calls

`find_referencing_symbols` returns **references**, and a reference is not a call. On one measured
symbol, 9 references comprised 5 call sites, 2 import lines, and 2 duplicate rows. Across a
stratified Python set, taking every reference as a caller scores **65% precision** against **100%**
once each site is classified by the syntax at its line — the whole of that gap is references that
are not calls ([../bench/README.md](../bench/README.md), which is where the current numbers live;
this sentence quoted 74% from a run that predated proven-negative truth).

This is why the gateway's cross-check **appends** the LSP reference list next to the graph's answer
rather than replacing it: the two engines answer related but different questions, and presenting one
as the other would substitute a new over-claim for the one it was correcting.

## Session state machine

Each `project_root` gets its own `_LspSession`. State transitions:

```
IDLE ──start──► WARMING ──warmup ok──► READY
                    │
                    └──warmup fail──► FAILED ──60 s cooldown──► (retry on next call)
```

- **WARMING** — server is starting. A call waits up to `_WARM_WAIT_S` for the boot to settle
  (see [Warming](#warming)) and returns a safe null with `reason: 'warming'` only if it does not.
- **READY** — server is up; queries are dispatched.
- **FAILED** — warmup threw; calls return safe null with `reason: 'boot-failed'` until
  the 60-second cooldown expires, then the session is discarded and recreated on the next call.

The cooldown period is **60 seconds** (`_COOLDOWN_SECONDS = 60`).

## Warming

A `WARMING` session is waited for rather than declined outright. Returning `'warming'` the instant
a session was booting made the engine effectively unavailable on the **first call of every
session** — which is the call an agent makes when it starts work on a repo. It also silently
disabled the cross-check behind `callers`/`context`'s `[?…]` unverified badges: those badges tell
the reader to confirm against the LSP, and the LSP had always just declined.

The wait lives in the provider, so **every transport inherits it**. It previously lived in
`commands/query.py` as a retry loop, which meant the CLI had it and the MCP and HTTP transports —
the ones an agent actually calls — did not.

The bound is **8.0 s** (`_WARM_WAIT_S`), additionally clamped to the caller's own budget. It is
sized from measurement: the `initialize` handshake settled in 2.30 s, 2.50 s and 1.85 s across a
70-file repo, an 803-file TypeScript repo and a mixed-language repo — flat with repo size, because
the handshake does not load the workspace. That load is the ~11.65 s the dispatch timeout below is
sized for, and it happens on the far side of `READY`, so the two budgets do not overlap.

The bound is a ceiling, not a cost: the wait ends the moment the session settles, and a session
already `READY` never waits at all. A boot that genuinely exceeds it — a cold `uvx` still resolving
and downloading `serena-agent` — degrades to the same safe null as before, with `retry_after_s` in
the envelope's `gaps`.

Sessions signal through a `threading.Event` set on **both** `READY` and `FAILED`, so a waiter is
never stranded on a session that has already settled.

## Budget / timeout

`budget` (milliseconds) is converted to seconds for the async call timeout.
If `budget` is 0 or absent, the timeout defaults to **30.0 s** (`_DEFAULT_TIMEOUT_S`).

## Safe-null reasons

| reason | When returned |
|---|---|
| `'engine-unavailable'` | Neither `serena` nor `uvx` is on PATH |
| `'warming'` | Session did not finish starting within `_WARM_WAIT_S` (8 s) of the call |
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
