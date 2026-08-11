# Edge Cases — codeintel install/index/query/status CLI

---

## Phase 1 — Config module

**EC-1.1 Malformed TOML**
- `.codeintel.toml` contains invalid TOML syntax.
- Expected: `load_config` catches the `tomllib.TOMLDecodeError`, logs a warning, falls back to global config (or defaults if global also missing). Never raises to the caller.

**EC-1.2 Unknown config key**
- `.codeintel.toml` has `backend = "bogus_engine"`.
- Expected: `load_config` returns the raw value without validation. The Gateway's `_KNOWN_ENGINES` check will catch it at query time. Config loading is intentionally permissive.

**EC-1.3 Missing home directory**
- `~/.codeintel/config.toml` path cannot be resolved (unusual container environment).
- Expected: `Path.home()` failure is caught; global config skipped; defaults returned.

**EC-1.4 Type override mismatch**
- Global config has `window = 20` (int); project config has `window = "twenty"` (str).
- Expected: project value wins (str). Downstream code may fail gracefully when it tries to use it — that is a caller concern, not config's.

---

## Phase 2 — CLI: index / query / status

**EC-2.1 `index` on non-existent path**
- `codeintel index /no/such/path`
- Expected: `Indexer.index()` returns 0 (its own guard `if not root.exists(): return 0`). CLI prints "Nothing new to index." Exit 0.

**EC-2.2 `index` with no write permission to `.codeintel/`**
- DB creation fails.
- Expected: `SemanticDb` raises, caught in CLI, prints error message, exits 1.

**EC-2.3 `query` with missing required flags**
- `codeintel query --op search` (missing `--target`)
- Expected: argparse prints usage and exits 2 (standard argparse behavior).

**EC-2.4 `query` when no engine is available**
- All engines unavailable (fresh machine, no graph/lsp installed, empty semantic DB).
- Expected: Gateway returns safe-null with `reason=engine-unavailable` or `no-result`. CLI prints "No result (reason: engine-unavailable)". Exit 0.

**EC-2.5 `status` with no index**
- `~/.codeintel/semantic.db` does not exist.
- Expected: Status prints "semantic: available (no index built yet)" or similar. Does not raise. Exit 0.

**EC-2.6 `index` embedding failure (fastembed not installed)**
- `fastembed` is absent.
- Expected: `Indexer._get_embedder()` raises `ImportError`, caught by `Indexer.index()`, returns -1. CLI prints "Indexing failed: fastembed not available — install with `pip install fastembed`". Exit 1.

---

## Phase 3–4 — Install subcommand

**EC-3.1 Settings file has non-JSON content**
- `~/.claude/settings.json` contains corrupted bytes.
- Expected: `json.load` raises `JSONDecodeError`. Caught by installer. Returns `{ok: False, action: "failed", reason: "JSONDecodeError: ..."}`. Existing file is NOT overwritten.

**EC-3.2 Settings file is JSON but not an object**
- `~/.claude/settings.json` = `[]` (JSON array).
- Expected: installer detects the root is not a dict, returns `{ok: False, action: "failed", reason: "expected object at root"}`. File not overwritten.

**EC-3.3 Settings directory not writable**
- `~/.claude/` exists but is read-only.
- Expected: `open(..., "w")` raises `PermissionError`. Caught; returns `{ok: False, action: "failed", reason: "PermissionError: ..."}`. CLI prints `✗ claude: failed — PermissionError`.

**EC-3.4 `install --agent bogus`**
- `codeintel install --agent bogus`
- Expected: argparse `choices` validation rejects it with a usage error. Exit 2.

**EC-3.5 `install --agent all` — one agent fails, others succeed**
- E.g. `~/.config/zed/settings.json` is read-only.
- Expected: Other three agents register successfully. Zed prints `✗ zed: failed — PermissionError`. Exit 0 (at least one succeeded).

**EC-3.6 Concurrent installs**
- Two `codeintel install --agent claude` processes run simultaneously.
- Expected: No data corruption — the last writer wins (both write the same content). Not a multi-process lock concern for v1 (rare edge case, acceptable).

**EC-3.7 `codeintel` binary not on PATH at registration time**
- The config is written but `codeintel` is not yet in the shell's PATH.
- Expected: Registration still completes (installer writes the config entry, not a validation of the binary). The agent host's config is correct; the user must ensure `codeintel` is on PATH before the agent tries to start it.
