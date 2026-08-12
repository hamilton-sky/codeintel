# codeintel — Planning & Implementation Assessment

*A three-angle review (planning quality · implementation critique · plan-vs-build fidelity), synthesized. Read-only review; no production code was changed.*

## Verdict at a glance

| Dimension | Grade | One-line |
|---|---|---|
| **Planning** | **A−** | Genuinely rigorous, uniform, executable plans. Top-decile for AI-generated planning. |
| **Plan→build fidelity** | **~96%** | The build faithfully delivered what the plans specified; all 86 tests green. |
| **Implementation (as a running system)** | **C+** | Several headline capabilities are non-functional or unsafe when actually run. |

**The paradox that defines this project:** the planning is excellent, the build is a faithful execution of the plan (96% of acceptance criteria delivered, 86/86 tests passing) — and yet the running tool has serious, specific defects. This is the classic gap between **"passes `pytest`"** and **"works when you run `codeintel serve` twice against two repos."** The plans and the tests validated each piece *in isolation*; nothing validated the integrated, real-world lifecycle (the transport rebuilding state per request, two real projects sharing one index, the real backend CLI contract). So the green suite *masks* the gaps rather than catching them.

---

## 1. Planning — A−

The planning artifacts (72 markdown files across 10 features) are the strongest part of the project.

**Strengths**
- **Verification-gated, scope-guarded phase decomposition, uniform across all 10 features.** Every phase carries a concrete "Done when" gate, a copy-pasteable verify command, an explicit scope guard ("Do NOT touch gateway.py / provider.py"), and a rollback note. This is directly buildable, not aspirational.
- **The safe-null / never-raise contract is threaded end-to-end as a *tested* invariant**, with a precise per-feature `reason` vocabulary (`engine-unavailable` / `project-not-indexed` / `timeout` / `warming` / `boot-failed` / `below-floor` / …) so an agent can pick a recovery action.
- **Non-filler edge cases and honest, trade-off-documented decisions** (Options → Chosen → Rationale → Trade-off), including a justified SPEC deviation (fastembed/ONNX instead of sentence-transformers, "~2 GB torch vs ~50 MB ONNX").

**Weaknesses**
- The **content-hash cache was scoped incoherently** (F4 made it per-gateway-instance; F6 later confirmed the server rebuilds the gateway every request — and fixed that reset problem *for reindex state* but left the cache behind).
- The **`.codeintel.toml` config plan is partially ceremonial** — loaded but never threaded into the Indexer/Searcher/Gateway; plus a default-value drift (`cosine_floor` 0.3 in F7 vs 0.25 in F5, never reconciled).
- **Uneven test-to-story coverage & label drift**: F3's project-root-switch story is never tested; F5 shipped a *failing* test punted to F9; the semantic `reason` name drifts across F5/F9 docs (`below-floor` / `no-index` / `empty-index`).

---

## 2. Implementation — C+

The skeleton is well-designed; the running system is not yet trustworthy.

**Genuine strengths**
- **The never-raise contract is real, not cosmetic** — defense-in-depth (provider → dispatch → query → handler), and `tests/test_never_raise.py` is true fault injection (monkeypatches `safe_null_result` to raise, injects `subprocess.TimeoutExpired`, drives a *live* HTTP server through a raising `Gateway.query`).
- **Clean, minimal `CodeProvider` protocol** (`provider.py:7-28`) uniformly implemented; excellent file-size discipline (largest file 235 lines vs the ~400 target).
- **The incremental semantic indexer's content-hash skip logic is correct and unit-tested** (`indexer.py:109-140`).

**The core weakness — state that must persist, doesn't**

`server.py:44` rebuilds the entire gateway (`_build_gateway()`) **on every request**, for both MCP and HTTP transports. The author clearly understood persistence — they hoisted `_REINDEXER = Reindexer()` to module scope precisely so it survives calls — but didn't apply the same to the `Gateway` / providers. Consequences:
- The **content-hash cache is cold on every request** → `cached: true` essentially never happens across an agent's calls.
- The **LSP engine is structurally non-functional through the running server**: a fresh `LspProvider` (and a fresh `uvx serena` subprocess) per call means every `op=symbol` returns `warming` forever and leaks a subprocess each time. This directly violates **F3's AC ("no per-request respawn")**.

---

## 3. Concrete bugs (prioritized)

Ranked by real-world impact. All are in the actually-running system, not hypotheticals.

1. **Cross-project semantic index corruption / data loss.** One global `~/.codeintel/semantic.db`, no `project_root` column (SPEC §6 *specified* one), no project filter in the KNN. `indexer.py:_cleanup_deleted` deletes globally — indexing repo B purges repo A's entire index; a search in B can return A's files. *(Data-safety regression.)*
2. **`index` and `search` use different DB files.** CLI/reindexer write `<project_root>/.codeintel/semantic.db`; the provider reads `~/.codeintel/semantic.db`. So `codeintel index .` is wasted work and `status`'s index-age is meaningless.
3. **Gateway/providers rebuilt per request** (`server.py:44`) → dead cache + non-functional LSP (see §2).
4. **Cache never busts on edit for symbol/free-text targets.** `cache.py:11-21` falls back to `sha256(target)` for anything that isn't a literal file path — i.e. every `impact`/`callers`/`symbol`/`search`, the SPEC's own headline examples. Edit the function, re-query → stale answer with `cached:true`. The one cache-bust test uses a file-path target (the only case that works), hiding it.
5. **Config file is fully built, documented, and completely disconnected.** `load_config()` is called once with the comment *"result unused here but exercises the path."* No `.codeintel.toml` key changes any runtime behavior.
6. **`op=pattern` is very likely dead against the real backend.** `graph.py:145` calls `search_code`/`pattern=`; the real `codebase-memory-mcp` tool is `search_graph`/`query`/`name_pattern` (verified against the live backend schema). Untestable by the current suite because it mocks `_run`.
7. **Never-raise leaks on the CLI `map` path.** `__main__.py:121-124` has no try/except and `mapper.py` assumes dict rows → a malformed backend response raises `AttributeError` straight through `main()`. (The MCP `code.map` handler *is* wrapped — the CLI path was just forgotten.)
8. **HTTP can crash instead of 4xx-ing** (contradicts F8 AC). `http_server.py:25` reads `int(Content-Length)` outside the try/except → a non-numeric header is an uncaught `ValueError`.
9. **Two unmet F4 ACs:** `op=overview` never falls back to lsp; fan-out reports `engine:"merged"` instead of the requested `both`/`all` — and the **test was written to match the drift**.
10. **Spec gaps:** `status` hardcodes `indexed:False, model:None`; `.gitignore` is *not* respected (only 3 hardcoded skip-dirs); semantic freshness runs **synchronously inline on every search** (no debounce, no deadline) — the opposite of the SPEC's "debounced, never blocks, deadline-bounded."
11. **`mapper.py` bypasses the protocol** — uses `GraphProvider._resolve_project`/`._run` private internals, breaking the swappability seam that is the whole point of the provider design (SPEC §5).
12. **Cypher injection** — `graph.py:96-98` interpolates `target` unescaped into `WHERE fn.name="{target}"`.

**Why the suite didn't catch these:** the tests never (a) call a handler twice to check cache/session persistence, (b) exercise two real project roots against the shared DB, (c) hit the real backend CLI argv/method contract (they mock `_run`), or (d) import/smoke-test the "primary" MCP stdio transport at all.

---

## Bottom line

**Was it well planned?** Yes — impressively. The process produced rigorous, uniform, executable plans and a build that hit ~96% of its own acceptance criteria with a real, tested safety invariant and clean architecture.

**Was it well built?** As a *scaffold*, yes. As a *running tool*, not yet — several of its headline differentiators (the cache, per-project isolation, LSP freshness, the config file) are non-functional or unsafe in production while passing every unit test.

**The meta-lesson (relevant to pathly itself):** faithful plan-execution and a green suite are necessary but not sufficient. The gaps here are precisely the ones that live *between* well-planned units — the integrated lifecycle, cross-entity state, and real external contracts — which is exactly what per-feature planning and per-feature unit tests structurally under-cover. A "wire it together and run it for real" integration phase (two repos, the live backend, the actual transport) would have caught nearly every item in §3.

**Recommended next moves (in impact order):** (1) make the semantic index project-scoped + unify the DB path [fixes bugs #1, #2]; (2) persist the gateway/providers as a module singleton like the reindexer [#3, and the LSP/cache consequences]; (3) fix the cache content-hash for non-file targets [#4]; (4) wire the config [#5]; (5) verify the graph backend method names + parameterize the Cypher [#6, #12]; (6) close the two never-raise/HTTP holes [#7, #8].
