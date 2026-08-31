# Bringing all three providers up and working

codeintel answers through three independent engines behind one never-raise `code.query`. Each is
**optional and degrades gracefully** — a missing engine returns a safe-null envelope with a `reason`,
never an error — so "working" means: installed, this repo indexed/warm where required, and
`doctor` reports it `ok`. This is the operational runbook; the per-engine design lives in
[`graph.md`](graph.md), [`lsp.md`](lsp.md), [`semantic.md`](semantic.md), install specifics in
[`install.md`](install.md).

Verified state on the author's machine for this repo (2026-08-24): `doctor --deep` → **ready 3/3,
healthy**. graph = codebase-memory-mcp 0.9.x or 0.10.x (indexed); lsp = serena booted via `uvx` and reached
READY; semantic = 3,921 chunks (fastembed 0.8.0 + sqlite-vec 0.1.9).

## TL;DR — the whole thing in three commands

```bash
codeintel setup --all      # install uv+deps, index this repo, warm serena (flag == consent, no prompt)
codeintel doctor --deep    # actively boot/probe every engine; prints a concrete fix per gap
codeintel install          # register codeintel as an MCP server in your agents' configs
```

`setup --all` is idempotent — it skips an engine already reporting installed. On a machine with none
of the backends, the graph binary is the one manual step (it is a standalone native binary, not a pip
package — see graph below); `setup` installs the rest and indexes.

---

## Engine 1 — graph (codebase-memory-mcp)

**Powers:** `callers`, `callees`, `impact`, `chain`, `hotspots`, `changed`, `overview`, `pattern` —
the call/import-structure ops.

**Bring-up**
1. **Install the binary on PATH.** It is a standalone native backend distributed by its own project,
   NOT pip-installable. Put `codebase-memory-mcp` for your platform on PATH (`~/.local/bin` here).
2. **Either dialect works; prefer `0.10.x`.** `0.9.x` answers in `{columns, rows}` JSON and
   `0.10.x` in a text layout, and `wire_text.py` reads both at one transport seam. `0.10.x` is the
   better backend — guessed `CALLS` edges drop from 24-43% of the graph to 9-30%, and Python
   enclosing-function attribution from ~32% lost to 2.7%. Two traps: `codebase-memory-mcp update`
   **deletes every index before** it can succeed and then fails on a non-TTY (pass `--standard`), and
   indexes are not portable across `0.9`/`0.10` — delete the repo's `.db` under
   `~/.cache/codebase-memory-mcp/` and re-index after switching. Start a daemon
   (`codebase-memory-mcp daemon start`): `0.10.x` otherwise spawns a temporary one per CLI call.
3. **Index the repo:** `codeintel index <path>` (or the first query auto-registers it).

**Verify:** `doctor` → `graph: ok, resolved project '…'`.

**Failure modes → fix**
- `engine-unavailable` — binary not on PATH → install it, re-run `setup`.
- `backend-incompatible` — the reply matched neither dialect, so most likely a backend newer than this codeintel. Upgrade codeintel first; failing that pin `codebase-memory-mcp==0.10.*`.
- `backend-unreachable` (NOT "not indexed") — the binary re-initialises its allocator every
  invocation (~5.8s measured), and a slow machine can exceed the resolve budget. Raise it:
  `export CODEINTEL_GRAPH_RESOLVE_TIMEOUT_MS=40000`. This is the failure that once reported a fully
  indexed repo as un-indexed.
- `project-not-indexed` — run `codeintel index <path>`.
- `project-not-indexed-standalone` — the repo is a nested repo under an indexed ancestor; root-scoped
  ops (`hotspots`/`overview`/`changed`) refuse rather than answer from the parent. Index it on its
  own: `codeintel index <path>`.
- Stale/duplicate registrations — the backend can hold two projects for one root; resolution now
  prefers the most complete (highest node count), so re-indexing fresh is the cure. The test suite's
  session reaper (conftest.py) deletes leaked `pytest-of-*` registrations automatically.

---

## Engine 2 — lsp (serena)

**Powers:** precise, compiler-grade symbol definitions and references — the `symbol` op (plus
`overview`/`context`). It does not power `callers`/`callees`; those route to the graph engine.
Live — **there is nothing to index**; it reads the workspace through language servers.

**Bring-up**
1. **Install `uv`** (provides `uvx`): `pip install uv`. That is the only dependency you install —
   serena itself is fetched on first use straight from git, via
   `uvx --from git+https://github.com/oraios/serena serena start-mcp-server --context ide-assistant --enable-web-dashboard false --project <root>`
   (plain `uvx serena` does NOT work — the package ships no `serena` executable of that name).
2. **Warm it once:** `codeintel setup --warm`. The first launch pulls serena via uvx and boots a
   language server for the repo's language, which is slow; after that it is cached. Without warming,
   the cost lands on a user's first LSP query instead.

**Verify:** `doctor --deep` → `lsp: ok, serena booted via 'uvx' and reached READY`. (Plain `doctor`
without `--deep` reports `warn: boot not verified` — that is lazy-boot, not a fault; `--deep`
actually boots it.)

**Failure modes → fix**
- `installed: false` — no `uv`/`uvx` → `pip install uv`.
- Slow/failed first query — cold serena boot → warm it ahead of time (`setup --warm`); if it never
  reaches READY, check `uvx --from git+https://github.com/oraios/serena serena start-mcp-server
  --context ide-assistant --enable-web-dashboard false --help` runs and that the network can reach
  GitHub.
- No answers for a given language — serena needs a language server for that language; a repo in an
  unsupported language gets no LSP results (graph + semantic still answer).

---

## Engine 3 — semantic (fastembed + sqlite-vec)

**Powers:** natural-language / "find the code that does Y" `search`, graph-augmented and ranked.

**Bring-up**
1. **Install deps:** `pip install fastembed sqlite-vec` (or `pip install -e .`, which pulls them in).
2. **Index the repo:** `codeintel index <path>` — embeds source chunks into a per-model sqlite-vec DB.
   The model is `BAAI/bge-small-en-v1.5` (fastembed downloads it on first use — one network fetch).
   Each model has its own cache file, so a repo configured with a different model can never corrupt
   another's rows.

**Verify:** `doctor` → `semantic: ok, N indexed chunks for this repo`.

**Failure modes → fix**
- `fastembed / sqlite-vec not importable` → `pip install fastembed sqlite-vec`.
- No/empty results on a fresh repo → not indexed yet → `codeintel index <path>`.
- First query stalls on a fresh machine — the model is downloading — **only on the CLI**
  (`codeintel query`), which indexes inline; subsequent queries are fast. The long-lived MCP/HTTP
  server never stalls a query on this: a cold repo instead gets `reason: 'indexing-in-progress'`
  immediately, while the pass (and the model download) runs in the background — retry shortly.
- Re-index after large edits so chunks don't go stale (`codeintel index`); the graph engine
  auto-reindexes changed files on `changed`, but semantic chunks are refreshed by re-indexing.

---

## Global: registration, CI, and skew

- **Register with agents:** `codeintel install` writes the MCP-server entry into each agent's config
  (verified here for `~/.claude.json`, `~/.codex/config.toml`, `~/.gemini/settings.json`,
  `~/.config/zed/settings.json`). `doctor` lists each registration and whether its command is runnable.
- **CI / a fresh box has none of the backends, by design.** All three degrade to safe-null with a
  `reason`, and the live tests SKIP (a skip is reported, never silently passed). So "3/3 healthy on my
  laptop" does not mean the no-backend paths are exercised — reproduce CI's shape with
  `env PATH="$(dirname "$(which python)"):/usr/bin:/bin" pytest -q` before trusting a green local run.
- **Version skew:** `doctor` reports `codeintel` vs the running process and each backend's version;
  `version_skew` is non-null when the installed console script is older than the source you are
  running. Re-install (`pip install -e .`) to clear it.
- **One command to trust:** whenever `code.query` looks empty or an engine seems missing, run
  `code.doctor` (or `codeintel doctor --deep`) — it names the single fix for each gap rather than
  leaving you to guess which of the three engines is down.
</content>
