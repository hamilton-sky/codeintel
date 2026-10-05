# Doctor and status — what the fields mean

`codeintel doctor`, the `code.doctor` MCP tool, `POST /code/doctor`, `codeintel status`, the
`code.status` MCP tool and `GET /code/status` are one report seen through five doors. This page is the
contract for it: what each field claims, what it deliberately does not, why the graph engine is
optional, what `--deep` asserts, how a failing graph backend is classified, and the one recovery
procedure that has a known fix.

The report is built by `run_doctor` (`src/codeintel/doctor.py`). `status` is a thinner view of the
same probes (`server.py`, `_code_status_handler_inner`) — it adds no check of its own.

## The fields

Each engine (`graph`, `lsp`, `semantic`) is probed independently and produces one row:

| Field | Meaning |
|---|---|
| `installed` | The engine's external dependency is present: a binary on `PATH` (graph, lsp) or an importable package (semantic). `null` means the probe itself raised and nobody could tell. |
| `runnable` | It was started and did not fail. `null` is "installed, readiness not verified" — the LSP session has not warmed, a deep boot ran out of time, or a deep verification query could not be asked. |
| `repo_indexed` | THIS repository is indexed by this engine. `null` for LSP, which keeps no persistent index. |
| `status` | The roll-up of the three: `ok`, `warn` or `fail` — see below. |
| `detail` / `remediation` | One sentence on what was found, and the one thing to do about it (or `null`). |

`status` is `fail` when the engine is not installed, is not runnable, or (graph, semantic) the repository
is not indexed — each of which has a remedy. It is `warn` when the engine is installed but readiness is
unknown (`runnable: null`, or semantic weights not yet downloaded). It is `ok` otherwise. The rule is
`doctor._status_for`.

Around the rows:

| Field | Meaning |
|---|---|
| `summary.ready` / `summary.total` | A literal count: how many of the three engines have `status != "fail"`, out of three. It is **not** filtered for optional engines, so it is the honest "2 / 3" even when `healthy` is true. |
| `summary.healthy` | Every **non-optional** engine has `status != "fail"`. This answers one question — *can this repository be worked on?* — and ignores the graph engine on purpose (see below). `codeintel doctor` exits `0` when it is true and `1` when it is not. `code.status` repeats it as top-level `healthy`. |
| `degraded` | The engines that are **installed and failing** (`installed is true` and `status == "fail"`), optional ones included, in `graph`, `lsp`, `semantic` order. Always present; `[]` when the report looked and nothing is failing, and `null` when there is no report to read it from — the `code.status` / `code.doctor` fallbacks for an unexpected error or a role that may not run doctor. `null` is "could not tell", which is not the same statement as "none". |
| `versions`, `version_skew`, `treesitter`, `registrations` | Diagnostics around the engines; unrelated to the verdicts above. |

### `healthy` is not "everything works"

`healthy` ignores the graph engine, so it cannot distinguish two states that need opposite responses:

| State | `graph.installed` | `graph.status` | `healthy` | `degraded` |
|---|---|---|---|---|
| No graph backend on this machine | `false` | `fail` | `true` | `[]` |
| A graph backend that refuses to run | `true` | `fail` | `true` | `["graph"]` |

The first is fine — the tool is fully usable on `semantic` + `lsp`. The second is not: the backend is
there, every graph question will answer nothing, and a report that said only `healthy: true` told the
reader the opposite. `degraded` is the field that separates them. It is derived from the per-engine
`status` (`doctor.degraded_engines`), so it can never disagree with a row, and it never changes
`healthy` — `degraded` is for the reader, not a new way to fail a pipeline.

"Failing" is the row's own `status == "fail"`, so it includes an installed engine whose repository is
simply not indexed yet (`repo_indexed: false`) — the engine cannot answer for this repository, and the
fix is the `codeintel index` the row already names. It is not a claim that the backend is broken; the
`detail` says which it is.

The human output says it in words: `codeintel doctor` and `codeintel status` print
`healthy, but graph degraded — installed, but not working for this repo`. When the repository is not
healthy the line reads `semantic degraded …` without the `healthy, but` lead.

### The top-level `graph` / `lsp` / `semantic` booleans mean *installed*

`code.status` carries `graph`, `lsp` and `semantic` as top-level booleans. They have always meant
**installed**: a binary on `PATH`, a package that imports. They do not mean usable, and they were
reported as `true` next to a graph backend that exited with an error. For "usable" read
`readiness.<engine>.status` and `readiness.<engine>.runnable`; for "installed and failing" read
`degraded`. `engines` (the list) is likewise the installed ones. The booleans keep their meaning
because callers rely on it — the fix is a second field, not a redefinition of the first.

### Why the graph engine is optional

The graph engine needs `codebase-memory-mcp`, a native binary distributed by its own project, which
`codeintel` cannot install for you. A machine without it still has semantic search, LSP symbols and
references, and the `symbol`/`search` ops — so a missing graph binary must not turn the repo "unhealthy"
or fail CI. That reasoning is about the binary being **absent**. It is not an argument for staying quiet
when it is present and broken, which is what `degraded` exists to say.

## Booting is not answering

Every engine can be *started* by a check that never asks it anything — and the project's recurring
failure is a health check that stopped there. A graph registration with zero nodes answers
`list_projects` and the wire-format probe perfectly, then answers every real question with nothing. A
semantic index can hold chunks and no vectors. A language server reaches `READY` and returns nothing for
this repository's code. In each, `runnable: true` was true of the process and false of the answer.

So the shallow doctor (the default, about three seconds, no side effects) asks *installed? runnable? is
this repo indexed?*, and `--deep` asks **a real question and requires content in the reply**:

| Engine | What `--deep` asserts |
|---|---|
| graph | A real `query_graph` against the resolved project returns at least one row. A project that resolves and holds nothing is `runnable: false`; a verification query that could not complete is `runnable: null` ("unknown") — never reported as an empty answer. |
| lsp | A language-server session boots, reaches `READY`, and a real `get_symbols_overview` for a source file in a served language returns content — not merely that the process is up. |
| semantic | This repository's chunks have vectors behind them, reached through the same extension a search loads. It does **not** load the embedding model; a diagnostic that downloads 50 MB is one people stop running. |

`--deep` is read-only. It is slower, and the LSP boot is the slow part.

## When the graph backend is installed and fails

The graph engine speaks to `codebase-memory-mcp` as a subprocess (`graph_backend.py`). A call that
produced no answer is classified by what the process *did*, and the answer to "why" is kept — it used to
be thrown away, and every cause was reported as "failed/timed out".

| Cause | How it is recognised | What the doctor row says | `Missing.kind` |
|---|---|---|---|
| **Not installed** | Nothing on `PATH` at start-up (`installed: false`), or the binary vanished before the call (`FileNotFoundError`) | `codebase-memory-mcp not found on PATH`; a binary that vanished later: `not installed, or is no longer on PATH` | `backend-error` |
| **Refused** | A non-zero exit inside the time budget | ``refused to run `list_projects` (exit N): <the backend's own message>`` | `backend-error` |
| **Timeout** | `subprocess.run`'s timeout expired | ``did not answer `list_projects` within Ns`` | `timeout` |
| **Unparsable** | Exit 0, and the reply is neither JSON (0.9.x) nor the 0.10.x text layout | ``answered `list_projects`, but in a form this release cannot read`` | `unparsable` |
| **Could not launch** | The file exists and the OS will not run it (no execute bit, a corrupt binary) | ``could not answer `list_projects`: the graph backend could not be launched: <reason>`` | `backend-error` |

"Timed out" is claimed only for a timeout. The old code inferred it from the shared deadline having
passed, so a backend that exited in a second was reported as one that never answered.

In a query envelope the kind becomes the `reason` — with one rename. Every query begins by resolving
the project through `list_projects`, and a failure *there* is reported as `reason: backend-unreachable`
(a statement about the backend, never `project-not-indexed`); a failure inside the op itself is reported
as the kind (`timeout`, `backend-error`, `unparsable`). A backend that is not installed at start-up never
gets that far: the envelope is `engine-unavailable`.

**What is quoted.** For a refusal, the backend's stderr: its `level=info`/`level=debug` log lines
dropped, whitespace collapsed, the **tail** kept (the reason is the last thing a process says) and capped
at about 400 characters. The home directory is redacted at capture — `~` for the path form, `<home>` for
the flattened project-id form — so the text is safe to print on the CLI, which does not pass through the
envelope boundary's `redact`, as well as over MCP and HTTP, which do.

**One launch or two.** The transport tries the piped-stdin form first and falls back to the deprecated
raw-JSON positional form, for an older backend that rejects the stdin form. That fallback runs only when
the first attempt could have failed for that reason: a **usage error** falls back, as it always has. It
does not run after a timeout (the budget is spent), for a binary that is gone, or when the backend says it
will not run at all (`could not start`, a coordination refusal) — launching the same command again can
only get the same answer. When both attempts fail, the **first** specific failure is the one recorded.

**Where the message reaches.** The doctor row (`detail`, and a `remediation` chosen for the cause), the
`code.status` `readiness.graph` row, and — through `Missing.describe()` — the envelope's `hint` and `gaps`
for a query that failed (`backend-unreachable`, `backend-error`). A hint for a refusal carries the
backend's message and the fix, not a command to run: the reader of an envelope is usually an agent with no
shell. That holds wherever the refusal lands: a refusal while resolving the project (`backend-unreachable`),
one inside an op that was already running (`backend-error`), and — for `changed <ref>` — the
`callers-unavailable` gap of a symbol whose caller lookup was refused all read the same sentence.

### Recovering from a coordination refusal

The failure that motivated this page:

```
level=info msg=version_cohort.claimed_unheld build=...
codebase-memory-mcp: CBM CLI could not start because a pre-coordination or unverified CBM generation
is active; close all CBM sessions and commands, then retry
```

Every call exits `1` within about a second. Closing the sessions it names is not enough on its own —
the lock files outlive the processes. The procedure that was verified to clear it:

1. **Close every `codebase-memory-mcp` process.** This also closes the graph tool in any agent session
   that owns one — that agent has to be restarted to get it back, so do it deliberately.
2. **Move the `cbm-daemon-<uid>` directory under the system temp directory aside** (rename it; do not
   delete it, so there is something to look at if it recurs). On macOS it was observed at
   `/private/tmp/cbm-daemon-501` — the backend does not use the per-user `$TMPDIR` there. The doctor
   names the directory that actually exists (`found at …`) and, when none does, the places it looked,
   built from the platform's temp directory rather than a fixed path.
3. **Retry** — `codeintel doctor`.

The doctor prints exactly this remediation, and only for this refusal: other refusals are told to
address what the backend said, and a backend that exits non-zero without a word is told so rather than
guessed at. None of them is told to run the failing command by hand — the doctor has already run it, and
the part of its output that explains it is what the row quotes.
