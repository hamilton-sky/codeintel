# Installation & registration

How `codeintel install` puts the server in front of an agent, and — more importantly — how it
*proves* it did.

This doc exists because this is the part of codeintel that has broken most often. Three separate
releases shipped a registration that wrote a well-formed config file to a location or in a shape the
host does not read, each time with a green test suite, because the tests asserted the bytes we wrote
rather than what the host consumes. The layout table below is the ground truth those bugs cost.

## What each host actually reads

| Agent | Config file | Home override | Key | Entry shape |
|---|---|---|---|---|
| Claude Code | `~/.claude.json` | `CLAUDE_CONFIG_DIR` | `mcpServers.codeintel` | `{"command": "...", "args": ["serve"]}` |
| Codex | `~/.codex/config.toml` | `CODEX_HOME` | `[mcp_servers.codeintel]` | TOML table, `command` + `args` |
| Gemini CLI | `~/.gemini/settings.json` | `GEMINI_CONFIG_DIR` | `mcpServers.codeintel` | `{"command": "...", "args": ["serve"]}` |
| Zed | `~/.config/zed/settings.json` | `XDG_CONFIG_HOME` | `context_servers.codeintel` | `{"command": "...", "args": ["serve"]}` |

Three traps are baked into that table:

- **Codex is TOML, not JSON.** A Claude-style `mcpServers` map in `~/.codex/config.json` is silently
  ignored. (Shipped broken in ≤ 0.8.1.)
- **Claude Code reads `~/.claude.json`, not `~/.claude/settings.json`.** The latter is for
  hooks/theme/permissions; an `mcpServers` block there is inert, which `claude mcp list` confirms.
  (Shipped broken in ≤ 0.11.1.) If an old install left one behind, `codeintel install` points it out
  and leaves it alone — it is your file, not ours to delete.
- **Zed's entry is flat.** `command` is a *string* with `args` beside it, under `context_servers`
  (not `mcpServers`). codeintel previously wrote a nested `{"command": {"path", "args"}}` object.
  (Shipped broken in ≤ 0.11.2.)

Every home override is read at **call time**, so a managed or CI setup that relocates a config root
is registered where it actually lives rather than in a `~` nobody reads.

## Which agents get registered

`codeintel install` defaults to `--agent auto`: it registers the hosts whose config root already
exists on this machine, and names the ones it skipped.

```bash
codeintel install                      # auto — only what you have
codeintel install --agent claude       # force one host
codeintel install --agent all          # force every supported host
```

Installing a Python package should not create `~/.gemini/` and `~/.config/zed/` for someone who has
neither. With no agent detected, install writes nothing and exits non-zero rather than reporting a
success that means nothing.

**If your host is skipped but installed**, it is because its config root does not exist yet — a host
you have never launched has not created one. Register it explicitly with `--agent <name>`; that
creates the file, and the host will read it on next start.

## Why the command is an absolute path

Registrations carry the absolute path to `codeintel` (e.g. `/Users/you/.local/bin/codeintel`), not
the bare name.

The bare name is resolved by the **host**, not by the shell you ran `install` in. A GUI-launched
desktop agent does not source your shell profile, so a `codeintel` your terminal finds on `PATH` is
routinely invisible to the app. This is the one failure the handshake verifier is structurally blind
to: verification runs in *your* environment, where `PATH` already works, so it happily confirms a
launch line the real host cannot execute.

```bash
codeintel install --relative-command   # opt back into the bare name
```

The cost of an absolute path is that an upgrade or a rebuilt venv can move the binary. Two things
cover that:

- **Re-running `codeintel install` repairs it in place** — only codeintel's own entry is rewritten;
  neighbouring servers and unrelated settings stay byte-identical.
- **`codeintel doctor` reports a stale launch command** with the exact repair, because a dead path is
  otherwise invisible from inside the agent — the host just says the server will not start.

```text
└─ claude registration: stale launch command in /Users/you/.claude.json
   fix: `/gone/bin/codeintel` no longer exists — re-run `codeintel install --agent claude`
```

## Three levels of proof

A config file proves nothing on its own. Each level below catches what the one above cannot.

**1. The file is written.** Necessary, never sufficient — this is precisely what passed while all
three bugs above were live.

**2. A real MCP handshake** (`codeintel install`, on by default). Install launches the exact command
it registered and drives `initialize` → `notifications/initialized` → `tools/list`:

```text
v claude: registered at /Users/you/.claude.json

v verified: codeintel 0.21.0 — 4 tools (code.query, code.status, code.doctor, code.map)
```

If the command is not on `PATH`, or the server fails to start, install says so and **exits
non-zero** instead of claiming a success your agent cannot use. `--no-verify` skips it.

**3. The release canary** ([`scripts/release_canary.py`](../scripts/release_canary.py)), run in CI
and again before every PyPI publish. Levels 1 and 2 still cannot catch a server that boots cleanly
and answers nothing — every codeintel result is a safe envelope whose `ok` is always `true`, and the
CLI never throws, so an exit-code check passes against an inert build. The canary installs the built
wheel into a clean environment, registers every host into a throwaway `HOME`, launches the command
those configs name, and asserts on the **answer text** of a real `code.query` over a fixture repo.

```bash
python -m build && python -m venv /tmp/canary && /tmp/canary/bin/python -m pip install dist/*.whl && /tmp/canary/bin/python scripts/release_canary.py
```

It is verified to go red on both historical failure modes: a config written where the host does not
read it, and a server returning `ok: true` with an empty body.

## After registering

Start a **new** agent session — hosts read MCP config at startup. Then confirm from inside the agent:

- `code.status` — per-engine `installed` / `runnable` / `repo_indexed`, probed against the live
  engines a query actually hits.
- `code.doctor` — the same, plus a one-line fix for each gap and any stale registration.

For Claude Code specifically, `claude mcp list` should now show `codeintel`.

## Offline / air-gapped install

The one non-local step in `codeintel setup` is `fastembed` downloading the
`BAAI/bge-small-en-v1.5` embedding weights (~50 MB) the first time the semantic engine runs.
Behind a corporate proxy with no route to the model host, that download fails and `codeintel
setup --all` cannot finish — the [2026-08-23 status doc](eval-2026-08-23-status-and-market.md)
measured this directly: `ProxyError 403`, empty index, `assert 0 > 0`.

A real workaround exists, based on how `fastembed` resolves its cache directory
(`fastembed.common.utils.define_cache_dir`): it defaults to `$TMPDIR/fastembed_cache`, but honors
the `FASTEMBED_CACHE_PATH` environment variable when set. codeintel does not pass its own
`cache_dir`, so setting that variable is enough to redirect (or pre-seed) the model cache without
any code change.

**On a machine with network access:**

```bash
export FASTEMBED_CACHE_PATH=/path/to/a/portable/model-cache
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
# copy /path/to/a/portable/model-cache to the air-gapped machine
```

**On the air-gapped machine**, point the same variable at the copied directory before running any
codeintel command that touches the semantic engine:

```bash
export FASTEMBED_CACHE_PATH=/path/to/a/portable/model-cache   # e.g. in your shell profile
codeintel setup --all /path/to/your/project
```

This is a real path derived from `fastembed`'s own cache resolution, not something this project
ships or tests end-to-end in CI (CI has network access, so the air-gapped case is unexercised
here). If it does not work for your `fastembed` version, `codeintel doctor` still degrades
cleanly — the graph and LSP engines are unaffected, and `codeintel` runs with `semantic` reporting
`installed: false` rather than crashing.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No supported agent found on this machine` | No host config root exists yet | `codeintel install --agent <name>` |
| `x NOT verified: ... is not on PATH` | `codeintel` isn't installed where the shell can see it | `pip install codecortex`, or `uv tool install codecortex` |
| Agent shows no codeintel tools | Session started before registration | Restart the agent |
| `doctor` reports a stale launch command | Binary moved (upgrade / rebuilt venv) | Re-run `codeintel install` |
| `! stale entry: ~/.claude/settings.json` | A pre-0.11.2 install wrote an inert block | Delete that `mcpServers` block by hand |

## See also

- [architecture.md](architecture.md) — how the gateway and providers fit together
- [deploy.md](deploy.md) — running codeintel as a shared HTTP service instead of a local stdio server
