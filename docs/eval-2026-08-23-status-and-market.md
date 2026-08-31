# codeintel — status and market position, 2026-08-23

> **Status: historical record.** A point-in-time snapshot, kept because the reasoning is worth having and correcting it would falsify the record. Numbers and version claims below were true when written and are NOT current — codeintel is at 0.22.0. Where it disagrees with a reference doc, the reference doc wins.
>
> **What became of the two defects in §2.**
>
> * **D1 — redaction corrupting answers under `HOME=/root`: fixed.** Both substitutions are now
>   boundary-anchored, so `/rootfs/etc/config.py` survives intact, and `_flattened_home` refuses to
>   redact a *single-segment* home at all — flattened, `/root` is a bare English word, and replacing
>   it rewrote ordinary prose ("root cause analysis" → "`<home>` cause analysis"). `tests/test_redaction_boundary.py` pins both. The report's other suggestion — exempting `reason` outright — was not
>   taken and is no longer needed, since anchoring stops a reason code from matching.
> * **D2 — `no-index` sitting outside the could-not-ask family: resolved, though not the way the
>   report proposed.** Widening the family to admit `no-index` would have been wrong: a pass that
>   *completed* and found nothing to embed genuinely is an answer about the repository. The real
>   defect was one level down — `no-index` was **overloaded**, covering both that case and an index
>   pass that ran and failed, because the provider discarded `Indexer.index`'s `-1` and its
>   `last_error`. A failed pass is now `index-failed`, which carries the cause in its `hint` and
>   *is* in both could-not-ask families (the gateway's `unreachable` set and the cold tier's
>   `_COULD_NOT_ASK`). `no-index` stays out of them, deliberately.
>
> The three source references in §6 have drifted with the code and no longer point at what they
> named — `semantic.py:158` and `gateway.py:181` in particular. Grep for the symbols rather than
> trusting the line numbers.
>
> Superseded in two ways worth naming: the graph backend's guessed-edge problem described here is now **disclosed** rather than silent (see [graph.md](graph.md#relationship-kind-and-how-an-edge-was-resolved)), and it is **measured** rather than argued about (see [../bench/README.md](../bench/README.md)).

**Subject:** codeintel / `codecortex` 0.15.5 (released 2026-08-19)
**Method:** the repository read at `b038efa`; the suite run in a clean venv on a fresh Linux
container (Python 3.11, running as `root`, no outbound access to the model host); release and
repository facts read from the PyPI and GitHub APIs; the competitive landscape from web research.
**Standard applied:** every number below was measured or read from an API, except §4, which is
second-hand and marked as such throughout.

This is a dated record in the sense of [`eval-2026-08-17.md`](eval-2026-08-17.md) — a point-in-time
measurement, not a living reference doc. It does not describe intended behaviour; it describes what
was true on 2026-08-23.

---

## 0. Executive summary

The engineering is well past the adoption. Those are the two halves of the status, and they should
be read separately, because improving one does nothing for the other.

| | Measured |
|---|---|
| Release | 0.15.5 on PyPI, 28 releases in 8 days (0.2.0 on 2026-08-12 → 0.15.5 on 2026-08-19) |
| Source / tests | 9,847 LOC across 50 modules · **12,533 LOC of tests**, 807 collected |
| Suite here | 779 passed · 12 failed · 16 skipped · 85.10% coverage (floor 83%) |
| CI | 138 runs; `main` green; lint / mypy / 3.11–3.13 / graph-contract / release-consistency all gating |
| Adoption | **0 stars · 0 forks · 0 watchers · 0 issues · 1 human contributor** |
| Quiet since | 2026-08-19 — 4 days |

Twelve failures, three of them this container's fault and **nine of them two real defects** (§2).
Both are the project's own recurring defect class — a fact asserted about the world by code that
never checked it — and both were surfaced by nothing more exotic than an environment the author had
not run in. That is the fifth consecutive time an unfamiliar environment has produced defects on
first contact, which is the single most important fact in this document: **the curve has not
flattened**, and the README is right to say so.

---

## 1. Status, measured

### 1.1 Release and repository

| Fact | Value | Source |
|---|---|---|
| Distribution | `codecortex` (the name `codeintel` is taken; CLI and import stay `codeintel`) | PyPI |
| Latest version | 0.15.5, uploaded 2026-08-19T06:28:22Z | PyPI API |
| Releases | 28, first `0.2.0` at 2026-08-12T13:35:15Z | PyPI API |
| Repository created | 2026-08-12T06:34:07Z | GitHub API |
| Last push | 2026-08-19T06:27:25Z | GitHub API |
| Licence | MIT | GitHub API |
| Classifier | `Development Status :: 4 - Beta` | `pyproject.toml` |

Twenty-eight releases in eight days is not instability — read against the CHANGELOG it is a
tight find-fix-ship loop, and the release-consistency check exists precisely because three of those
versions were documented but never tagged.

### 1.2 Assurance actually in place

Worth stating plainly, because it is unusual at this size:

- **`graph-contract`** installs the pinned `codebase-memory-mcp==0.9.*` and **fails if its live
  tests skipped** — a skip is treated as an unverified contract, which is how a total backend
  outage once stayed green.
- **Release canary** registers a built wheel with Codex and Claude Code in a throwaway `HOME`,
  boots the server those configs name, and asserts on real answer text. Semantic engine only.
- **Doc-vs-code drift tests** derive their expectations from code (`_WITHDRAWN_OPS`) and from
  `ci.yml`, never from hand-typed lists.
- **Version-skew detection** (0.15.5): a running server that is serving code older than what is
  installed now says so.
- **Fault-injection tests** behind the never-raise contract, not convention alone.

### 1.3 Adoption

Zero on every public counter as of 2026-08-23: stars, forks, watchers, open issues, external
contributors. PyPI download figures were not reachable from this container (the stats host is
blocked by the egress proxy), but with no repository engagement the honest reading is that every
bug found so far has been found by the author. The README already says an issue from someone else
is the most useful thing the project could receive; nothing has changed that.

### 1.4 One drift worth fixing

`README.md` says the suite is "~620 tests, ~30s". It collects **807** and takes **~80s** here. Small,
but it is a hand-typed claim about a machine-readable fact — the exact shape the project now has two
tests to prevent elsewhere.

---

## 2. What a fresh environment surfaced

`pytest tests/ -q` in a clean venv: **779 passed, 12 failed, 16 skipped**, 85.10% coverage.

### D1 — Redaction corrupts answers when `HOME` is `/root` (8 failures)

**Severity: high. Reproducible on demand, and it is not confined to tests.**

`redact._flattened_home()` returns the *bare string* `root` when `HOME=/root`, and
`redact_text()` replaces it as an unanchored substring across every field of the envelope:

```
'root-not-allowed-for-role'         → '<home>-not-allowed-for-role'   # a reason code, mangled
'## Callers of get_root_path (3)'   → '## Callers of get_<home>_path'  # a symbol name, mangled
'- src/app/rootstrap.py'            → '- src/app~strap.py'             # a real path, mangled
'run: codeintel index /srv/rootfs/app' → 'run: codeintel index /srv~fs/app'   # a hint, unrunnable
```

The eight RBAC/containment failures are the visible half; the mangled `result` bodies are the half
that matters, because they reach an agent as the answer.

**Why it fires in the case that counts.** `HOME=/root` is what you get in Docker, in a
devcontainer, in most CI images, and on a box running the HTTP transport as a service — and this
project ships a `Dockerfile` and a [`deploy.md`](deploy.md) for exactly that. The redaction module's
own docstring gives its reason for existing as answers travelling "across the HTTP transport to
callers who are not the machine's owner": the deployment it was written for is the deployment it
breaks in.

**Mechanism, not an accident of one username.** Two independent substitutions are unanchored. The
flattened form (`root`) matches inside any identifier; the path form (`/root`) matches inside any
longer path (`/rootfs`, `/rootcerts`). Any home directory whose basename is a common substring
behaves the same way — `root` is merely the one that ships in a base image.

**Suggested fix.** Anchor both substitutions at a path or identifier boundary, refuse to redact a
flattened form below some length, and exempt `reason` outright: reason codes are a closed
vocabulary the caller matches on, not prose that could contain a home path. A property test that a
redacted `reason` still parses as one of the known reasons would have caught this without anyone
thinking of `root`.

### D2 — `no-index` is outside the could-not-ask family (1 failure)

The semantic provider returns `reason="no-index"` for "this repo has no semantic index"
(`providers/semantic.py:158`). The graph provider spells the same class of condition
`project-not-indexed` / `project-not-indexed-standalone`. Two consumers know only the graph
spellings:

- `gateway.py:181` — the `unreachable` set used by `_merge` to decide whether a fan-out that
  produced nothing was *"no engine could be asked"* or *"asked, found nothing"*. With `no-index`
  absent, a fan-out where the semantic engine had no index reports `no-result`, which the tool's
  own documentation tells an agent to read as "nothing found". That is the could-not-ask /
  asked-and-found-nothing collapse the comment ten lines above says the codebase is careful to
  preserve everywhere else.
- `tests/test_cold_process.py:53` — `_COULD_NOT_ASK`, which is why this shows as a failure at all.

**And CI cannot see it.** The cold-process step runs inside the `lsp-contract` job, which is
`continue-on-error: true` (deliberately, because serena is fetched from an upstream HEAD). So the
one test that catches this cannot turn a run red. The step guards against being *skipped* but not
against *failing*.

**Suggested fix.** One shared reason vocabulary — the could-not-ask and asked-and-found-nothing
families defined once in the package and imported by the gateway and the tests, instead of three
hand-typed copies. Then either move the cold-process step into a job that gates, or split
`lsp-contract` so the parts that do not depend on upstream serena can fail loudly.

### Not defects — this container's own limits (3 failures)

Recorded so the next reader does not re-diagnose them:

| Test | Cause |
|---|---|
| `test_onboarding::test_setup_index_real_db` (`assert 0 > 0`) | `fastembed` model download blocked — `ProxyError 403`, so nothing can be embedded and the index is empty |
| `test_cold_process` semantic probe | same missing index; the probe reaches D2's reason string because there is no index to reach past |
| `test_installer::test_doctor_reports_a_live_registration_as_runnable` | the `codeintel` entry point is in a venv that is not on `PATH`, so the registered command genuinely is not runnable |

The first two are worth noting for a different reason: **an air-gapped or proxied machine cannot
complete `codeintel setup`.** The README calls the model download "the one-time exception" to
local-first operation; on a locked-down corporate network that exception is the whole install. A
documented offline path (pre-seed `~/.cache`, or point at a local model directory) closes it.

---

## 3. Value — what is actually differentiated

### 3.1 The contract, not the engines

Two of the three engines wrap binaries this project does not own, and the third is
`bge-small` + `sqlite-vec` — commodity. The differentiator is everything around them:

- **Never-raise, enforced by fault injection.** No competitor in this category treats "the caller
  must never receive an exception or a malformed body" as an invariant with tests behind it.
- **A backend's error text is not an answer.** 0.15.3 found serena's failure prose reaching the
  model as `result`, imperative instructions included. That is a prompt-injection channel most MCP
  wrappers still have open, and closing it is a real security property, not a nicety.
- **Withdrawal over caveat.** `deadcode` is withdrawn (`reason: "op-withdrawn"`) rather than shipped
  with a warning, because its output is an instruction to delete code and its precision was measured
  wrong in both directions. Very few projects remove a feature from their own capability table.
- **Registration that is proven, not assumed** — absolute-path registration plus a real
  `initialize` / `tools/list` handshake against the command that was actually written.
- **Local-first**: one process, no telemetry, no per-query network — which is what makes
  `--engine all` safe to point at a private repo.

### 3.2 Where the value leaks

- **The graph engine is pinned to `codebase-memory-mcp==0.9.*`** because 0.10 changed its response
  format. The most capable third of the product is one upstream release behind by necessity.
- **The LSP wire contract is watched, not gated** (`continue-on-error`), and D2 shows what that
  costs.
- **No comparative number.** [`benchmarks.md`](benchmarks.md) measures index throughput and query
  latency — honestly and reproducibly — but not the number this market buys on: tokens and tool
  calls to answer a real question, against a grep-only baseline. Every competitor publishes one.

---

## 4. The market

> **Reliability note.** Everything in this section is second-hand. Several comparison sites were
> unreachable behind this container's egress proxy, so the figures come from search-result
> summaries and vendor-adjacent posts. Treat them as order-of-magnitude: one tool was reported at
> both 42k and 47.4k stars in the same week. The *shape* of the landscape is reliable; the digits
> are not.

| Tool | Approach | Reported scale |
|---|---|---|
| CodeGraph | tree-sitter graph, 42 MCP tools, MCP *and* LSP, VS Code + JetBrains | ~42–47k stars; publishes "57% fewer tokens, 71% fewer tool calls" |
| `codebase-memory-mcp` | graph **plus semantic vector search and hybrid LSP resolution**, 158 languages, single static binary, auto-detects 10 agents | ~40k stars; #1 GitHub Trending, Jun 2026; has a paper |
| GitNexus | zero-server knowledge graph, BM25 + vector + RRF, 16 MCP tools, Claude Code skills/hooks | ~1.2k → 42k stars, Apr–Jun 2026; enterprise tier |
| Serena | LSP wrapper, 40+ languages — **this project's own LSP backend** | ~24–28k stars |
| Sourcegraph MCP · Augment | commercial context engines exposed over MCP | Augment: $252M raised; claims 71–80% agent-quality lift |
| Claude Code (native) | agentic grep, no index | Anthropic removed vector search in 2025 and still ships grep-only |

Three consequences.

**4.1 The graph backend is the closest competitor.** `codebase-memory-mcp` already ships semantic
vector search, hybrid LSP type resolution, impact analysis, 14 tools, a dependency-free static
binary, and auto-registration with ten agents. A user who installs it alone gets most of this
project's capability table with less setup. The marginal value of codeintel over its own backend is
the unification, the safe-null contract, and the LSP merge — real, defensible, and considerably
narrower than "three engines" implies. The pitch should name it.

**4.2 Naming is working against discovery.** `codeintel` was taken on PyPI (SublimeCodeIntel), so
the distribution is `codecortex` — and there is a separate `CodeCortex` on GitHub with a nearly
identical pitch. Both names resolve to someone else. With zero stars, that is a compounding
problem: nothing distinguishes this project in a search result, and nothing vouches for it once
found.

**4.3 The category thesis is contested, and the honest position is already the design.** Anthropic
still ships grep-only; independent 2026 benchmarks favour indexed retrieval. The defensible framing
is not "indexes beat grep" but *agentic search as the backbone, structural index where it pays,
degrading to grep when an engine is missing* — which is precisely what the safe-null contract
already does. Say it explicitly rather than leaving it implicit in a failure mode.

---

## 5. What follows, ranked

1. **Fix D1.** It is live for every containerised user, it corrupts answer content and reason codes
   alike, and it is roughly ten lines plus a property test.
2. **Fix D2, and make the test that catches it able to fail CI.** One shared reason vocabulary;
   split or re-home the cold-process step.
3. **Publish one comparative number** — tokens and tool calls over N real questions: codeintel vs
   grep-only vs the raw graph backend. Without it the project cannot be compared, and this market
   compares on exactly that.
4. **Lead with the contract, not the engine count.** "The code-intelligence tool your agent cannot
   crash on, and that never dresses a backend failure as an answer" is defensible against a 40k-star
   graph tool. "Three engines" is not.
5. **Document an offline install.** The model download is the one non-local step and it is fatal
   behind a proxy — as measured here.
6. **Get one external user.** Every defect to date is self-found, including both in this document.
   One outside issue is worth more than another release.

---

## 6. Reproducing this

```bash
python3 -m venv /tmp/ci-venv && /tmp/ci-venv/bin/pip install -e .[dev]
/tmp/ci-venv/bin/python -m pytest tests/ -q          # 779 passed, 12 failed, 16 skipped here

# D1, directly — no test runner involved:
HOME=/root /tmp/ci-venv/bin/python -c \
  "from codeintel.redact import redact_text as r; print(r('root-not-allowed-for-role'), r('src/app/rootstrap.py'))"

# D2, by reading:
#   src/codeintel/providers/semantic.py:158   reason='no-index'
#   src/codeintel/gateway.py:181              the 'unreachable' set, which omits it
#   tests/test_cold_process.py:53             _COULD_NOT_ASK, which also omits it
```

Repository and release facts: `https://api.github.com/repos/hamilton-sky/codeintel` and
`https://pypi.org/pypi/codecortex/json`.
