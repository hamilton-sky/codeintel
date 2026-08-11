# PROJECT SPEC — `codeintel` (working name)

> **A standalone, MCP-native code-intelligence server that gives any coding agent
> (Claude, Codex, Gemini, Zed, …) the best structural *and* semantic understanding
> of any codebase — install once per repo, one tool interface, three engines behind it.**

This is a **greenfield project spec**, purpose-built — not an extraction of any existing
codebase. It is written to be seeded into a Pathly **project board** and decomposed into
features (each `## Feature Fn` below → one `pathly/features/<slug>/`).

Working name: `codeintel` · MCP server: `codeintel-mcp` · CLI: `codeintel`
(names are placeholders — rename freely; alt ideas: *Cortex, Codescope, Lens, Beacon*).

---

## 1. Problem & motivation

Coding agents are only as good as their view of the codebase. Today that view is fragmented:

- **Structural-breadth** tools (whole-repo symbol graphs: who-calls-X, impact/blast-radius,
  call chains) live in one silo. They're fast and broad but can lag recent edits.
- **Structural-precision** tools (LSP: exact definition/references for one symbol) live in
  another. They're always-fresh but narrow and slow to warm up.
- **Semantic** search ("find the code that *does* X" when you don't know the symbol name — the
  Sourcegraph/Cody axis) is a third silo, usually cloud-hosted and privacy-hostile.

No single tool an agent can install gives all three behind one clean, safe interface. Agents
either wire up several MCP servers with different contracts, or fall back to raw grep and miss
the structure. The result: worse impact analysis, blind refactors, and repeated re-discovery.

**`codeintel` closes this** by unifying the three engines behind ONE agent-facing tool
(`code.query`) with a hard safety contract (never breaks a prompt), local-only operation
(no code leaves the machine), and a one-command install into every major agent host.

## 2. Goals & non-goals

**Goals**
- One MCP tool surface (`code.query`) that any agent host can call, exposing all engines via an
  `engine` selector — no per-engine wiring for the agent.
- Three engines: **graph** (breadth), **lsp** (precision/freshness), **semantic** (meaning).
- **Never-raise / safe-null contract**: any internal failure returns a null result, never an
  error — the agent degrades to grep, never crashes.
- **Local-first & private**: no network egress, no API keys, no code sent anywhere. Semantic
  embeddings run on a local model.
- **Install-once-per-agent**: `codeintel install` self-registers the MCP server into Claude
  Code, Codex, Gemini, Zed (mirrors how best-in-class code MCPs already self-register).
- **Zero-config default**: auto-detect which engines are available; degrade cleanly when one
  isn't installed.
- **Fresh enough, cheap enough**: incremental, content-hash-keyed indexing; debounced background
  reindex; bounded per-query latency.

**Non-goals (v1)**
- Not a code editor, linter, or formatter.
- Not a cloud service or multi-tenant server (single-user, local process).
- Not a replacement for the agent's own file read/edit tools — it *complements* them.
- No org-wide/shared index in v1 (per-machine cache only).
- Not tied to Pathly at runtime — Pathly is only the (optional) build harness; the shipped tool
  has **zero Pathly dependency**.

## 3. Users & primary use cases

**Primary user: an autonomous coding agent** (Claude/Codex/Gemini/Zed) mid-task.
**Secondary user: a developer** at a CLI, or a harness (like Pathly) injecting context.

Representative calls:
| Intent | Call |
|---|---|
| "Who calls `parse_result`? What breaks if I change it?" | `engine=graph, op=impact` |
| "Exact definition + all references of this symbol, right after my edit" | `engine=lsp, op=symbol` |
| "Where is authentication handled?" (no symbol name known) | `engine=semantic, op=search` |
| "Give me everything on this file before I refactor" | `engine=all, op=context` |
| "Trace the call chain from route to DB write" | `engine=graph, op=chain` |

## 4. Capabilities (functional requirements)

### 4.1 Engines
- **Graph engine** — whole-repo structural index: functions/methods/classes with caller
  (`in_degree`) and callee (`out_degree`) counts; impact/blast-radius; call chains; text+graph
  search. Multi-language (tree-sitter class of coverage). Can lag edits → advisory, self-heals
  via reindex.
- **LSP engine** — always-fresh, precise single-symbol intelligence via a language server:
  symbols overview, definition, references. Async warm-up; first call may return null while the
  server boots, then fast.
- **Semantic engine** — meaning-based search over locally-embedded code chunks (see §6). Returns
  ranked `path:line` + snippet; floored by a relevance threshold so weak matches return empty,
  not noise.

### 4.2 Unified gateway (`code.query`)
- Single entry point taking `{op, target, engine?, budget?, role?, project_root?}`.
- **Ops:** `impact | callers | callees | chain | symbol | context | pattern | search | overview`.
- **Engine selector:** `graph | lsp | semantic | both | all | auto`
  (`auto` = pick the best available for the op; `both` = graph+lsp; `all` = graph+lsp+semantic).
- **Safe-null envelope:** `{ok, op, target, result, engine, cached, reason?}` — `result:null`
  never means "crash"; `reason` distinguishes *found-nothing* from *engine-not-installed*.
- **Content-hash cache:** repeated query on an unchanged file served from cache; an edit changes
  the hash and forces a refresh. Only non-empty results cached.
- **Auto-backend detection:** probe which engines are installed; route accordingly; a machine
  with none degrades to a documented null (agent uses grep).
- **Optional role/op tiering:** an off-by-default policy layer to restrict ops per caller role
  (harnesses like Pathly can enable it; standalone default = all ops for all callers).

### 4.3 Freshness
- Incremental indexing keyed by per-file content hash — only changed files reparse/re-embed.
- Debounced, fire-and-forget background reindex fired on-demand from a query and on a file-watch
  (v2) — never blocks a response.

### 4.4 Transports
- **MCP server (primary)** — stdio JSON-RPC; exposes `code.query` (+ granular tools, §7).
- **HTTP (parity, optional)** — `POST /code/query` with the same contract, for harnesses.
- **CLI** — `codeintel query --op … --target …` for humans/scripts.

## 5. Architecture

```
                       ┌──────────────────────────────┐
   agent (MCP) ───────▶│  codeintel gateway            │
   harness (HTTP) ────▶│   • op/engine routing         │
   human (CLI) ───────▶│   • safe-null contract        │
                       │   • content-hash cache         │
                       │   • auto-detect + reindex seam │
                       └───────────────┬───────────────┘
                                       │  CodeProvider protocol
                 ┌─────────────────────┼─────────────────────┐
                 ▼                     ▼                     ▼
          GraphProvider          LspProvider          SemanticProvider
          (breadth)              (precision/fresh)    (meaning)
                 │                     │                     │
        code-graph backend      language server        local embeddings
        (tree-sitter index)     (LSP via MCP)          + sqlite-vec index
```

**Design principles**
- **Provider protocol** — every engine implements the same `CodeProvider` interface
  (`build_result(op, target, files, budget, project_root) -> Result | null`), so engines are
  swappable and composable (`both`/`all` just fan out and merge). This is the seam that lets a
  wrapped backend later be replaced by an in-house one **without changing the public contract.**
- **Never-raise everywhere** — every provider returns null on any failure; the gateway can only
  ever ADD information to a response, never break one.
- **Layered, single-responsibility modules** — gateway / providers / index-store / transports /
  config are separate concerns; hard file-size discipline (~≤400 lines/file).

**Backend strategy (key decision — see §13 Q1): wrap-and-unify.** v1 wraps two proven, existing
open engines behind the provider protocol (fast path to best-in-class, no re-solving 158-language
parsing or LSP), and **owns** the unification layer, the semantic engine, the safety contract,
and the install/UX. The provider seam keeps a future self-contained reimplementation open without
breaking callers.

- **GraphProvider** → wraps a code-graph backend (e.g. `codebase-memory-mcp`-class: tree-sitter
  whole-repo index, symbol degrees, graph query). Shell/CLI or MCP transport.
- **LspProvider** → wraps a language-server bridge (e.g. Serena-class, launched via `uvx`): one
  long-lived MCP session, async warm-up, single cached session per project root.
- **SemanticProvider** → **fully in-house** (this project's original engine, §6).

## 6. Semantic engine (the meaning-based piece — built fresh here)

The one engine that is genuinely new. Local, private, no API keys.

- **Model:** a bundled local sentence-embedding model (e.g. `all-MiniLM-L6-v2`, 384-dim) via
  sentence-transformers. First-use lazy load with retry; if the library is absent the engine
  reports `unavailable` and the gateway degrades — never a hard failure.
- **Index store:** a `code_embeddings` **sqlite-vec (vec0)** virtual table — one row per code
  chunk: `{id, project_root, path, chunk_start, chunk_end, content_hash, embedding}`. Keyed for
  incremental rebuild by `(path, content_hash)`.
- **Indexer:** walks the repo respecting `.gitignore`, chunks each source file, embeds each
  chunk, upserts rows. Skips files whose `content_hash` is unchanged (cheap incremental). A total
  chunk cap is enforced and any drop is logged (never silent truncation). Unreadable/binary files
  are skipped, not fatal.
  - **Chunking v1** = fixed line-window with overlap (simple, language-agnostic).
  - **Chunking v2** = function/class chunks (reuse the graph backend's existing parse rather than
    re-parsing) — better recall; table shape already supports it.
- **Search (`op=search`):** treat `target` as a natural-language query, embed it, KNN over
  `code_embeddings`, return a ranked `## Code matches` block (`path:line` + snippet + score).
  Weak matches floored by a cosine ceiling → empty, not noise. Empty index → safe-null (agent
  uses grep).
- **Result shape:** `path:line` pointers + short snippet by default (token-cheap); a follow-up
  hydrate op can return full chunk text on demand.

## 7. Public contract — MCP tools exposed to the agent

Two groups: **lifecycle** (the agent controls freshness + artifacts on-demand) and **query** (the
intel). Every tool **never-raises** — it returns a safe-null envelope `{ok, result, reason?}` on any
miss, so a missing engine / empty index degrades the agent to grep, never errors.

**Lifecycle — agent-controlled, on-demand:**
- **`index`** `{project_root?, full?}` — (re)index the repo on demand. Incremental by default (a
  fast content-hash pass — only changed files reparse/re-embed); `full:true` rebuilds from scratch.
  The agent calls this to guarantee freshness before a query burst, **especially right after its own
  edits**. (The server also auto-reindexes, debounced, as a safety net — this tool is explicit control.)
- **`map`** `{project_root?, budget?, inject?}` — write/refresh `CODE_INTEL.md` on demand (F10);
  `inject:true` links it into `CLAUDE.md`/`AGENTS.md`.
- **`status`** `{project_root?}` — index freshness, which engines are installed/ready, model state —
  so the agent can decide whether to reindex first.

**Query — the intel:**
- **`search`** `{query, project_root?, k?}` — semantic NL search ("find the code that does X"). [semantic]
- **`impact`** `{target, project_root?}` — blast radius: callers + what breaks if you change it. [graph]
- **`symbol`** `{target, project_root?}` — precise, always-fresh definition + references. [lsp]
- **`query`** `{op, target, engine?, budget?, project_root?}` — the power tool: the full op matrix
  (`impact|callers|callees|chain|symbol|context|pattern|search|overview`) × engine
  (`graph|lsp|semantic|both|all|auto`). The three named tools above are thin wrappers over it.

**Typical agent loop:** `status` → (if stale) `index` → `search`/`impact`/`symbol`/`query` →
`map` when it wants a refreshed orientation file. Exact wire names (dots vs underscores) are an F1
detail; the shape above is the contract.

## 8. Install & UX

- `codeintel install [--agent claude|codex|gemini|zed|all]` — self-register the MCP server into
  the chosen agent host(s)' config (idempotent). Mirrors how leading code MCPs self-register.
- `codeintel index <repo>` — build/refresh the graph + semantic index for a repo.
- `codeintel map <repo> [--inject]` — write/refresh `CODE_INTEL.md` (F10); `--inject` links it into `CLAUDE.md`/`AGENTS.md`.
- `codeintel serve [--http :PORT]` — run the MCP server (and optional HTTP parity port).
- `codeintel query --op … --target … [--engine …]` — human/CLI one-shot.
- `codeintel status` — engine availability + index freshness + model state.
- **Config:** `.codeintel.toml` at repo root (project-local) overriding `~/.codeintel/config.toml`
  (global). Keys: `backend = auto|graph|lsp|semantic|both|all|off`, `semantic = on|off`,
  `reindex = off|on-demand|watch`, chunking params, cap, cosine floor. **No external DB
  dependency** — config is a plain file; the index cache lives per-machine under `~/.cache/…`.

## 9. Cross-cutting requirements

- **Safety:** never-raise contract is a tested invariant, not a convention (fault-injection tests
  per provider + gateway).
- **Privacy:** no network calls in the default path; semantic runs locally; document exactly what
  (if anything) any wrapped backend touches.
- **Performance bounds:** every engine call is deadline-bounded; a wedged subprocess can never
  block a response (bound the *wait*, degrade to null). Cache hot paths.
- **Freshness:** incremental reindex debounced to at most once per window; on-demand from queries;
  file-watch is a v2 upgrade.
- **Portability:** Linux/macOS/Windows. Windows argv/subprocess quirks handled explicitly.
- **Observability:** `code.status` + structured logs; optional query log (off by default — the
  board-logging that a harness like Pathly layered on is NOT part of the standalone core).

## 10. Tech stack (proposed)

- **Language:** Python 3.11+ (fastest path — the semantic + provider patterns are proven in
  Python; sentence-transformers + sqlite-vec are first-class). MCP via the Python MCP SDK.
  *(Alt: TypeScript/Node if agent-ecosystem parity matters more — see §13 Q2.)*
- **Semantic:** sentence-transformers (`all-MiniLM-L6-v2`), sqlite-vec (vec0).
- **Graph backend:** a `codebase-memory-mcp`-class tree-sitter code-graph engine (wrapped).
- **LSP backend:** a Serena-class LSP-over-MCP bridge, launched via `uvx` (wrapped).
- **Packaging:** `pipx`/`uvx`-installable; single `codeintel` entrypoint.

## 11. Milestones → **features** (each becomes one `pathly/features/<slug>/`)

Ordered so each lands something safe end-to-end.

### Feature F1 — Skeleton + `code.query` contract (safe-off)
MCP server boots; `code.query` + `code.status` exist; `CodeProvider` protocol + a `NoneProvider`
that always returns safe-null. No real engine yet.
**AC:** `code.query` returns a well-formed safe-null envelope for any input; server registers as an
MCP tool; contract documented; never raises (fault-injection test passes).

### Feature F2 — Graph engine adapter
`GraphProvider` wrapping the code-graph backend: `impact/callers/callees/chain/pattern` +
`overview`. Auto-detect if the backend binary is present; null + `reason:engine-unavailable` when
not.
**AC:** on an indexed repo, `op=impact` returns real caller/callee data; missing backend → safe-null
with a `reason`; deadline-bounded.

### Feature F3 — LSP engine adapter
`LspProvider` wrapping the language-server bridge: async warm-up, single cached session per root,
`op=symbol/overview`. First call returns null (warming) then real data.
**AC:** after warm-up `op=symbol` returns fresh definition/references; boot failure → cooldown, no
per-request respawn; never blocks.

### Feature F4 — Unified gateway
Engine selector (`graph|lsp|semantic|both|all|auto`), `both`/`all` fan-out+merge, content-hash
cache, auto-backend detection, optional role/op tiering (off by default).
**AC:** one endpoint serves all engines; `engine=both` merges graph+lsp; unchanged file served from
cache; edit busts it; tiering toggle works and defaults to permissive.

### Feature F5 — Semantic engine (in-house)
`code_embeddings` vec0 table + local-embedding indexer (incremental, content-hash) +
`op=search` KNN with cosine floor. (§6)
**AC:** `op=search` on a natural-language query returns relevant `path:line` matches on a real repo;
re-indexing an unchanged repo re-embeds nothing; empty index → safe-null; weak matches floored to
empty.

### Feature F6 — Freshness / reindex seam
Debounced fire-and-forget incremental reindex fired on-demand from queries (graph + semantic);
`reindex` config; never blocks.
**AC:** editing a file and re-querying reflects the change within one debounce window; reindex runs
off-thread; `off` disables it.

### Feature F7 — Install & UX
`codeintel install/index/serve/query/status`; self-register into Claude/Codex/Gemini/Zed;
`.codeintel.toml` config; per-machine cache.
**AC:** `codeintel install --agent all` makes the tool callable from each host; `index` builds both
indexes; config file overrides defaults; idempotent.

### Feature F8 — HTTP transport parity (optional)
`POST /code/query` mirroring the MCP contract for harness callers.
**AC:** identical safe-null envelope over HTTP; never 500s on an engine miss (only malformed
requests get 4xx).

### Feature F9 — Docs + test suite
README (agent-first framing), per-engine docs, the never-raise invariant suite, indexer
incrementality tests, an end-to-end smoke on a real repo.
**AC:** a new user can install + query in <5 min from the README; CI runs the safety + incremental
+ ranked-result suites green.

### Feature F10 — MD map-file mode (universal, zero-integration)
`codeintel map` generates a compressed, **ranked** `CODE_INTEL.md` from the graph index —
architecture overview, top modules, key symbols (highest fan-in), entry points, "who-calls-what"
highlights — under a size budget. The universal layer: **any** agent reads files, so this works
even for hosts with no MCP support, and primes orientation with zero setup. Committable; refreshed
on `index`. Complements (never replaces) the live `code.query` tool — the file is a static
snapshot for orientation; the tool answers fresh/arbitrary queries. (Prior art: Aider's repo-map.)
**AC:** `codeintel map` writes a self-contained, human-readable `CODE_INTEL.md` with a ranked
module/symbol overview under a byte budget; deterministic on an unchanged repo, updates on change;
consumable with no tool installed; optional `--inject` links/appends it into the repo's existing
agent-context file (`CLAUDE.md`/`AGENTS.md`).

## 12. Rollout order

F1 → F2 → F3 → F4 → F5 → F6 → F7 → (F8) → F9 → F10. F1–F4 give unified structural intelligence; F5
adds the differentiator (semantic); F6–F7 make it fresh + installable; F8 is optional parity; F9
ships it; F10 adds the universal MD-map layer (needs the graph from F2). Each feature is
independently shippable behind the safe-null contract.

**The three consumption modes** (matched to three needs): **MD map file** (any agent, static
orientation, zero setup — F10) · **MCP `code.query` tool** (MCP hosts, live/fresh/arbitrary queries
— F1–F6) · **HTTP** (harnesses that inject calls — F8).

## 13. Open questions / decisions to make before/at kickoff

1. **Wrap vs reimplement the structural engines.** v1 = wrap-and-unify (recommended: fastest to
   best-in-class, provider seam keeps reimplementation open). Confirm, or commit to a heavier
   self-contained build.
2. **Python vs TypeScript.** Python is the fastest path (semantic + providers proven there);
   TS/Node may fit the agent ecosystem better. Pick one.
3. **Index scope/size caps** — whole-repo vs a source-dir allowlist; the total-chunk cap value.
4. **Bundle vs fetch the embedding model** — ship weights vs download on first index.
5. **Which agent hosts for the v1 `install`** — Claude Code + Codex first, others fast-follow?
6. **Name** — lock the product name before F1 (affects the binary, config path, MCP id).

---

### Appendix — provenance
The design lineage is a proven unified code-intelligence gateway (one endpoint, engine selector,
safe-null, content-hash cache, debounced reindex, auto-backend detection) plus a designed-but-
unbuilt local-semantic engine. This project rebuilds those capabilities **standalone and
agent-first**, dropping all harness-specific coupling (no shared board, no external settings DB).
