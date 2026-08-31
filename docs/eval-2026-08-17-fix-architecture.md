# codeintel — fix architecture after eval-2026-08-17

> **Status: historical record.** A point-in-time snapshot, kept because the reasoning is worth having and correcting it would falsify the record. Numbers and version claims below were true when written and are NOT current — codeintel is at 0.22.0. Where it disagrees with a reference doc, the reference doc wins.
>
> **What became of the core design.** §2 proposes three modules. One shipped and two did not:
>
> | Proposed | Status |
> |---|---|
> | `outcome.py` — a typed result for every internal seam that returned `X \| None` | **Shipped**, and it is load-bearing. Its docstring carries this evaluation's own worst bug as the rationale: a cold language server timed out, `_call_tool` returned `None`, and four conversions later the caller rendered `## References (none)` — a confident false statement byte-identical to a true one. |
> | `answer.py` | **Not built.** |
> | `envelope.py` — "the only constructor of `Result` in the codebase" | **Not built.** Envelope construction is still distributed. |
>
> So the diagnosis in §1 was adopted and the single-constructor consolidation was not. Read §2 as
> the reasoning behind `outcome.py`, not as a description of the tree.

Read: the eval doc, and `src/codeintel/{gateway,provider,cache,containment,reindexer,indexer,searcher,semantic_db,server,reset,doctor,source_kind}.py`,
`src/codeintel/providers/{graph,lsp,semantic}.py`, `src/codeintel/commands/{query,index,_common,reset}.py`,
`tests/{conftest,test_lsp_real}.py`.

Every claim below cites a line I read. Where I could not verify a mechanism without running the
tool, I say so and give the one-command experiment that decides it (§8).

---

## 1. Root-cause analysis

### 1.1 The report's thesis: half right, and it is the half that matters least

> "the envelope has only two expressible states and lacks a third: ran, produced output, output is
> incomplete."

The **state is genuinely missing** — `Result` (provider.py:27-39) has `result`, `reason`, `hint`,
`reindexing` and nothing that says "this body is short of an answer". I agree with the observation.

I do **not** agree it is the root cause, because at the moment B1 occurs the process *has* the
fact and destroys it one function above the envelope. The path, exactly:

```
lsp.py:498  ref_raw = self._call_tool(session, "find_referencing_symbols", …)
lsp.py:360      except Exception: return None          # timeout, error, cancellation → None
lsp.py:507  ref_lines = self._format_refs(self._loads(self._extract_text(ref_raw)))
lsp.py:408      _loads(None) -> None
lsp.py:441      _format_refs(None): if not isinstance(data, dict): return []      # None → []
lsp.py:496  ref_section = "## References\n(none)"      # the default survives
lsp.py:509  if ref_lines:  …                            # never taken
```

Four lossy conversions, each locally reasonable, ending in a string that asserts a fact nobody
established. Note that the machinery to report this correctly *already exists and is already
armed*: `_extract_text` sets `self._last_backend_error` on an error result (lsp.py:384, :389), and
`build_result` turns exactly that into `reason: "backend-error"` (lsp.py:311-316). `_op_symbol`
simply never consults it once it has a definition.

So: **had the envelope gained a third state last month, B1 would still be here**, because the code
that knows about the failure never talks to the code that sets the flag. The third state is a
symptom of the real defect, not its cause.

### 1.2 What I think the root cause actually is

Two things, both structural.

**(a) There is no shared outcome type, so every seam collapses five meanings into `None`.**

`None` at an internal seam currently means: never asked · timed out · backend errored · parsed to
nothing · genuinely empty. The graph provider *knows* this is wrong and has re-invented a fix three
separate times in one file:

- `_FAIL` / `_UNPARSABLE` sentinels — graph.py:426-429, with a docstring explaining why `None`
  could not carry it;
- `ProjectLookup(resolution, reason)` — graph.py:287-296, "a resolution attempt AND why it failed,
  which the caller must tell apart";
- `_search_symbols` returning `None` vs `[]` deliberately — graph.py:799-803.

Three ad-hoc instances of the same missing abstraction, in one module. `lsp.py` has none of them,
which is why the critical bug is in `lsp.py`. This is the classic signature of a type that should
exist and doesn't.

**(b) The envelope is assembled by the leaves, so every cross-cutting concern is an op author's
memory test.**

`safe_null_result` is called from inside the providers (12 sites in graph.py, 6 in lsp.py, 5 in
semantic.py), and each op renders its own final markdown. Five cross-cutting properties therefore
have no owner, and each is implemented by a different subset of ops:

| concern | who owns it today | consequence |
|---|---|---|
| completeness | nobody | B1 |
| scope caveat | one op renderer, graph.py:1222-1228 | B6, and absent in 2 engines (B7) |
| provenance / titling | the *caller's* basename, graph.py:974 | B6, B8 mislabelled |
| line coordinates | each renderer | B10 — **in two engines, not one** |
| path redaction | nobody at all | B9 |

graph.py:974 deserves its own sentence. `_op_overview` titles the answer
`## Architecture: {_repo_display_name(root)}` — the basename of the directory *the caller asked
about*, not of the project that answered. That change was made for a good reason (keeping a
flattened home path out of a committed `CODE_INTEL.md`) and it has the side effect that **the
presentation layer asserts a provenance the data layer never established**. It is what upgrades
B6/B8 from "wrong numbers" to "confidently mislabelled numbers".

B10 is the cleanest proof that "each renderer owns coordinates" is the defect rather than "the LSP
adapter has an off-by-one". The eval saw one engine; there are two:

- LSP: `body_location.start_line`/`end_line` emitted raw (lsp.py:432-434) and `_ref_line` extracts
  serena's own 0-based number verbatim (lsp.py:412-418).
- Semantic: `semantic.py:169` renders `m['line']`, which is `chunk_start`, which is 0-based by
  construction (`start0 = max(0, start - 1)`, indexer.py:406; `_window_spans(0, …)`,
  indexer.py:384). A hit at the top of a file renders as `path:0` — the same tell as `alpha.py:0`.

Fix the LSP adapter alone and half the bug ships.

### 1.3 On scope specifically — the report's B6 diagnosis is factually wrong about this code

> "Ops must not each re-derive it — that is why three of them disagree."

They don't re-derive it. There is exactly **one** resolver (`_lookup_project`, graph.py:568),
called from exactly **one** place on the query path (graph.py:1157), and **one** policy set
(`_ROOT_SCOPED_OPS`, graph.py:40) enforced **once**, before dispatch (graph.py:1185) — and
`overview`, `deadcode`, `changed`, `changes`, `hotspots` are all in it. Three ops cannot disagree
by re-derivation when there is nothing to re-derive.

What is actually wrong is different and, I think, more interesting:

1. **Resolution trusts a registry claim it never verifies against the data.** `_lookup_project`
   asks `list_projects` whether a project exists for this root (graph.py:592-607). Nothing
   afterwards checks that the rows which come back are *about* that root. Hiding a `.db` file does
   not remove the registration, so resolution can legitimately return `scope="exact"` while the
   backend answers from somewhere else entirely — and then graph.py:974 titles it with the caller's
   own repo name. Under that reading, the scope machinery was not bypassed in B6; it was fed a
   claim it has no way to check. **A dispatcher-level scope gate, implemented exactly as the eval
   recommends, would not have caught B6** — the verdict was `exact`, so the new gate passes too.

2. **The real duplication is the opposite of what the eval says: scope exists in one engine and is
   absent from the other two.** semantic.py:114 checks only that `project_root` is a non-empty
   string, then indexes and searches whatever subtree it was handed (semantic.py:143-152). LSP has
   no notion of scope at all — it binds serena to whatever root arrives (`--project project_root`,
   lsp.py:88-100). B7 is not "semantic forgot to refuse"; scope is a `GraphProvider` concept that
   was never lifted.

### 1.4 A third root cause the report does not name, worth four bugs on its own

**A one-shot CLI process runs the long-lived server's background machinery.**

`Gateway.query` calls `maybe_reindex` on every query (gateway.py:262). In a fresh process
`_last_fired` is empty, so the debounce always passes (reindexer.py:90-93); the pass is submitted to
a **daemon** pool (reindexer.py:26-31) and `_in_flight` is set (reindexer.py:101-102). Ten lines
later the same query asks `reindex_pending` (gateway.py:272) — which is now true **because this
query just made it true**.

```
codeintel query  ──►  maybe_reindex()  ──►  _in_flight.add(root)   ──► daemon thread starts
        │                                          │                        │
        └────────►  reindex_pending()  ────────────┘                        │
                          = True, always                                    │
                                                                  process exits, thread killed
                                                                  mid-write, every time
```

That is **B11** exactly — not a bad derivation from mtime, but a self-report of a job the query
itself started and can never finish. It is also **B17** (the walk restarts every query because the
debounce state died with the last process). It is the most plausible source of **B16**: the indexer
already documents the concurrent-pass hazard (indexer.py:681-686) and daemon threads are killed
mid-write at interpreter exit.

And it is a strong candidate for **B7's missing file**, which the eval reads as a scope bug:
`Searcher.has_index` is `count > 0` (searcher.py:113-116), while actual completion is recorded
separately in `project_index_meta` (indexer.py:777, :781; semantic_db.py:237) and **never
consulted**. A pass killed halfway leaves an index that reads as complete forever, and
`search "embedding provider factory"` then answers from a partial corpus with total confidence —
the same "ran, incomplete, doesn't know it" shape as B1, in a completely different subsystem.

### 1.5 One-sentence root cause

> The provider boundary is at the wrong altitude: providers are asked to return finished,
> presentation-ready envelopes, so each must independently implement outcome typing, scope,
> provenance, coordinates and redaction — and each implements a different subset. The missing
> third envelope state is the most visible instance of that, not its cause.

---

## 2. The core design

**Name:** *Outcome / Answer / one assembly layer.* One coherent change: providers stop producing
envelopes and start producing typed, sectioned answers; one layer the gateway owns turns those into
the published envelope and owns every cross-cutting concern.

### 2.1 Shape

```
                        ┌──────────────────────────────────────┐
  commands/ ──┐         │  gateway.Gateway.query               │
  server.py ──┼────────►│   1. build QueryContext (scope once) │
  http_server ┘         │   2. enforce scope table (all 3)     │
                        │   3. dispatch → Answer               │
                        │   4. envelope.to_envelope(Answer)    │
                        └───────┬──────────────────────────────┘
                                │
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
   GraphProvider           LspProvider            SemanticProvider
   .answer(ctx)            .answer(ctx)             .answer(ctx)
        └───────────────────────┴────────────────────────┘
                                │  returns
                                ▼
                    Answer(sections, confidence, provenance)
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
                answer.py               outcome.py
                (leaf)                    (leaf)
```

New modules, all leaves or near-leaves:

| module | depends on | owns |
|---|---|---|
| `outcome.py` | stdlib | `Ok` / `Missing` |
| `answer.py` | `outcome` | `Answer`, `Section`, `Gap`, `Confidence`, `Provenance`, `loc()` |
| `scope.py` | stdlib + a capability Protocol | `ScopeVerdict`, `QueryContext` |
| `redact.py` | stdlib | `redact(ctx, text)` |
| `envelope.py` | `answer`, `scope`, `redact`, `provider.Result` | the ONLY place a `Result` is built |

Dependency rule, one direction, enforceable in one test:
`providers/*` may import `outcome`, `answer`, `scope`. They may **not** import `envelope`, and
`safe_null_result` is removed from them entirely.

### 2.2 The types

```python
# outcome.py — every internal seam that today returns `X | None`
@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T

@dataclass(frozen=True)
class Missing:
    kind: Literal["timeout", "backend-error", "unparsable",
                  "not-asked", "unsupported", "capped", "warming"]
    detail: str = ""
    retry_after_s: float | None = None

Outcome = Ok[T] | Missing
```

Applies to `_call_tool`, `_extract_text`, `_loads`, `_run`, `_query_rows`, `_search_symbols`.
graph.py's `_FAIL`/`_UNPARSABLE` (graph.py:426-429) and `ProjectLookup` (graph.py:287) collapse into
this and stop being one file's private invention.

```python
# answer.py
class Confidence(StrEnum):
    COMPLETE = "complete"   # ran; the engine vouches for the whole body
    PARTIAL  = "partial"    # ran; produced a body; KNOWN to be missing a named part
    EMPTY    = "empty"      # ran; complete; genuinely nothing — a true negative
    REFUSED  = "refused"    # did not run: wrong scope / unsupported / not indexed
    FAILED   = "failed"     # tried; backend did not answer

@dataclass(frozen=True)
class Gap:
    section: str                  # "references" | "callees" | "corpus" | "scope" | "*"
    kind: str                     # "backend-timeout" | "index-warming" | "ancestor-scope"
                                  # | "partial-index" | "unverified" | "capped" | "corpus-mixed"
    detail: str                   # one sentence, caller-facing
    retry_after_s: float | None = None

@dataclass(frozen=True)
class Section:
    name: str
    body: str
    gaps: tuple[Gap, ...] = ()

@dataclass(frozen=True)
class Provenance:
    engine: str
    project: str | None           # backend project id — compared, NEVER rendered (it is a path slug)
    answered_root: str | None     # the root the DATA is about
    asked_root: str
    scope: Literal["standalone", "contained", "unknown"]

@dataclass(frozen=True)
class Answer:
    sections: tuple[Section, ...]
    confidence: Confidence
    provenance: Provenance
    reason: str | None = None
    hint: str | None = None
```

### 2.3 The one function that makes B1 unrepresentable

```python
def section(name: str, outcome: Outcome[T], render: Callable[[T], str]) -> Section:
    if isinstance(outcome, Ok):
        return Section(name, render(outcome.value))
    return Section(
        name,
        f"## {name.title()} — not retrieved",
        gaps=(Gap(name, outcome.kind, outcome.detail, outcome.retry_after_s),),
    )

def answer_of(*sections: Section, prov: Provenance) -> Answer:
    gaps = tuple(g for s in sections for g in s.gaps)
    conf = Confidence.PARTIAL if gaps else (
        Confidence.EMPTY if all(_is_empty(s) for s in sections) else Confidence.COMPLETE)
    return Answer(sections, conf, prov)
```

This is the whole trick, and it is why it is a design and not a patch: **an op only ever holds an
`Outcome`, and the only function that converts an `Outcome` into renderable body text also converts
a `Missing` into a `Gap`.** There is no code path that renders a section from a value the op does
not have. `_op_symbol` becomes:

```python
defn = self._find_symbol(ctx)                        # Outcome[list[dict]]
if isinstance(defn, Missing):
    return Answer.failed(defn, prov)                 # no definition ⇒ no answer at all
refs = self._find_refs(first_match, ctx)             # Outcome[dict] — Missing on None/timeout/isError
return answer_of(section("definition", defn, _render_def),
                 section("references", refs, _render_refs), prov=prov)
```

Cold language server ⇒ `refs = Missing("timeout", "the language server had not finished loading
this workspace")` ⇒ `confidence = PARTIAL`, body says *"## References — not retrieved (the language
server had not finished loading this workspace; re-ask in a few seconds)"*.

Crucially, **"zero references, definitively" is still expressible**: `Ok({})` renders
`## References (0)` with `confidence = COMPLETE`. Those two answers are now different bytes. That is
the third state — but note it attaches to the **section**, not to the envelope. That distinction is
the design: a qualification that does not name which part of the body it is about cannot be acted
on. The envelope-level `confidence` is *derived*, never set by hand.

### 2.4 The assembly layer

```python
# envelope.py — the only constructor of Result in the codebase
def to_envelope(a: Answer, *, op, target, engine, cached, ctx) -> Result
```

It owns, in one place:

1. `ok / op / target / engine / cached`, and `result` = the joined section bodies **plus rendered
   gap notes plus the scope note**. (Gap notes go into `result` on purpose — see §4.)
2. `reason` / `hint` for null results, with today's exact reason strings preserved.
3. New additive keys: `confidence`, `gaps`, `scope`.
4. **Redaction**: `redact(ctx, …)` applied to `result`, `hint`, `reason` and every gap detail — one
   function, one call site. B9 becomes structurally impossible instead of a sweep that has to be
   repeated (graph.py:347-356 records what happened last time a sweep was done by hand: "`_display`
   was fixed first, `_render_scan` was missed and shipped … `chain` and `pattern` turned out to be a
   third and fourth").
5. **Provenance check** — this is what actually fixes B6: if `provenance.answered_root` is set and
   is not `ctx.root`, force a scope Gap; for a root-scoped op, downgrade to `REFUSED`. This is a
   check on *data*, not on the registry, so it survives the hidden-`.db` case that the eval's own
   prescription would sail through.
6. Titling: headings are built from `provenance`, never from the caller's basename. If the engine
   cannot name what answered, the heading says so.

### 2.5 Scope, resolved once, for all three engines

```python
@dataclass(frozen=True)
class QueryContext:
    op: str; target: str; engine: str
    root: str            # realpath'd
    display: str         # basename — for headings only, never for provenance
    budget_ms: int       # the CLI must start supplying one (see §7 ship 1)
    scope: ScopeVerdict
```

`ScopeVerdict` is computed from three cheap facts, none of which requires trusting a registry:

- `own_repo` — `.git` exists at root (`_has_own_git_dir`, graph.py:319, moves here verbatim);
- `nearest_indexed_root(engine)` — graph: the existing `list_projects` match (moves here);
  semantic: the longest `project_root` prefix in `chunk_hashes`; lsp: always the root (serena binds
  per-root, so it is standalone by construction);
- verdict = `standalone` | `contained` | `absent`, **per engine**.

The dispatcher enforces one table for every engine:

```python
_SCOPE_REQUIREMENT = {
    "overview": ROOT, "deadcode": ROOT, "hotspots": ROOT, "changed": ROOT, "changes": ROOT,
    "search": ROOT_SOFT,        # contained ⇒ answer + scope Gap; absent ⇒ index then answer
    # default: SYMBOL           # contained ⇒ answer + scope Gap (correct for a monorepo subdir)
}
```

`ROOT + contained` ⇒ `REFUSED` **before the provider is called**, so it covers semantic (B7) and lsp
without either of them knowing what scope is. `SYMBOL + contained` ⇒ dispatch, and the *envelope*
attaches the caveat — not the op (graph.py:1222-1228 moves out).

### 2.6 What this eliminates structurally (not by patch)

| bug | how it dies |
|---|---|
| B1 | a dropped sub-call cannot become an empty section; + budget fix (§7) |
| B6 | provenance check on returned data; heading from provenance not basename |
| B7 | one scope table, enforced for all engines in the dispatcher |
| B9 | one `redact()` call site in `envelope.py` |
| B10 | `loc(file, line0)` is the only thing that can format `file:line` — both engines |
| B5 (disclosure half) | corpus mix becomes a `Gap`, not a hand-appended note |
| B3/B4 (disclosure half) | `_VERIFY_NOTES` (graph.py:228-237) becomes Gaps with confidence |
| B14 (reporting half) | `index` reports per-engine `ScopeVerdict` in the same vocabulary |

### 2.7 The second, smaller structural change I would ship with it

**`RunMode`.** `Gateway(..., mode=RunMode.ONESHOT | SERVER)`. In `ONESHOT`, `maybe_reindex` is not
called at all — a process that cannot finish a reindex must not start one — and therefore cannot
report `reindexing: true` for a job it created. ~20 lines; it is B11 + B17 + most likely B16, and it
removes the torn-write pressure on both databases. This does not belong in `answer.py`, but it is
the same insight applied to a different axis: **a component should not be asked to carry a
responsibility it structurally cannot honour.**

---

## 3. What the core design does not fix

| bug | still independent? | one-line approach |
|---|---|---|
| **B2** callees | **split** | See below — half is ours and fixable, half is upstream. |
| **B3** deadcode FPs | yes | Pull the op. No structural fix; the design only lets it say it is unsure, which is not enough for an op that says "delete this". |
| **B4** deadcode FNs | yes | Same. Needs a labelled corpus before it can return (§5). |
| **B5** corpus mix | yes | Partition candidates by file extension into code/doc pools at *query* time (searcher.py:299-315), fill top-k from code first, report the mix as a Gap. No schema change, no reindex. |
| **B8** phantom counts | yes, and probably not ours | `_op_overview` (graph.py:965-1010) is a pure passthrough of `get_architecture`. The eval's fix ("scope the count query by project id") targets code that does not exist here. In-repo remedy: cross-check the total against a `search_graph` count and emit a Gap when they disagree; show provenance. |
| **B11** reindexing flag | fixed by RunMode, not by the envelope | Delete the field; emit a `stale-index` Gap when a *server*-mode reindex is genuinely in flight. |
| **B12** hotspots skew | yes | Two suspects before blaming the backend's visitor: we query `label: "Function"` only (graph.py:1115), which excludes `Method` nodes — and Python methods and TS class methods are `Method`; and `min_degree: 1` drops anything the extractor gave degree 0. Query `Function|Method`, then report per-language coverage as a Gap when one language exceeds ~90% of the ranking. |
| **B13** reset | yes | codeintel has zero references to `~/.cache/codebase-memory-mcp` (grepped: none). Either shell the backend's own reset, or rename the flag and state the limitation. Not a design problem — an ownership one. |
| **B14** index lifecycle | mostly | index.py:62 calls `_graph_reindex` and swallows the result (`except Exception: pass`, index.py:63-64). Poll `list_projects` until the project resolves, bounded; report per-engine; never print success for an engine that did not land. |
| **B15** ignore policy | **not achievable as stated** | See §6.6. |
| **B16** corrupt DBs | cause fixed by RunMode; reporting is separate | `doctor` globs the two cache dirs for `*.corrupt` and reports count + age. |
| **B17** re-walk | fixed by RunMode | — |
| **B18** granularity | not a bug | Backend node granularity; document it in the `callers` output as a standing note. |
| serena writing `.serena/` into target repos | yes | Pass serena a project config outside the repo, or state that the tool writes. It contradicts the read-only promise. |

**B2, split properly** — this is the correction that matters most in this section. The eval assigns
all of B2 to the extractor. But `_op_callees` is:

```
graph.py:872   MATCH (a)-[c:CALLS|USAGE]->(b) WHERE a.name="{target}" …
```

That keys on the **unqualified name**, matching *every* node in the graph called `write_board_mirror`
— and, transitively, pulling in whatever those other nodes call. Some of the "5 of 7 wrong" rows are
not extractor noise; they are our own query matching a different node that happens to share a name.
That is ours, and it is a week's work: resolve the target to a unique `qualified_name` first
(`search_graph` already returns it), key the traversal on that, and when the target is ambiguous
return the candidate list instead of merging them — `_op_chain` already does exactly this
(graph.py:905-911). What remains upstream and can only be *filtered*: cross-language edges and
non-code files admitted as symbol nodes. Filter what we can see (drop callee rows whose file
extension is not a code extension, and whose language family differs from the caller's — that alone
kills the `.ts` preload, the `.json` in `.archive/`, and likely the conftest row), and mark the
section PARTIAL with a `name-resolved-not-type-resolved` gap.

---

## 4. Migration & compatibility

**There is no published JSON schema, and that is load-bearing.** The MCP tool returns plain `dict`,
deliberately:

```
server.py:342-345   # MUST stay `dict`, not `Result`. FastMCP derives this tool's output schema from
                    # the return annotation, and it validates a TypedDict's NotRequired keys …
```

guarded by `test_mcp_server.py::test_no_tool_advertises_the_optional_envelope_fields_as_required`.
So the contract is: six keys, two optional keys, and prose in `_MCP_INSTRUCTIONS`
(server.py:309-326) plus the tool description (server.py:359-368).

Therefore: **additive only. No version field. No `code.query2`.** A version field on an unschema'd
dict buys nothing and costs a permanent branch in every consumer.

**What an old caller sees:**

| key | before | after |
|---|---|---|
| `ok`, `op`, `target`, `engine`, `cached` | unchanged | unchanged |
| `result` | body | body, **plus gap notes and the scope note inline** |
| `reason`, `hint` | present on null results | same strings, same conditions |
| `reindexing` | `true`, always (B11) | **removed** |
| `confidence` | — | new: `complete`/`partial`/`empty`/`refused`/`failed` |
| `gaps` | — | new: `[{section, kind, detail, retry_after_s}]`, omitted when empty |
| `scope` | — | new: `standalone`/`contained`/`unknown` |

Three deliberate decisions:

1. **Gap notes must be rendered into `result`, not only into `gaps`.** Agents read `result`. A
   qualification that lives only in a new field is invisible to every consumer that exists today,
   which would reproduce the exact failure mode the change is meant to end. The new fields are for
   programmatic consumers; the body text is for the model.
2. **The one behavioural change old callers will see is intentional**: where a cold LSP used to
   emit `## References (none)`, it now emits `## References — not retrieved (…)`. `result` is still
   non-null, `ok` is still true, shape is unchanged. It is a *content* change, and it is the entire
   point of the release.
3. **`reindexing` gets deleted, not derived.** The eval says derive-or-delete; I say delete.
   It has been `true` for every answer this tool has ever produced (§1.4), which has trained every
   consumer to ignore it — a field with a poisoned reputation is worse than no field. Removing an
   optional key from an unschema'd dict is safe: `r.get("reindexing")` becomes `None` → falsy, which
   for 100% of today's answers is *more* accurate than what the caller gets now.

**Reason strings are part of the contract and do not change.** `project-not-indexed-standalone`,
`backend-error`, `warming`, `backend-unreachable`, `not-in-graph`, `below-floor`, `engine-unavailable`
keep their exact spellings; tests assert them (test_lsp_real.py:232, :253) and the eval singles them
out as one of the things that works. New reasons only for genuinely new states.

**One doc change is mandatory and is not cosmetic.** `_MCP_INSTRUCTIONS` currently ends:

> "a null `result` with a `reason` means 'nothing found / not indexed yet', NOT an error"

That sentence is the instruction that makes a partial answer invisible — it tells the model that
non-null means answered. It must gain: *"a non-null `result` may still be incomplete: check
`confidence`; when it is `partial`, the body names which section is missing and why."*

**Rollout:** one minor release. Ship `confidence`/`gaps`/`scope` in the same release as the body-text
change, because they describe the same fact and splitting them creates a release where the body is
qualified and nothing machine-readable says so.

---

## 5. Test strategy

Three tiers. The costs are real and I state them.

### Tier 1 — type-level tests (cheap; catch the *class*, not the instance)

- **Import guard:** no module under `providers/` imports `safe_null_result` or `envelope`. One
  assert; makes the architecture self-enforcing.
- **The starvation test** (this is the one that kills B1 for ops that don't exist yet): reflect over
  every op method on every provider, drive each with a stub whose *every* sub-call returns `Missing`,
  and assert the resulting `Answer` is never `COMPLETE` and its body never contains `(none)`,
  `(none found)`, `(no matches)`. Then a second pass where sub-calls return `Ok(empty)` and assert
  `EMPTY`/`COMPLETE`. This codebase already knows this pattern works and why — graph.py:347-356:
  *"The test now enumerates the module rather than a list of functions someone remembered to write
  down."* Same reasoning, applied to the failure axis instead of the rendering axis.
- **Coordinate test:** `loc()` is the only function that may produce `file:line`; property test that
  no emitted line number is ever `0` and that a known 0-based input renders as `n+1`. Would have
  caught both halves of B10.

Cost: hours. Runs in the default suite.

### Tier 2 — cold-process integration (this is what B1 needs and what does not exist)

The constraint is exact: **B1 only reproduces on call #1 in a fresh process.** No in-process fixture
can express it. Worse, the current suite *encodes the bug as correct*:

```
tests/test_lsp_real.py:162-177
    def test_symbol_definition_only_when_no_references(...)
        …  # find_referencing_symbols returns "{}"
        assert "## References" in r["result"]  # section present, empty
```

That fixture must split into two: `Ok({})` keeps the assertion; a `Missing` case asserts
`confidence == partial`. And note the hole it left: there is a test for *every* tool call failing
(test_lsp_real.py:241-253) and none for *`find_symbol` succeeds, `find_referencing_symbols` fails* —
which is B1's exact shape.

```
tests/coldstart/
  fixtures/tstest-ts/     2 files — the eval's §8 repro, committed verbatim
  fixtures/onefile-py/    1 file, 1 def — the B8 exactness fixture
  fixtures/hotspot-mix/   1 gnarly .py + 1 gnarly non-JSX .ts — the B12 fixture
  runner.py               subprocess.run([sys.executable, "-m", "codeintel",
                            "query", "--op", …, "--json"], cwd=fixture, env=HERMETIC)
```

Rules that make it meaningful rather than decorative:

1. **One process per assertion.** The runner never reuses a process. That is the only way call #1
   is call #1. This is the single design constraint of the whole tier, and it is why it cannot be a
   normal pytest fixture.
2. **Hermetic caches.** `CODEINTEL_HOME=<tmp>` already redirects the semantic cache
   (semantic_db.py:29). The **graph cache has no such override** — a real gap, and I would close it
   as part of B13: without it, cold graph tests either pollute the developer's real cache or cannot
   run hermetically at all.
3. **Assert an invariant, not an expected answer.** For `symbol`: *either* (`confidence == complete`
   **and** references match the committed grep ground truth exactly) *or* (`confidence == partial`
   **and** a gap names the `references` section). Never a third outcome. A warm-only suite cannot
   state this, because it never sees the second disjunct.
4. **Repeat cold runs 5×, in 5 processes.** B1 is timing-dependent; one cold run can pass by luck.

**Cost, honestly:** each cold LSP run is 10–20 s wall (uvx fetch + serena boot + LS load), so 5×
is 1–2 minutes, needs `uvx` and network on first run, and lives permanently near the flakiness line.
So: `@pytest.mark.coldstart`, excluded from `pytest -q`, run as a **required** CI job and pre-release
gate. The existing opt-in shape (`CODEINTEL_LIVE_LSP=1`, test_lsp_real.py:273-276) is right in form
and wrong in default — an opt-in test nobody sets is not coverage, which conftest.py:1-20 already
says out loud about a different gap. Flip it: opt-out locally, mandatory in CI.

### Tier 3 — fixture exactness (B8, B12, deadcode)

- **B8:** index `onefile-py`, run `overview`, assert **exact** counts: `Function: 1`, no `Class`, no
  `Method`, and `nodes <= 3`. Today's assertion vocabulary ("contains `## Architecture`") cannot
  fail on B8. A 1-symbol repo is the cheapest decisive oracle there is.
- **B12:** `hotspot-mix` — assert both the Python and the non-JSX TS function appear in the ranking.
  20 lines; would have caught a 100%-`.tsx` ranking on the first run.
- **Deadcode corpus** — the expensive one, and the one I would still buy. ~30 labelled symbols
  across TS + Python: rollup/vite plugin hook, `Record<string, fn>` dispatch entry, RTK
  `createSlice` reducer, `[project.scripts]` entry point, `__all__` export, a decorated handler, and
  ~8 genuinely dead functions including the three `comms_formatters.py` cases the eval found. CI
  computes **precision and recall as numbers** and fails below a threshold, rather than asserting
  individual rows. This is the artifact that decides whether `deadcode` may return at all.
  **Cost: ~1 developer-day to build, plus ongoing maintenance.** There is no cheaper way to stop a
  bug that is wrong in both directions.

### What I would not build

An end-to-end harness against the two real repos. The answers change under you, it is ~8 minutes of
indexing per run, and it cannot be asserted on. The eval already demonstrated the correct use of
those repos: a periodic manual adversarial pass, not CI. Budget one per release instead.

---

## 6. Where I disagree with the report

### 6.1 "Three ops each re-derive scope; that is why they disagree" (B6) — wrong about this code

One resolver (graph.py:568), one call site (graph.py:1157), one policy set (graph.py:40) enforced
once (graph.py:1185), containing all four repo-wide ops. The prescription ("resolve once before
dispatch") is directionally right and I have adopted it — but **for a different reason**, and as
stated it would fix B7 and **not** B6: in the hidden-`.db` case resolution returns `exact`, so a
new dispatcher-level gate passes exactly as the current one did. The missing piece is a check on
the *returned data's* provenance, not on the registry's claim. Shipping the eval's B6 fix as
written would produce a green test suite and an unfixed bug.

### 6.2 B1 recommendation #1 ("gate on serena's workspace-load state") — not implementable, and harmful

serena's surface here is three tools (lsp.py:164-172); none reports workspace-load state, and
`_State.READY` (lsp.py:149) already means "the MCP session initialised", which is a different fact.
Worse, a *gate* converts a partial answer into no answer, and the definition half was correct and
useful. Recommendation #2 (separate definition and references, each with its own completeness) is
the right one and makes #1 unnecessary.

The eval also misses the mundane contributing cause: **the CLI passes no budget.**
`commands/query.py:60-67` calls `gw.query(...)` with no `budget`, so LSP falls to
`_DEFAULT_TIMEOUT_S = 5.0` (lsp.py:288, :23) and graph to 5000 ms (graph.py:1155) — against a first
`symbol` query the eval itself measured at **11.65 s**. Raising the cold-path budget is plausibly a
bigger single-line win on B1 than anything else in that section, and it costs one argument.

### 6.3 B10 is misdiagnosed as an LSP bug

`semantic.py:169` emits 0-based `chunk_start` as `path:line`. Same defect, second engine. The
prescribed fix ("convert at the adapter boundary; add a fixture") would fix the half that was
observed and leave the half that wasn't. This is precisely why I want a coordinate type rather than
an adapter patch.

### 6.4 B11's fix ("derive from index generation vs filesystem mtime") is over-engineered and aimed at the wrong cause

The flag is not badly derived. It is true because the query started the reindex ten lines earlier
(gateway.py:262 → reindexer.py:101-103 → gateway.py:272). No mtime comparison is needed; stop
running a background reindexer in a process that cannot finish it. Then the existing derivation is
already correct — and I would still delete the field (§4).

### 6.5 B2's recommendations are aimed at a codebase this project does not own — and miss the part that is ours

Points 1–5 of B2 (local-scope suppression, language constraint, receiver typing, data-file nodes,
`.archive/` exclusion) are all inside the `codebase-memory-mcp` **binary**. codeintel issues a
Cypher string and renders rows (graph.py:870-879). Writing them as recommendations to this project
implies work that cannot be done here. Meanwhile the eval misses the defect that *is* ours: the
query keys on the **unqualified** `a.name`, so it merges every same-named node in the repo (§3).
That is fixable this week and probably accounts for several of the wrong rows.

### 6.6 B15 "one shared ignore policy consumed by both the graph extractor and the semantic indexer" — not achievable

`source_kind.py` already **is** that shared policy; its docstring (source_kind.py:1-26) explains why
it was created, and it is consumed by the indexer (indexer.py:353) and by the graph provider
(graph.py:153-159, :219). What it cannot do is influence what the external backend *extracts* — we
can only post-filter what it returns. "They clearly have separate notions of what to skip" is not
what the code shows; what the code shows is one notion applied at two different *stages*, only one
of which we control. The honest formulation is: one policy, applied as a post-filter on the graph
side, with the residual gap documented.

### 6.7 The priority table is wrong at #3, and about `hotspots`

`deadcode` is ranked #3 at effort **L**, "fix behind a flag". **Pull it and do not schedule the
fix.** Wrong in both directions with no labelled corpus means there is no evidence it can be made
right, and it is the one op whose output is an instruction to delete code. Spending L-effort there
ahead of B5 — which makes `search`, the most-used op, useless on any doc-heavy repo — is the wrong
trade. Same judgement for `hotspots`: 100% `.tsx` in both repos is not "skewed", it is not working,
and it should sit behind the same gate.

### 6.8 A carve-out on "the never-raise contract is right"

Agreed at the **tool boundary**. It is wrong *inside* the providers, where
`except Exception: return None` (lsp.py:360-361, graph.py:962-963, graph.py:1011-1012,
lsp.py:512-513) is the actual mechanism that destroys the information the envelope is then blamed
for being unable to express. Internally, failures should be *values* (`Missing`), not swallowed
exceptions. The contract should be "never raise **at the boundary**", enforced in one place, not a
`try/except` in every method.

---

## 7. Sequencing

### Ship 1 — days, no contract change, highest safety-per-line

1. **Pull `deadcode` and `hotspots` from the default op set** and from the tool description
   (server.py:359-368) and `_MCP_INSTRUCTIONS` (server.py:318-319, which currently *recommends*
   both). Contains B3, B4, B12 immediately.
2. **RunMode: no background reindex in one-shot processes; delete `reindexing`.** B11, B17, most
   likely B16.
3. **CLI passes a budget; LSP cold budget → 30 s.** Half of B1, one argument.
4. **`loc()` conversion in the LSP *and* semantic renderers.** B10, both halves.
5. **Stop titling `overview` with the caller's basename** (graph.py:974) and add a stopgap
   `redact()` at the two hint/result sites that leak home paths. B9 partial, and it removes B6's
   false attribution even before the real fix.

### Ship 2 — 1–2 weeks, the core design

6. `outcome.py` / `answer.py` / `envelope.py` / `scope.py` / `redact.py`; providers rewritten to
   return `Answer`; `safe_null_result` removed from providers; scope table enforced in the
   dispatcher for all three engines; provenance check on returned data.
   **B1 fully, B6, B7, B9 fully.**
7. Tier 1 + Tier 2 tests land **with** it, not after. The starvation test and the cold-start job are
   what stop this class from returning.

### Ship 3 — measurement-gated, in this order

8. Semantic corpus split at query time + mix Gap (B5) — highest user-visible value remaining.
9. `callees` keyed on qualified name + language/extension filter (our half of B2).
10. `index` lifecycle: poll until graph-queryable, per-engine report, no success line for an engine
    that did not land (B14).
11. `reset --all` covers the graph cache, or is renamed with the limitation stated; `doctor` reports
    `.corrupt` files (B13, B16). Graph cache-dir override lands here too — the cold-start test tier
    needs it.
12. Labelled deadcode corpus. `deadcode` returns **only if** precision clears the threshold, renamed
    `unreferenced-candidates`, with per-candidate confidence.
13. `hotspots`: `Function|Method` query + per-language coverage Gap; returns when the mix fixture
    passes.

### Pull from the product rather than fix

- **`deadcode`** — indefinitely. It comes back when a corpus says it can, or not at all. I would
  plan as though it does not.
- **`hotspots`** — until the mix fixture passes. Cheaper to fix than deadcode, same gate.
- **`impact`'s callees half** — this is the interesting product call. The eval's own evidence is
  that `callers` is exact on 5 symbols across 2 languages with zero missing call sites, while
  callees was 5/7 wrong on the one symbol tested. Ship `impact` with the callers half plus an
  explicit gap for callees, and render callees only when the target resolves to a unique qualified
  name. Half an op that is right beats a whole op that is half wrong, and the `Answer`/`Gap` shape
  makes "half an op" a first-class, honestly-labelled thing instead of a silent omission.

---

## 8. What I could not verify without running the tool

Two mechanisms I reasoned about but could not close from source alone. Both are cheap experiments
and both should be run before Ship 2 starts, because they change how much of B6/B8 the provenance
check actually catches.

1. **B6 — with the `.db` hidden, does `list_projects` still list the project?**
   `codebase-memory-mcp cli list_projects '{}'` with the file renamed. If the entry is **still
   listed**, the mechanism is registry-trust exactly as I describe in §1.3, and the provenance check
   is the right fix. If the entry is **gone**, then resolution took the ancestor branch
   (graph.py:556-565), scope was `ancestor`, and `overview`/`deadcode` should have refused at
   graph.py:1185 — which would mean there is an enforcement bug I could not find by reading, and it
   must be found before anything else in this document is built.

2. **B8 — where do 17 nodes / `Class: 5` come from on a 1-file repo?** `_op_overview` is a pure
   passthrough (graph.py:965-1010), so the numbers are the backend's. But note that `callers q` on
   the same project answered `not-in-graph` (graph.py:1212) — i.e. resolution *succeeded* and the
   symbol was absent, which is consistent with the project having resolved to some other tree. The
   one command that settles it: `codebase-memory-mcp cli list_projects '{}'` from that directory and
   compare `root_path` against `realpath(.)` — on macOS `/tmp` → `/private/tmp` alone can defeat the
   exact match at graph.py:535 while the prefix branch at graph.py:538 also fails, which would send
   it down the not-indexed path and then into the `overview` LSP fallback (gateway.py:355-366).
   The rendered output format rules the LSP fallback out (it is graph's `## Architecture:` shape,
   lsp.py:530 renders `## Overview:`), so a third possibility remains: the reindexer's orphaned
   `index_repository` (reindexer.py:188, fired by the *previous* query per §1.4) registered that
   directory between the two calls. If that is what happened, it is another consequence of the
   one-shot-process defect, and Ship 1 item 2 removes it.
