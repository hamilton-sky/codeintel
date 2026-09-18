# Readiness evaluation — 2026-09-10, with a 2026-09-17 status pass

An external assessment of codeintel against two repositories the author did not write
(`daycap`, `bright-sky`), scoring it **7/10** and recommending it as an early-adopter beta.

It lived untracked at the repository root for a week. This is the salvaged half: the findings
that are still open, plus a ledger of the ones that closed, because a plan whose resolved items
are silently deleted cannot be audited and gets re-litigated instead.

## Status ledger — what closed, and where

| finding | status as of 2026-09-17 |
|---|---|
| **P0** Indexing reports success when no files are readable | **Closed** — `d1dfc15` fails closed on unreadable repositories. Re-verified: `bright-sky` now indexes 29,903 chunks in 12m44s where it previously reported `0 files, 0 chunks` and exited 0. |
| **P0** False-positive graph edges for qualified methods | **Partly closed** — `#34` makes the failure legible: a caller heading now reads `2 resolved · 43 name-matched · 5 unstated` above the rows, and `StrategyChain.resolve`'s two true callers rank first among unbadged rows. The *disclosure* is fixed; the *resolution* is still heuristic, so Phase 2 below stands. |
| **P1** Confidence visible but not actionable | **Closed** — `#44`. Not by adding the `min_confidence` filter this row asked for: an engine-side filter would drop rows, and the one thing measured across four repositories is that this engine is never wrongly silent. The rows ride the envelope instead (`rows[]`, `evidence`, `evidence_class`), so the caller filters and the tool keeps reporting everything it found. See Phase 3. |
| **P1** Background indexing state unclear | **Unverified** — not re-tested on 2026-09-17. |
| **P1** Full-suite performance or hang risk | **Closed** — `#39`. It never hung: it completed in 672s, twice measured. Three tests queried the gateway with a real root, each firing a full background reindex of this checkout (146s, 154s) on daemon threads nothing joined, which starved `test_hard_exit`'s nested pytest at the 47.8% mark. Suite now 219s. |
| **P2** SQLite resource warnings | **Closed** — `#39`, and misnamed: not one came from sqlite. Sixteen HTTP listeners kept open by `shutdown()` without `server_close()`, one template file, eight bare `open(...).read()`, two MCP children killed without being waited on. `ResourceWarning` is an error in the suite now. |

Two findings the original assessment could not have had, both fixed since:

* An empty LSP reference list was rendered as `## References (0)` at `confidence: complete` — a
  confident "nothing references this" about the question asked immediately before deleting code.
  Closed by `#33`, which added the `unresolvable` outcome kind.
* `bench/oracle_py.py` scored a function's own call sites as fabricated callers, understating the
  measured engine by 10–60 points. Closed by `#35`. The corrected numbers are in
  [`bench/README.md`](../bench/README.md).

## Measured position, 2026-09-17

Against `codebase-memory-mcp 0.10.8`, stratified targets, `graph` arm:

| repository | direct precision / recall | wrongly silent |
|---|---|---|
| `daycap` (TypeScript, 23 source files) | 100% / 100% | 0 / 8 |
| `snitch-simulator` (Python) | 100% / 100% | 0 / 6 |
| `pathly-adapters` (Python, 3,284 files) | 90% / 100% | 0 / 10 |
| `corpus-ts` (fixture, smoke test) | 50% / 80% | 0 / 3 |

Across 27 scored symbols on four repositories, **zero wrongly silent** — the deletion trap did not
open once. The remaining precision loss is concentrated in short, colliding names, which is what
the target lists are stratified onto deliberately.

---

## Phase 3: Improve trust and result ergonomics — **delivered** (`#44`)

Priority: **P1**

> All three acceptance criteria hold. Two items are delivered differently from how they are
> written below, and one part of item 4 is refused outright; each is stated at its own item rather
> than quietly adjusted.
>
> Writing it surfaced two defects in its own first draft, both the shape this repository keeps
> finding — a summary true of a proxy and read as a claim about something else:
>
> * `impact` renders callers and callees into one body and recorded only the second half, so
>   `evidence.returned` was 3 over a body printing 6. The rows are now recorded from the list that
>   was handed to `_display`, after it was displayed, so `rows` and the `- ` lines are the same
>   rows by construction rather than by two derivations agreeing.
> * `safe_for_destructive` was computed while the answer was still being rendered, and three of the
>   gaps an edge answer can raise are recorded after its rows are printed. Measured: an answer over
>   two same-named symbols came back `confidence: partial`, `gaps: [target-ambiguous]` and
>   `safe_for_destructive: true` — the envelope contradicting itself in the one direction that ends
>   in a deletion. It is settled in `build_result` now, where the answer is whole.
>
> Both are pinned: `test_the_evidence_summary_agrees_with_the_rows_it_summarises` and
> `test_the_envelope_never_calls_an_answer_safe_while_calling_it_partial`, with the `evidence`
> summary registered in `tests/test_summary_integrity.py`'s census so the next one fails on the
> commit that adds it.

### 1. Make the first screen trustworthy — delivered

> A `>`-quoted block above the heading, on `callers` / `callees` / `impact`, carrying exactly the
> lines below. **Silent on a clean answer**: a banner over every result is furniture, and furniture
> is not read — the same argument `_evidence_headline` makes for its own silence on a single
> bucket. One banner over an `impact` answer, not one per half.

Start every structural result with a compact confidence summary:

```text
Confidence: partial
Verified callers: 2
Possible callers: 48
Truncated: yes
Safe for destructive decisions: no
```

Detailed explanations can follow afterward.

### 2. Distinguish discovery from proof — delivered, on both channels

> In **two** places, because the choice and the answer happen at different moments. `code.query`'s
> `op` description labels each op, which is what an agent reads *before* it picks one; the envelope
> carries `evidence_class` on every answered result, which is what it reads after.
>
> The envelope's label is **not** the op's. An op-keyed constant would say `evidence` on every
> `callers` result including the 48-row one in which two rows were callers — true of the op and
> false of the answer, which is the substitution this document's own findings are made of. So the
> op supplies a ceiling (`provider._OP_CEILING`, pinned against the tool description by
> `test_every_op_is_labelled_with_what_its_answer_supports`) and the answer supplies the verdict:
> `callers` comes back `evidence` when every row followed a real binding and `advisory` when it did
> not. Nothing partial is ever `evidence`.
>
> `impact`, `context` and `chain` stay `advisory` whatever their rows say, as written below. Their
> rows still carry per-row `verified`, so the evidence-grade subset is reachable — a narrower and
> more honest claim than the whole answer being proof.
>
> "Explicitly recommend verification" is the first screen's `Safe for destructive decisions: no`,
> the `Settle it:` command `#42` added, and the MCP instructions telling an agent to require
> `evidence_class: "evidence"` before deleting or renaming.

Label operations by intended use:

- Discovery: semantic search, pattern search, overview, hotspots.
- Evidence: LSP definition and references, high-confidence graph edges.
- Advisory only: heuristic impact and call chains.

For destructive questions, explicitly recommend verification when evidence is incomplete.

### 3. Return structured confidence metadata — delivered, less one field

> `rows[]` on the envelope, one entry per printed row, carrying `relation`, `name`,
> `qualified_name`, `file`, `module_scope`, `edge`, `verified`, `evidence`, `strategy`,
> `confidence` and `why`. `relation` is not on the list below and is needed by it: `impact` answers
> callers and callees into one body and therefore one `rows` array, and without it the two
> questions that op exists to keep apart are one undifferentiated list.
>
> **The receiver/type field is refused.** The backend reports no receiver type, so the key would be
> `null` on every row of every answer. "When available" is the honest qualifier and it evaluates to
> never; a field that promises a capability nobody has is worse than its absence, because the
> absence is at least legible.

Do not require agents to parse explanatory prose. Each row should include:

- Resolver strategy.
- Numeric confidence.
- Verified boolean.
- Edge type.
- Reason for inclusion.
- Receiver/type evidence when available.

### 4. Improve truncation behavior — delivered, less the cursor

> **No continuation cursor.** "Where supported" is the load-bearing phrase: `codebase-memory-mcp`
> offers none, and the only way to produce one here is to invent paging over a `LIMIT 50` query —
> a second query returning a second arbitrary fifty with no guarantee it excludes the first. That
> is a cursor in name, and a caller would page it believing otherwise.
>
> The rest holds. `evidence.total` is `null` **exactly** when the backend's own cap was hit, which
> is the case where the size is genuinely unknown; our candidate cap is the other case and those
> rows *are* in hand, so `total` states them and `total > returned`. Rows are sorted resolved
> first, within direct calls first, so a reader who stops early stops on the evidence.

When results are capped:

- Return a continuation cursor where supported.
- Keep exact total counts separate from returned-row counts.
- Never describe a capped list as complete.
- Prefer verified rows before heuristic rows.

Acceptance criteria:

- ~~An agent can safely filter results using structured fields only.~~ **Met** —
  `test_an_agent_can_filter_on_structured_fields_alone`, which checks the filter against the prose
  rather than against itself: filtering `rows[]` on `verified` returns the same set as reading the
  badges out of the body.
- ~~Qualified caller queries show verified results before possible matches.~~ **Met** —
  `test_qualified_caller_queries_show_verified_results_before_possible_ones`, over rows interleaved
  on the way in so passing cannot be an accident of input order.
- ~~Truncation cannot be mistaken for completeness.~~ **Met** —
  `test_truncation_cannot_be_mistaken_for_completeness`, checked in all four channels that could
  claim otherwise: `evidence`, `confidence`, `gaps` and the rendered first screen.

## Phase 4: Stabilize test and resource behavior — **delivered** (`#39`)

Priority: **P1**

> Every acceptance criterion below is met. The isolation step it proposes — bisect by directory —
> was not the route: the suite was instrumented instead, because the reported symptom contained
> one wrong inference (it never hung) and a bisect would have inherited it. Per-test timeouts are
> `pytest-timeout` at 180s with `timeout_method = signal`, so one overrun fails one test rather
> than the session.

### 1. Isolate the full-suite stall

Run the suite with duration and timeout reporting:

```bash
pytest -vv --durations=50 --timeout=60
```

Then bisect by test directory or file until the CPU-heavy test is identified.

Investigate:

- Reindexing loops.
- Process pools or excessive parallel workers.
- Semantic model initialization.
- Graph-backend subprocess cleanup.
- Recursive filesystem watching.
- Tests waiting on MCP processes that never exit.

### 2. Add per-test timeouts in CI

Use a timeout plugin or explicit process timeout for integration tests. Mark slow and live-backend tests separately so unit tests remain quick and deterministic.

Suggested groups:

- Unit: no external process or downloaded model.
- Integration: local backend processes.
- Live: installed graph/LSP engines.
- Release canary: built-wheel end-to-end verification.

### 3. Close SQLite connections deterministically

Audit semantic database construction and teardown:

- Add context-manager support.
- Make `close()` idempotent.
- Ensure providers close owned connections during shutdown.
- Ensure tests use fixtures that always finalize connections.
- Turn `ResourceWarning` into an error in the relevant test group.

Acceptance criteria:

- Full suite completes within a documented time budget.
- No test can run indefinitely.
- No unclosed SQLite connection warnings remain.
- MCP server shutdown leaves no child processes or database handles behind.

## Phase 5: Strengthen onboarding and documentation — **delivered**

Priority: **P2**

> All four items ship as [`docs/trust.md`](trust.md), linked from the README's status banner and
> listed first in the docs index. Its claims are checked by `tests/test_docs_trust_claims.py`:
> every `reason` and gap kind it names is one the product emits, every command it shows is parsed
> against the real CLI, all eight states are present, and each degraded one carries a command —
> which is this phase's own acceptance criterion, enforced rather than asserted.

### 1. Set precise expectations

Keep the existing honest beta language and add a short trust model:

- Semantic and pattern search locate candidates.
- LSP results are preferred for definitions and references.
- Graph results depend on resolver evidence.
- Low-confidence impact results must be verified.

### 2. Add a five-minute verification workflow

Ask new users to select a symbol whose callers they already know and run:

```bash
codeintel doctor --deep /path/to/repo
codeintel query --op context --target KnownSymbol /path/to/repo
```

The guide should explain how to read confidence, gaps, possible matches, and index freshness.

### 3. Document supported and degraded repository states

Cover:

- Fully indexed and healthy.
- Graph-only.
- LSP-only.
- Semantic-only.
- Reindexing with a usable stale snapshot.
- Permission failure.
- Empty or unsupported repository.
- Partial parser coverage.

### 4. Provide friend-ready setup instructions

Recommended flow:

```bash
pip install codecortex
codeintel setup --all /path/to/project
codeintel doctor --deep /path/to/project
codeintel install --agent codex
```

Tell the user to restart the agent after registration and verify a known caller before trusting impact analysis.

Acceptance criteria:

- A new user can install, diagnose, and verify the tool without reading internal architecture documentation.
- Every common failure includes one concrete next action.
- Documentation never implies that heuristic graph edges are authoritative.

## Phase 6: Release-readiness gates — **open**, and the only phase left

Priority: **P2**

Create explicit promotion levels.

> Status pass 2026-09-17, after `#44` closed Phase 3. Every gate below is marked from something
> checked rather than from the phase ledger above it — a promotion level inferred from "the phase
> that was supposed to deliver it merged" is the same substitution this document's own findings are
> made of.
>
> **Beta / early adopter is met on all four gates.** Of the ten gates above that level, **seven are
> met, one is refused as written, one is partly met, and one is open** — and the one that is open
> is the one no commit can close.
>
> | level | gate | status |
> |---|---|---|
> | beta | safe envelopes | **met** — never-raise + safe-null, and a null result carries `reason`/`hint` |
> | beta | honest partial-confidence reporting | **met** — `confidence`/`gaps`, and since `#44` `rows[]`/`evidence`/`evidence_class` |
> | beta | graph, LSP and semantic all work locally | **met** — `doctor --deep` (`#41`) puts a real question to each and requires content |
> | beta | focused integration coverage | **met** — live contract jobs for both backend dialects and for LSP run on every PR |
> | rec. beta | qualified-method false positives fixed or excluded by default | **refused as written** — see below |
> | rec. beta | zero-readable-file indexing fails clearly | **met** — `d1dfc15` |
> | rec. beta | background index state durable and inspectable | **partly** — inspectable, not durable |
> | rec. beta | full suite completes reliably | **met** — `#39`; 1,593 tests in 226s locally, four Pythons green in CI |
> | rec. beta | SQLite warnings eliminated | **met** — `#39`, and `ResourceWarning` is an error in the suite |
> | general | precision benchmark, multiple real Python and TS repos | **met** — four arms, two of each language, 27 scored symbols |
> | general | verified-caller precision ≥ 95% | **met** — threshold agreed at 95% (2026-09-18) and enforced by the benchmark's exit code; 100% / 100% / 97% |
> | general | large-repo performance budgets documented and enforced | **met** — re-measured at 0.23.4, and the per-query work budget runs in CI |
> | general | upgrade and uninstall paths tested | **met** — upgrade tested end to end on all four agents; `codeintel uninstall` now exists and is tested |
> | general | several external users set up unassisted | **open** — the one gate no commit can close |
>
> **Why the false-positive gate is refused rather than failed.** "Excluded by default" is a
> behaviour this project has deliberately chosen against: dropping a name-matched row trades a false
> positive for a false negative, and before a delete "no callers" is the more dangerous of the two.
> The measured position is **zero wrongly silent across 27 symbols on four repositories** — that is
> what exclusion-by-default would spend. `#44` resolves the tension instead of picking a side: the
> rows are still all returned, and the *caller* can now exclude them with `rows[].verified` or
> `evidence_class`. The underlying resolution is still heuristic, which is Phase 2 and stands.
>
> **The precision gate, now measured.** The gate asks for *verified-caller* precision, and every
> other number in this document is precision over **all** rows — a different quantity that had been
> standing in for it. `bench/score.py` now carries a fourth arm, `graph_verified`, which applies
> `rows[].verified` and scores what is left. Measured 2026-09-17 on the same trees and backend:
>
> | repository | `graph` direct | `graph_verified` direct | wrongly silent |
> |---|---|---|---|
> | `daycap` | 100% / 100% | **100% / 100%** | 0 / 8 |
> | `snitch-simulator` | 100% / 100% | **100% / 100%** | 0 / 6 |
> | `pathly-adapters` | 90% / 100% | **97% / 78%** | 0 / 10 |
> | `corpus-ts` (fixture) | 60% / 86% | **75% / 43%** | **1 / 4** |
>
> **The threshold is 95%, agreed 2026-09-18 by the repository owner.** The gate had read "an agreed
> threshold" for eight days with nobody having agreed one, which is a gate that cannot be failed and
> therefore cannot be passed. It is now `score.VERIFIED_PRECISION_FLOOR`, and it is enforced by the
> benchmark's **exit code** — `python bench/run.py <repo>` returns 1 when the floor is not met, so
> it can be a release step rather than a number someone reads. Exit 2 stays what it always was: the
> harness could not run, which is a different fact from an engine scoring badly.
>
> It applies to `graph_verified`'s DIRECT-caller precision and nothing else: not impact (a
> recall-first question with the opposite failure cost), not the unfiltered `graph` arm, and not
> `wrongly silent`, which is a separate count precisely because averaging it into a precision figure
> would bury it. A FAIL prints that caveat beside itself, because raising precision by dropping rows
> is always available and this gate does not read the column that prices it.
>
> Three of the four arms are held to it and all three pass: **100% / 100% / 97%**. `corpus-ts` is
> exempt, and the exemption is the part worth scrutinising — excluding the arm that fails is a
> suspicious move in general. It is four hand-written symbols chosen to be maximally adversarial in
> a 24-file tree written to have a known answer; holding it to a production floor would either force
> the fixture to be made easier, destroying what it is for, or park the gate permanently red. So its
> verdict line still prints on every run, saying it was not applied and why, and a test pins the
> exemption list at exactly one entry.
>
> **No pooled cross-repository figure is printed and none is invented here** — the arms have
> different symbol counts and averaging them would be its own summary defect. The floor is applied
> per repository, which is also the only way a single bad tree cannot be averaged out of view.
>
> The result worth reading twice is the last column, and it has since been corrected by its own
> instrument. On the first run of this arm it read **0 everywhere**, and this document said so:
> filtering to verified rows had never emptied a caller list across 27 symbols. That was true, and
> it was true of the POPULATION rather than of the filter — every scored symbol either had verified
> callers or had no callers at all, so the filtered arm had nothing it could silence and a column
> that cannot move was being read as reassurance.
>
> `settleQueue` was added to the `corpus-ts` fixture to remove that: a symbol reached only through a
> re-export facade, so both its caller edges bind by bare name, with two real calls as truth.
> `graph` returns both; `graph_verified` returns nothing. The column reads **1 / 4** on the fixture
> and still 0 on the three real repositories.
>
> So exclusion-by-default costs a wrongly-silent answer on a shape that is constructed but not
> exotic — a re-export chain is ordinary in both languages here. That prices the standing argument
> for keeping heuristic rows in the default answer; it does not settle it, and one fixture symbol is
> not a rate. The rows the filter drops elsewhere remain *additional* callers of symbols that also
> had verified ones.
>
> **What "inspectable, not durable" means.** `Reindexer.reindex_pending` answers whether a root is
> being rebuilt, and an answer served in that window carries `reindexing: true` plus a hint saying
> the index is as of the last completed pass. That is the inspectable half and it is real. The
> durable half is not: the in-flight set is an in-process dict, so a restarted server reports no
> reindex in progress whether or not one was interrupted. The ledger's "not re-tested on
> 2026-09-17" also still stands for the originally reported symptom.
>
> **Performance budgets, closed on both halves.** *Documented*: `docs/benchmarks.md` is re-measured
> at 0.23.4 on the same machine and corpus family — 29,903 chunks indexed in 607 s at 49.3
> chunks/sec, 1.72 GB peak RSS, a 68.1 MB index, and warm queries at **p50 233 ms / p95 242 ms**
> pooled over four passes.
>
> The re-measurement settled the prediction that document had been carrying. It said `_verify`'s
> per-candidate disk reads should have made queries slower, direction knowable and magnitude not.
> **The regression is not observable**: 233 ms against 235 ms at 0.10.0, because the CPU query embed
> (~230 ms) dominates reading sixty short spans by two orders of magnitude. Sound about direction,
> wrong about whether it would show — which is why "re-measure" was the right instruction and
> "adjust" would not have been.
>
> *Enforced*: not as a wall-clock number. A latency assertion on a shared CI runner is a summary
> whose referent is the RUNNER — true of that machine on that morning, read as a claim about the
> code, and flaky as well. `tests/test_query_budget.py` counts the work instead, and the counted
> quantities are the ones that actually regress: one file read per candidate, a read set flat in
> corpus size, one embed per search, one vector query per search, and `rerank=off` reading only `k`.
> Those are identical on a laptop and on a cold runner, so they run in the ordinary suite across
> four Python versions rather than in a special job nobody trusts.
>
> That also converts the latency result above from luck into an invariant. It holds *because* the
> read set is bounded by the candidate limit; the test is what keeps that true.
>
> **Upgrade: tested.** `tests/test_upgrade_path.py` drives the sequence a person actually performs
> — install, the binary moves, `doctor` names the stale launch command, re-run install, `doctor` is
> clean — against a real executable created, registered, deleted and recreated elsewhere, for **all
> four agents**. Both halves had been tested before and neither had been tested as that sequence:
> the repair was proven for `codex` alone, the one agent whose config is TOML, while `claude`,
> `gemini` and `zed` write JSON through a different path in two different shapes. A repair that
> wrote a second entry, or wrote to a file other than the one `doctor` reads, passes every isolated
> assertion and fails the loop.
>
> Nothing was broken. Eighteen tests passed on the first run, and that is the result: the promise
> `docs/install.md` makes held for the three formats nobody had run it against, and now has
> something standing behind it. A negative control (move the binary, skip the reinstall) proves the
> loop can fail.
>
> **Uninstall: built, then tested.** This was the one place the gate asked for a feature rather
> than a test — `reset` removed caches and `install` wrote registrations, but nothing was the
> inverse of `install`, so "uninstall tested" could not be closed by testing anything. `codeintel
> uninstall` now is that inverse, and its whole risk is one sentence: it edits a user-owned config
> file holding other people's servers and settings. So the tests are mostly about what it must not
> do, and three of those are refusals rather than features:
>
> * it never deletes the config file, **including when codeintel was its only entry** — removing
>   somebody's `~/.claude.json` because we happened to be its last server is a far larger action
>   than the one asked for;
> * it never rewrites a file it could not fully parse. Zed's JSONC (rewriting it through
>   `json.dumps` would silently delete the user's comments, which in Zed's default config is most
>   of the file) and two same-named TOML tables are both refused, with the block to remove by hand
>   — the same refusals `install` makes, for the same reasons;
> * it does **not** delete the index. That is `reset`'s, it costs ten minutes to rebuild on a large
>   repo, and a command called "uninstall" destroying it silently is the surprise this project
>   spends its comments avoiding. The location and the `reset --all` command are printed instead.
>
> `--agent auto` resolves to *where codeintel is registered*, not to *what is installed here* —
> which is the question `install` asks and the wrong one twice over: an agent removed from the
> machine still holds the entry, and an agent present but never registered has nothing to remove.
>
> No confirmation prompt, unlike `reset --all`, and that asymmetry is deliberate: this edits one
> entry and `codeintel install` puts it back, which is asserted rather than assumed. `--dry-run` is
> there for looking first.

### Current: beta / early adopter — **met**

Requirements:

- ~~Safe envelopes.~~
- ~~Honest partial-confidence reporting.~~
- ~~Working local graph, LSP, and semantic engines.~~
- ~~Focused integration coverage.~~

### Recommended beta — 3 of 5 met, 1 refused, 1 partly

Requirements:

- Qualified-method false positives fixed or excluded by default. — **refused as written**; the
  exclusion is now the caller's to make, and the resolution is Phase 2.
- ~~Zero-readable-file indexing fails clearly.~~ `d1dfc15`
- Background index state is durable and inspectable. — **partly**: inspectable per answer,
  process-local rather than durable.
- ~~Full suite completes reliably.~~ `#39`
- ~~SQLite warnings eliminated.~~ `#39`

### General recommendation — 4 of 5 met

Requirements:

- ~~Precision benchmark across multiple real Python and TypeScript repositories.~~ four arms,
  `bench/README.md`.
- ~~Measured verified-caller precision above an agreed threshold, preferably 95% or higher.~~ —
  **met**: threshold agreed at **95%** on 2026-09-18, enforced by `bench/run.py`'s exit code, and
  cleared by all three gated arms (100% / 100% / 97%).
- ~~Large-repository performance budgets documented and enforced.~~ — **met on both halves**:
  re-measured at 0.23.4 in `docs/benchmarks.md`, and enforced as a counted per-query work budget in
  `tests/test_query_budget.py` rather than as a wall-clock number a shared runner cannot hold.
- ~~Upgrade and uninstall paths tested.~~ — **met**: the upgrade loop end to end on all four agents
  (`tests/test_upgrade_path.py`), and `codeintel uninstall` — which had to be built before it could
  be tested — in `tests/test_uninstall.py`.
- At least several external users have completed setup without maintainer assistance. — **open**,
  and the only gate in this document that no commit can close.

## Suggested execution order

### Milestone 1: Safety patch — **delivered**

- ~~Fix permission and zero-file indexing behavior.~~ `d1dfc15`
- Prevent repeated background-index restarts. — **not re-verified**
- ~~Separate verified from heuristic graph counts.~~ `#34`
- Add the `StrategyChain.resolve` regression fixture. — **still open**: the case is hand-checked
  and written up in `bench/README.md`, but it is scored by no arm. Establishing truth at that
  scale is the blocker.

Release criterion met: `bright-sky` now indexes 29,903 chunks where it previously reported zero
files and exited successfully.

### Milestone 2: Precision release

Target: following release

- Add confidence filtering.
- Tighten class-qualified method resolution.
- Prioritize high-confidence results.
- Add structured per-edge evidence.
- Measure precision on `daycap`, `bright-sky`, and the internal fixture corpus.

Release criterion: qualified method queries no longer headline unrelated suffix matches as callers.

### Milestone 3: Reliability release

Target: following release

- Resolve the full-suite performance stall.
- Add test timeouts and test categories.
- Eliminate database resource warnings.
- Document performance budgets.

Release criterion: clean lint, type check, unit suite, integration suite, and release canary on a clean machine.

### Milestone 4: Friend-ready release

Target: after external validation

- Run onboarding tests with several developers.
- Collect setup failure telemetry locally and privately, or provide an opt-in diagnostic bundle.
- Refine documentation based on real installation attempts.
- Publish a clear support matrix and known limitations.

Release criterion: multiple external users install, index, query, and upgrade without maintainer intervention.

## Validation matrix

| Capability | Daycap | Bright Sky | Required target |
|---|---:|---:|---:|
| Health diagnostics | Pass | Pass with semantic failure correctly shown | Pass |
| Architecture overview | Pass | Pass | Pass |
| Exact definition | Pass | LSP path resolution failed for qualified method | Pass |
| Exact references | Pass | Not retrieved — now DISCLOSED as `unresolvable` rather than reported as zero (`#33`) | Pass |
| Direct callers | Strong (100%/100% measured) | Many false positives, now counted separately in the heading (`#34`) | High precision |
| Direct callees | Strong with caveats | Core callee found; noise present | High precision |
| Semantic search | Pass | **Resolved** — 29,903 chunks indexed 2026-09-17 | Clear success or failure |
| Pattern search | Pass | Pass | Pass |
| Hotspots | Useful | Useful | Pass |
| Changed-file impact | Pass for observed state | Not fully assessed | High precision |
| Indexing UX | Pass | **Resolved** (`d1dfc15`) | Fail safely |
| Full automated suite | Did not complete during assessment | N/A | Reliable completion |

## Metrics to track

Track these per release:

- Verified caller precision and recall.
- Heuristic caller precision and recall.
- Percentage of edges with resolver evidence.
- Qualified-query false-positive rate.
- Indexing success rate.
- Zero-file scan count by reason.
- Median and p95 indexing time by repository size.
- Median and p95 query latency by operation.
- Background-index retry count.
- LSP boot success rate.
- Unclosed resource warnings.
- Full-suite duration and slowest tests.
- Percentage of queries returning partial confidence.

## Final recommendation

*(Rewritten 2026-09-17. The original closed on "complete the two P0 items"; both have since been
addressed, so the gate it named has moved.)*

Offer codeintel to technically confident early adopters now, with one instruction: **read
`confidence` and `gaps`, and treat a caller count as a lead rather than an authority.** Measured
accuracy is bimodal and the discriminator is knowable before asking — a symbol whose leaf name is
unique in the index resolves essentially exactly; a colliding method name in a large tree does not,
and now says so on its first line.

*(Gate updated again. Both items this section named on 2026-09-17 have since closed.)*

The gate Phase 4 named — resolve the stall, eliminate the resource warnings — is met: `#39`. The
suite completes in 219s, `ResourceWarning` is an error, and no test can run longer than 180s.

The barrier to recommending it broadly was **not accuracy** — it was setup, and specifically three
configuration mistakes that produce a plausible *wrong answer* rather than an error: a language
missing from `.serena/project.yml`, a TypeScript tree with no `tsconfig.json`, and a repository not
indexed standalone. `doctor` catches all three, and as of `#41` `--deep` no longer takes a booted
process for a working one — it puts a real query to each engine and requires content. Phase 5 is
delivered: [`docs/trust.md`](trust.md) is what a stranger reads first.

What remains is **Phase 6**, and the ordinary work in it is done. External validation — several
people installing and verifying without the maintainer — is the only gate still open, and it is the
one no commit can close. One gate sits at "partly" and it is not waiting on coverage: background
index state is inspectable per answer but process-local, so a restarted server reports no reindex in
progress whether or not one was interrupted. Making it durable is a design change, not a test.

One gate is refused rather than failed, and that is worth re-reading before anyone counts it as
outstanding: excluding heuristic rows by default is a behaviour this project has declined, and
`bench/README.md` now prices what declining it costs. The beta / early-adopter level is met on all
four of its gates, which is the level this document recommends the tool at.

The verified-caller precision gate is closed: measured by `graph_verified`, held to a **95% floor
agreed on 2026-09-18**, and enforced by `bench/run.py`'s exit code rather than by someone reading a
table. All three gated arms clear it — 100% / 100% / 97%. The target whose callers are *all*
name-matched was added on the way (`settleQueue`, in the checked-in fixture), turning that arm's
`wrongly silent` column from a zero that could not move into a `1 / 4`.

Phase 3 closed in `#44`. One thing it asked for does not exist and one cannot be built honestly:
the receiver/type evidence field has no backend behind it, and a continuation cursor over a
`LIMIT 50` query would page nothing. Both are recorded at their items rather than dropped.

## Recommended message to an early-adopter friend

> I am testing a local code-intelligence tool for coding agents. It combines architecture maps,
> language-server definitions and references, semantic search, caller/callee graphs, and
> change-impact analysis. It is genuinely useful for exploring an unfamiliar repository. It is
> still beta: caller answers head with a breakdown of how each row was resolved, and only the
> `resolved` rows followed a real import or language-server binding — treat the rest as leads to
> verify, not as facts, especially before deleting or refactoring anything.
