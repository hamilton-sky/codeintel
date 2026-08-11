# User Stories — Debounced fire-and-forget incremental reindex seam

## Story 1 — Stale-result elimination after file edit

**As** a developer using a code-intel tool,
**I want** my follow-up query to reflect a file I just edited,
**so that** I get accurate results without manually triggering a reindex.

### Acceptance Criteria

- AC1.1: After editing a file and re-querying within the same session, the result reflects the
  change within at most one debounce window (default 30 s).
- AC1.2: The query response is not delayed — reindex runs off-thread and returns before or
  concurrently with the response.
- AC1.3: If no files changed since the last reindex, no new reindex run starts.

---

## Story 2 — Non-blocking queries

**As** a developer,
**I want** every query to return immediately even while reindexing is in progress,
**so that** my workflow is never blocked by background index maintenance.

### Acceptance Criteria

- AC2.1: `Gateway.query()` calls `maybe_reindex()` and returns a result without waiting for
  reindex to finish.
- AC2.2: Reindex runs in a daemon thread; process exit does not hang waiting for it.
- AC2.3: If a reindex is already running for the same `project_root`, a new call does NOT
  start a second concurrent run (debounce guard).

---

## Story 3 — Config gate

**As** an operator or CI pipeline,
**I want** to disable background reindexing with a single config flag,
**so that** tests and controlled environments get deterministic, no-background-IO behavior.

### Acceptance Criteria

- AC3.1: Setting `CODEINTEL_REINDEX=off` (env var) disables all background reindex calls.
- AC3.2: With reindex disabled, `maybe_reindex()` is a no-op and returns immediately.
- AC3.3: All existing tests pass unchanged with `CODEINTEL_REINDEX=off` in the environment.

---

## Story 4 — Never-raise contract

**As** the codeintel gateway,
**I want** `maybe_reindex()` to never raise an exception,
**so that** a reindex failure never propagates to the caller or corrupts a query response.

### Acceptance Criteria

- AC4.1: Any exception inside the background reindex thread is caught and logged; it does not
  propagate.
- AC4.2: `maybe_reindex()` itself never raises, even if the thread pool is exhausted or the
  project root does not exist.
- AC4.3: On reindex failure the `ContentHashCache` in `Gateway` is NOT cleared — prior cached
  results remain valid.
