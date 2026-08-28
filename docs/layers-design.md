# Architectural layer views for `codeintel c4` — design

Status: **design only.** Nothing here is implemented. This document decides *how*, so an
implementation session does not have to re-litigate it.

Companion reading: [`docs/c4.md`](c4.md) (what `c4` does today), `src/codeintel/c4.py` (how).

---

## 0. The one-paragraph version

`c4` today draws directories. This adds a second, orthogonal reading of the same elements:
**layers**, computed from import direction. Layers are inferred by default so a fresh repo gets
something for free, overridden by a committed config when an architect has an opinion, and — once
an opinion is declared — turned into a **check** that CI can fail on. The check is the point. The
diagram is the by-product.

Two facts shape everything below, both re-measured against this repo's live index while writing
this:

1. **Layers must be computed on `IMPORTS` alone.** The union `IMPORTS ∪ CALLS|USAGE` that `c4`
   draws today is not layerable, and the reason is worse than "it has cycles" — see §1.
2. **Inferred layers can never produce a violation.** This falls out of the ranking algorithm as a
   theorem, not a limitation of the implementation (§2.3). It means the feature has two halves that
   are *not* peers: inference gives you a picture, declaration gives you a gate.

---

## 1. Why `IMPORTS` only — and why the union is worse than "cyclic"

The prior audit established that the union has 3 SCCs, the largest swallowing 21 of 58 `src`
modules, plus 7 mutual pairs. That is true, and by itself it is enough to disqualify the union as a
layering input: you cannot rank a graph in which a third of the nodes are mutually reachable.

But the interesting question is *why* those cycles exist, because the answer decides how much the
`CALLS|USAGE` overlay is allowed to claim. I sampled the mutual pairs against the live index:

| claimed mutual pair | the reverse edge, resolved | verdict |
|---|---|---|
| `cache ↔ provider` | `provider.attach_confidence` → a symbol named `get` | `provider.py:107` is `result.get("result")` — a **dict lookup** |
| `auth ↔ policy` | `policy.__init__` → `auth.enabled` | `policy.py:38` is a **keyword parameter** `enabled: bool = False`; `auth.py:49` is `TokenAuth.enabled` |
| `searcher ↔ semantic_db` | `semantic_db._table_dim` → `search` | bare name |
| `graph_resolution ↔ providers.graph` | `graph_resolution.__init__` → `_project_cache`, `_negative_until`, `_project_cache_lock` | same-named module attributes in both files |

Then I widened it. Every `CALLS|USAGE` edge pointing into `cache.py`:

```
MATCH (a)-[c:CALLS|USAGE]->(b) WHERE b.file_path STARTS WITH 'src/codeintel/cache.py'
RETURN a.file_path, b.name, count(*) LIMIT 60
```

**54 of the 60 rows returned target a symbol named `get`** (the query hit its own 60-row limit, so
there are likely more). The sources include `src/codeintel/paths.py`, `src/codeintel/term.py`,
`src/codeintel/source_kind.py`, `src/codeintel/logconfig.py`, twelve `tests/*` files, and
`scripts/release_canary.py`. Every `dict.get(...)` in the repository is being attributed to
`ContentHashCache.get`, because it is the only user-defined `get` in the project. The six rows that
are *not* `get` are the real ones: `gateway → ContentHashCache`, `gateway → put`, `gateway →
clear`, `cache → _compute_hash`.

So `cache.py` is the highest-fan-in module in the union graph and roughly nine tenths of that
fan-in is fabricated.

Three consequences, all load-bearing:

- **Layer ranks are computed on `IMPORTS` only.** Not as a pragmatic simplification — the union is
  measurably wrong about direction, and a rank derived from it would encode `dict.get` as an
  architectural dependency.
- **`CALLS|USAGE` up-edges are advisory and never gate CI** (§5). They are drawn, tagged, and
  counted. They are not evidence.
- Sidebar, not this design's call: today's `fan_in >= 5` hotspot tag in `c4.py` is computed from
  the union and inherits this contamination. Another agent is currently touching that threshold —
  this measurement is offered to them as input, not as a demand. The layer work computes its **own
  `IMPORTS`-only fan-in** rather than reusing `element["fan_in"]`, precisely so the two cannot
  drift into each other.

### 1.1 Re-verifying the acyclicity claim

`IMPORTS` restricted to `src/` on this repo: **63 distinct file pairs**, and hand-checking the
adjacency confirms no back edges — every path terminates in one of `provider`, `paths`, `outcome`,
`loc`, `policy`, `installer`, `containment`, `progress`, `source_kind`, `redact`, `reindexer`,
`commands/_common`, `commands/index`, `graph_render`, `logconfig`, `metrics`. Acyclic, as the audit
found. The same slice under `CALLS|USAGE` is **256 file pairs**, and at least five of them are
direct reversals of an `IMPORTS` edge.

That 63-vs-256 ratio is also the honest measure of `IMPORTS` recall on this codebase, and it comes
back in §7 as the single biggest limit of the whole feature.

---

## 2. The algorithm

### 2.1 Input

Layers are computed over **the same elements `c4` already emits**, not over a second granularity.
Concretely, the input is the `element_of` map and the `imports`-sourced subset of the relations
that `build_c4_payload` already produces:

```
files ──keep_source──► kept ──group_elements(depth)──► element_of
                                                          │
IMPORTS file pairs ───────────────────────────────────────┤
                                                          ▼
                                             element-level IMPORTS digraph G
```

The same filters apply, unchanged: self-loops folded into cohesion, ancestor pairs dropped (LikeC4
forbids an element relating to its own ancestor), out-of-scope endpoints dropped. Reusing the
existing pipeline means the boxes in the layer view are the boxes in the index view — the same
objects, re-read.

Cost of this choice: layer granularity is `--depth`-dependent. See §7.

### 2.2 Ranking

```
1. Tarjan SCC over G                       →  components
2. Condense: C = G / SCC                   →  always a DAG, by construction
3. Reverse-topological DP over C:
       height(c) = 0                       if c has no outgoing edges
       height(c) = 1 + max height(succ)    otherwise
4. rank(element) = height(component(element))
```

O(V+E). Deterministic given a deterministic node order — **sort element ids before iterating**, or
the emitted `.c4` will churn between runs on an unchanged repo, and `c4`'s output is meant to be
committed and diffed.

**Why height-to-sink, not depth-from-source.** Depth-from-source buckets by "how far from an entry
point," which scatters shared leaves: a utility reached both directly from the CLI and via four
hops of provider code lands at whichever depth the longest caller chain gives it, and identical
primitives end up on different rows. Height-to-sink puts every pure leaf at 0 together — `paths`,
`outcome`, `term`, `provider` — which is the reading people expect from a layer diagram.

**Why longest path, not shortest.** This is the decisive part and it is not a matter of taste.
With `height(u) = 1 + max height(v)`, every edge `u → v` satisfies `height(u) ≥ height(v) + 1`, so
**every edge strictly descends**. With `1 + min`, that invariant does not hold: one shortcut edge
from a high module directly to a leaf drags it down next to its own dependencies, and the diagram
then shows edges pointing sideways and upward for no architectural reason. Longest path is the only
rule that guarantees the picture reads correctly.

### 2.3 The theorem that reframes the feature

That invariant has a consequence the brief did not state and which the implementation must not
accidentally paper over:

> On a DAG, longest-path ranking makes every edge strictly descend. Therefore **inferred layers
> produce zero violations, always, by construction.**

The only thing inference can ever flag is an SCC of size > 1 — i.e. an import cycle. So:

```
             ranks from        edges judged      can fail CI?
  ┌──────────────────────────────────────────────────────────┐
  │ inferred  │  IMPORTS  │  IMPORTS         │ cycles only    │
  │ declared  │  config   │  IMPORTS         │ YES — the gate │
  │ declared  │  config   │  CALLS|USAGE     │ never          │
  └──────────────────────────────────────────────────────────┘
```

This is not a defect. It is what makes the config worth writing, and it is also the mechanism that
solves the false-positive problem in §5.4 — a config *generated from* inferred ranks is a green
baseline on the commit that generated it, by the same theorem.

It does mean one honest statement belongs in the output: an empty violation report with no declared
config is not a clean bill of health. It is the absence of an opinion.

### 2.4 When `IMPORTS` *does* have cycles

This repo is clean. Most will not be. This is the part most likely to be got wrong, so it is
specified tightly.

**Principles.** Never fail. Never refuse to emit — the repo with cycles is the repo that most needs
the picture. Never silently flatten. Never lie by deletion.

**Rules.**

1. An SCC of size > 1 is condensed into **one synthetic element** of kind `cycle`, occupying
   exactly one rank. Its metadata carries `members` (sorted ids, capped for readability with an
   overflow count) and `size`. This is the only honest rendering: the members genuinely have no
   relative order, and drawing them stacked would invent one.
2. Every such SCC is emitted as a **`cycle` finding**, listing members and one witness back-edge per
   member pair found. `cycle` findings **do** gate CI — unlike overlay up-edges, an import cycle is a
   source-confirmed structural fact.
3. The header states the condensation, so a reader knows how much was collapsed:
   `// 58 elements -> 55 condensation nodes (3 cycles, largest 21 members)`.
4. **Degradation guard.** If the largest SCC covers more than 50% of elements, the layer view is
   close to meaningless. Emit it anyway, but mark it `degraded: true` in the payload, say so in the
   view `description` (where a diagram's reader sees it, not only in a comment), and have the CLI
   print one warning line. The 50% figure is calibrated by a real measurement, not invented: the
   union on this repo condenses 21 of 58 into one node, which is exactly the shape this guard is
   meant to catch.
5. Declared layers are unaffected by cycles — a cycle spanning two declared layers produces the
   ordinary `violation` findings for its wrong-way edges *and* a `cycle` finding. Both, not one.

**Explicitly rejected: minimum feedback arc set.** Removing a minimal edge set to force a DAG is
NP-hard, the practical heuristics are non-deterministic (so the committed `.c4` would churn), and
the removed edge is precisely the interesting one. Making the picture pretty by deleting the finding
is the failure mode this whole feature exists to prevent.

**Explicitly rejected: refuse to emit.** Fails the repos with the most to gain.

---

## 3. The config

### 3.1 Where it lives

`.codeintel.toml` at the project root — the file `load_config` already reads. `config.py:86`
(`out.update({k: v for k, v in cfg.items() if k not in _DEFAULTS})`) preserves unknown keys
verbatim, and `tests/test_config.py:63` pins that behaviour. So a `[layers]` table survives
`load_config` today with **zero changes to `config.py`**.

It survives *unvalidated*, though. `_DEFAULTS` and `_ENUMS` are scalar- and enum-shaped; a nested
table does not fit `_coerce`'s model and forcing it in would distort a module whose contract is
"every key degrades to a default." So **`c4` validates the layers block itself**, in a new
`c4_layers.py`, under the same never-raise / degrade-and-report contract as the rest of the module.
A malformed `[layers]` yields a named `reason`, not a traceback, and not a silently empty check.

This repo currently has no `.codeintel.toml`. The layers feature would be the first reason to write
one — worth noting, because it means the file's ergonomics are untested in practice.

### 3.2 Schema

```toml
[layers]
# Top to bottom. The ORDER is the rank: index 0 is highest; imports may only point down.
order = ["cli", "server", "gateway", "providers", "core"]

# Optional switches, both default false — see §5.2 for why.
strict_adjacent  = false   # true: skipping a layer is a violation
allow_same_layer = true    # false: a same-layer import is a violation
require_all      = false   # true: an element matching no layer is a finding

[layers.members]
cli       = ["src/codeintel/commands/**", "src/codeintel/__main__.py"]
server    = ["src/codeintel/server.py", "src/codeintel/http_server.py"]
gateway   = ["src/codeintel/gateway.py", "src/codeintel/policy.py", "src/codeintel/auth.py"]
providers = ["src/codeintel/providers/**"]
core      = ["src/codeintel/*.py"]

# Known exceptions. `reason` is REQUIRED.
[[layers.allow]]
from   = "src/codeintel/graph_resolution.py"
to     = "src/codeintel/server.py"
reason = "settle-loop reach-through; tracked in #412"
```

### 3.3 Membership matches **file paths**, not element ids

This is the most consequential schema decision, so it gets its own heading.

Element ids (`providers.graph`, `commands`) depend on `--depth`, which is **auto-fit** — it moves
the moment a repo crosses the 100-element cap. A config keyed on element ids would silently stop
matching when a repo grew, and the failure mode is the worst kind: the check keeps passing while
covering less and less. File paths are stable across depth changes, across the cap, and across
`--scope`.

So: patterns are globs over the repo-relative file path. An element's layer is derived from its
member files.

Matching rules:

| | |
|---|---|
| `*` | matches within one path segment, not across `/` |
| `**` | matches across segments |
| resolution | **most specific pattern wins** |
| specificity | (1) count of literal segments before the first wildcard, then (2) pattern length, then (3) position in `order` |
| genuine tie | all three equal → `layer-ambiguous` finding, first in `order` wins, and the ambiguity is *reported* |

Most-specific-wins is what lets `core = ["src/codeintel/*.py"]` act as a catch-all while
`gateway = ["src/codeintel/gateway.py"]` still claims its file, without the author having to reason
about ordering. First-match-in-order would work too, but only if authors happen to list specific
before general, and getting it wrong is invisible.

**Implementation trap worth naming:** do not reach for `fnmatch` here. `fnmatch`'s `*` crosses `/`,
so `src/*.py` would match `src/a/b.py` — silently widening every pattern an author writes. Write the
~20-line explicit segment matcher. `pathlib.PurePath.match` is also not a fix; its `**` handling
only became correct in 3.13, and this package's floor is 3.11.

### 3.4 Elements whose files span layers

At depth 3, `src/codeintel/commands/` is one element. If the config puts `commands/doctor.py` in
`cli` and `commands/_common.py` in `core`, the element has no single layer.

Resolution: the element takes the **highest** layer any of its files matches, and records
`layer_split: {"cli": 12, "core": 1}` in metadata plus a `split` finding.

Highest, not majority, because layering constrains what may depend on what: a container holding a
`cli` file must be treated as `cli` for direction purposes, or edges into it are wrongly judged OK.
The conservative direction for a check is "report more." The finding should also say the obvious
fix — a deeper `--depth` resolves the split.

### 3.5 Elements the config does not mention

They land in **`unassigned`**. Not the bottom layer, not dropped.

`unassigned` is rendered as its own band at the bottom, and edges touching it are **never
violations** — you cannot violate an order you did not declare. The CLI reports the coverage
number, which is the honest measure of how much of the check is real:

```
43 of 58 elements assigned to a declared layer; 15 unassigned
```

`require_all = true` promotes unassigned elements to a gating finding, for teams who want the config
exhaustive. Default off.

### 3.6 The shorthand form, and why it may not gate

The user's sketch was `layers: cli > gateway > providers > storage` — an order with no membership.
Support it (`order` without `[layers.members]`), and fall back to inferring membership by matching
each layer name against path segments (`providers` claims `**/providers/**`).

But: every such assignment is stamped `inferred_membership: true`, and **`--check` refuses to gate
on it** — it exits 1 with `declared order without declared membership cannot gate; add
[layers.members]`. Guessing which layer a file belongs to and then failing someone's build on the
guess is the worst outcome this feature could produce. The shorthand is for looking, not for
gating. (This is the same instinct as `docs/c4.md`'s "`c4` does not guess a scope.")

### 3.7 Surfacing declared-vs-inferred disagreement

Both ranks are **always** computed and both are always in the payload: `layer_declared` and
`layer_inferred_rank` per element. Nothing is silently reconciled.

Two disagreement signals fall out:

- The **violation set itself** is the primary one: an edge whose declared direction is wrong is
  exactly a place where the code disagrees with the architect.
- **`spread`** — a declared layer whose members span a wide inferred rank range (say ≥ 3) is
  probably two layers wearing one name. Informational only, cheap to compute, and genuinely useful
  when someone first writes a config. Reported as `layer 'core' spans inferred ranks 0..4`.

---

## 4. Emitting it as LikeC4

### 4.1 The real tension

LikeC4's `model {}` containment is a single tree — an element has exactly one parent. Today that
parent is a directory. Layers are an orthogonal partition of the same set. **You cannot have both
as containment.** Every option below is a way around that, and they are not equally good.

Also hard, and already encoded in `plan_output`: LikeC4 merges every `.c4` in a directory into one
project, and declaring the same element id twice is a duplicate-element parse error. So "just emit
a second model file next to the first" is not available.

### 4.2 Options

**A — tags + explicit-enumeration views.** *(Recommended for v1.)*
Model containment stays directory-shaped and completely unchanged. Each element gains a
`#layer_<name>` tag. Layer views are new `view` blocks that `include` the element ids of each layer.

- Guaranteed to work: uses only tag declaration, `#tag` on an element, and `include <id>` — all
  three already in use in `c4.py` today.
- Composes cleanly with the concurrent renderer work: that agent is adding views; this adds views.
- **Weakness, stated plainly:** tags colour and filter, they do not position. `autoLayout TopBottom`
  will lay out by edge direction, which on `IMPORTS` happens to correlate strongly with rank — but
  LikeC4 is not being *told* about the bands. The result may read as a coloured graph rather than a
  stack. Acceptable for v1; the reason Phase 0 probes option C.

**B — invert containment: layers as parents, directories as tags.** *(Rejected as a replacement.)*
Gets true stacking for free. Destroys the directory view, which is the thing that works today and
that `docs/c4.md` documents. Cannot coexist in the same output directory (duplicate ids). Could ship
as a *separate* output dir (`codeintel-c4-layers/`), which does work and needs no uncertain LikeC4
feature — keep this as the fallback if both A and C disappoint, at the cost of two `likec4 start`
invocations and a second ownership marker.

**C — `group` inside a view body.** *(Preferred, conditional on a probe.)*
I believe LikeC4 supports a `group` construct in view bodies for visual grouping that does not
change model containment. If it exists and it constrains layout into bands, it is the exact answer.
**I am not confident of the syntax, and not confident it does more than draw a box.** Do not write
code against it until Phase 0 confirms.

**D — LikeC4's deployment model.** *(Named, not recommended.)*
`deployment { }` with `instanceOf` is a genuinely separate hierarchy over already-declared model
elements, plus `deployment view` to render it. Structurally that is *precisely* "a second
containment tree." Semantically it means "where this runs," and using it for layers will confuse
anyone who knows LikeC4. It may also carry constraints I do not know — whether an element can be
instantiated more than once, whether relationships are auto-derived into the deployment view.
Probe it in Phase 0 for completeness; adopt only if C fails and B's two-directory cost is judged
worse than the semantic abuse.

### 4.3 What A looks like concretely

Additions to `SPECIFICATION_BLOCK`:

```
  element cycle {
    notation 'Import cycle'
    style { shape rectangle color ci_bad border dashed }
  }
  relationship violates { line dotted color ci_bad head normal }

  tag layer_cli   { color #0c8ba6 }
  tag layer_core  { color #586472 }
  tag violation   { color #c0392b }
  tag cycle       { color #c0392b }
  tag allowed     { color #586472 }
```

(`ci_bad #c0392b` would be a third user-declared colour alongside the existing `ci_accent` /
`ci_chrome`. The user-declared-colour mechanism is already verified against LikeC4 1.59.2 — see the
comment above `SPECIFICATION_BLOCK` in `c4.py`.)

Views — **enumerate ids explicitly, do not use a tag-filter predicate**:

```
  view layers {
    title 'codeintel — layers'
    description '<provenance line: IMPORTS only, coverage stated>'
    include cli_a, cli_b, server, gateway, providers_graph, core_paths
    autoLayout TopBottom
  }
```

I have seen LikeC4 tag filtering written both as `include element.tag = #x` and as
`include * where tag is #x`, and **I do not know which is correct for 1.59.2, or whether both are.**
The generator does not need to know: it computed the membership, so it can emit the ids. Explicit
enumeration sidesteps an unverified grammar feature entirely and is strictly more robust. Adopt the
predicate form later, if ever, purely for readability of the generated source.

**Violating edges carry the kind, not a duplicate relation.** An edge is emitted once; its kind is
`violates` when it violates, otherwise `imports` / `calls_usage` as today. So the violation view is
"the elements incident to a `violates` edge," and there is no risk of duplicate-relation semantics.

**Do not emit an empty violations view.** If there are no violations, emit a `// no layer
violations` comment and let the CLI and `--json` carry the zero. Diagrams are bad at showing
absence, and an empty view is at best a blank canvas and at worst a parse question I have not
verified.

### 4.4 Coexistence with the concurrent renderer work

Named explicitly, because these are the seams that will conflict:

- **`_ident`, reserved-word escaping, collision suffixing.** Layer names come from user config and
  can contain anything. Layer tag ids and any layer element ids **must** go through whatever
  escaping/collision helper that agent lands. Do not fork a private copy — a design that sanitises
  layer names differently from element names will produce two ids that collide in one file.
- **View emission.** Emit layer views from a *separate function* returning a list of lines that
  `render_c4_dsl` appends inside `views {}`, rather than threading edits through the existing
  emitter. That agent is adding per-container drill-down views to the same block; two diffs in one
  function is an avoidable merge.
- **`_EMPTY` / `_EMPTY_STATS`.** Adding top-level `layers` and `findings` keys means adding them to
  `_EMPTY` too, or a failure path returns a payload missing keys a renderer reads. Note that
  `render_c4_dsl` already has a deliberate asymmetry here (`elements` is *not* defaulted, by
  design) — preserve that intent: `layers` should be `.get`-defaulted, because a model without
  layers is legitimate, whereas a model without elements is a caller bug.
- **`fan_in` is contaminated over the union — do not reuse it.** `element["fan_in"]` in today's
  payload counts union edges, and §1 measured that 54 of the 60 returned `CALLS|USAGE` edges into
  `cache.py` are `dict.get(...)` misattributed to `ContentHashCache.get`. `cache.py` is therefore
  the highest-fan-in element in the model on roughly nine tenths fabricated evidence. Any layer-side
  metric — fan-in, fan-out, "most depended upon," an element's rank weight — must be recomputed
  from **`IMPORTS` only**, under its own field name, rather than reading the existing one. The same
  caution applies to any `weight`/`n` on an `advisory` record (§5.5): a count over `CALLS|USAGE` is
  a count of name matches, not of dependencies, and must not be presented as a severity signal.
  Stated at this length deliberately, so a later implementer does not rebuild the trap by reaching
  for the field that is already sitting there. (This measurement has been relayed to the agent
  currently revising the hotspot threshold; changing that threshold is their call, not this
  design's.)

---

## 5. The violation view and its CI story

### 5.1 What counts as a violation

> An element-level **`IMPORTS`** edge `u → v` where both `u` and `v` have a **declared** layer, and
> `v`'s layer is strictly *above* `u`'s layer in `order`.

Both ends must be declared. `IMPORTS` only. No exceptions, no heuristics.

### 5.2 Finding classes and what gates

| class | source | default gating |
|---|---|---|
| `violation` | `IMPORTS`, both ends declared, wrong direction | **exit 2** |
| `cycle` | SCC of size > 1 in element-level `IMPORTS` | **exit 2** |
| `unassigned` | element matches no declared layer | exit 2 *only if* `require_all` |
| `allow-no-reason` | allowlist entry with no `reason` | **exit 2** |
| `advisory` | `CALLS`/`USAGE`-only edge pointing up | never |
| `split` | element's files span declared layers | never |
| `spread` | declared layer spans a wide inferred rank range | never |
| `stale-allow` | allowlist entry matching no current edge | never |
| `layer-ambiguous` | two patterns tie on every specificity key | never |

Two defaults deserve their reasoning:

- **Same-layer imports are allowed.** They are normal. `allow_same_layer = false` for teams who
  want strictly acyclic layering.
- **Skip-layer imports are allowed.** `cli` importing `core` directly, skipping `gateway`, is not a
  violation by default. Half the "layered architecture" literature means strict adjacency and half
  does not; strict adjacency produces a wall of findings on every real codebase. `strict_adjacent =
  true` opts in.

### 5.3 Exit codes

`c4`'s existing contract is `0` = a model was written, `1` = nothing was written and the reason is
named. A check introduces a **third** outcome that neither covers: it ran fine, wrote the model, and
found problems.

| code | meaning |
|---|---|
| `0` | ran; nothing gating found (or `--check` was not passed) |
| `1` | the command failed — nothing written, reason named. **Existing contract, unchanged.** |
| `2` | `--check` only: ran successfully, gating findings exist |

`2` rather than `1` is not cosmetic. A CI step must distinguish "codeintel is broken / the repo is
not indexed" from "your architecture drifted." Conflate them and the first person to hit a broken
index will allowlist the failure, and the gate is dead. Corollary: `--check` on a repo that fails to
index exits **1**, not 2.

`--check` deliberately does **not** imply `--no-index` — one behaviour per flag — but the CI example
in the docs should show both, for the reason `docs/c4.md` already gives.

### 5.4 Avoiding a wall of false positives on first run

Four mechanisms, in order of importance:

1. **The check is opt-in and needs a config.** No `[layers]` block → `--check` exits 0 with "no
   declared layers; nothing to check." Nobody gets a wall they did not ask for. This falls out of
   §2.3 for free.

2. **`--suggest-config` produces a green baseline, provably.** It prints a `[layers]` block derived
   from the inferred ranks. Because those ranks come from the very edges the check will judge, and
   because longest-path ranking makes every edge strictly descend (§2.3), **pasting the suggestion
   yields exactly zero `violation` findings on the commit that generated it.** Every finding after
   that is a real change. This is the strongest possible answer to "wall of false positives," and it
   is a theorem rather than a hope.

   The suggested layers are named `layer_5` … `layer_0`, with a comment listing each layer's members
   and telling the author to rename. **Do not try to guess that a layer "is the gateway layer"** —
   that is exactly the kind of inference `docs/c4.md` refuses ("`c4` does not guess a scope"), and
   getting it wrong in a file the user is about to commit is worse than being blunt.

3. **The allowlist, with a required `reason`.** An entry without one is itself a gating finding
   (`allow-no-reason`). That single rule is the difference between an allowlist and a mute button.
   Allowed violations are still **drawn** (dotted, tagged `#allowed`) and still counted — visible,
   just not gating.

   And: **report stale entries.** An allowlist entry matching no current edge means the violation
   was fixed and the exception should be deleted. Without staleness reporting an allowlist only ever
   grows, and in three years nobody knows which entries are load-bearing.

4. **No `--max-violations N` ratchet.** Rejected. A numeric budget invites a team to sit at N
   forever, and it says nothing about *which* violations are tolerated. The allowlist is the
   ratchet, and it names every exception and why.

### 5.5 The violation record — **decided**

The report format question is really "what does a violation record need to carry," and Phase 2 has
not produced a real finding yet. So: **define the record as data, serialize plain text first.**

Committing to SARIF now would lock the record shape to SARIF's model before we know the fields.
Committing to plain text now would make the record shape implicit inside a formatting function —
the same mistake in the other direction. A defined record with plain text as *the first of several*
serializers means adding SARIF or GitHub annotations later is a new serializer, not a refactor of
the check.

One finding is one record. Every finding class in §5.2 uses the same shape; irrelevant fields are
`null` rather than absent, so a consumer never has to branch on key existence.

```python
{
  # ---- classification -------------------------------------------------------
  "rule":      "layer-order",        # stable machine id; never renamed once shipped
  "kind":      "violation",          # the §5.2 class
  "severity":  "gating",             # gating | advisory | info — RESOLVED, see below
  "message":   "cli imports server, which is above it",   # one line, human

  # ---- the offending edge, at BOTH granularities ----------------------------
  "from_element": "commands",        # what the diagram labels
  "to_element":   "server",
  "from_paths":   ["src/codeintel/commands/status.py"],   # the member files carrying it
  "to_paths":     ["src/codeintel/server.py"],
  "witness":      {"from": "src/codeintel/commands/status.py",
                   "to":   "src/codeintel/server.py",
                   "n":    1},       # ONE concrete file pair, always present
  "witnesses_total": 3,              # file pairs behind this element-level finding
  "weight":          7,              # summed count(*) across them

  # ---- the layers ----------------------------------------------------------
  "from_layer": "cli",       "from_layer_index": 0,
  "to_layer":   "server",    "to_layer_index":   1,
  "direction":  "up",                # up | same | skip
  "layers_skipped": 0,

  # ---- provenance ----------------------------------------------------------
  "edge_source":  "imports",         # imports | cycle | calls_usage
  "confirmed_by": ["imports"],
  "cycle_members": null,             # populated only when kind == "cycle"

  # ---- disposition ---------------------------------------------------------
  "allowlisted":  False,
  "allow_reason": None,
  "allow_index":  None,              # which [[layers.allow]] entry matched

  # ---- context -------------------------------------------------------------
  "depth": 4,                        # the roll-up this was checked at
}
```

Six choices in there are load-bearing:

- **Both element id *and* file path, always.** Element ids are what the diagram labels and what a
  human recognises, but they are unstable across `--depth` (§3.3). File paths are stable but are not
  what the picture says. Neither alone is enough, and a later serializer needs the paths while a
  later text reader needs the ids.
- **A `witness` file pair is mandatory.** An element-level violation is an aggregate over possibly
  many file pairs; a finding a human cannot go look at is not actionable. `witnesses_total` plus a
  capped `from_paths`/`to_paths` gives the same treatment `c4.py` already applies to `dropped` via
  `DROPPED_REPORT_CAP` — show some, count the rest.
- **`severity` is resolved, not derived by the serializer.** `strict_adjacent` and `require_all` flip
  whether a given `kind` gates. The record carries the answer *after* config is applied, so no
  serializer re-implements config logic and no two serializers can disagree about the exit code.
- **`rule` is a stable string.** It costs nothing now and it is what a future allowlist could key on,
  what a suppression file would reference, and what SARIF's `ruleId` maps to. Renaming one after
  release breaks other people's config, so treat the set as an API.
- **Allowlisted findings stay in the list**, with `allowlisted: true` and `severity` demoted to
  `info`. They are not filtered out — §5.4's "visible, not gating" is a property of the record, not
  of one serializer's formatting.
- **Deterministic order.** Sort by `(kind, from_element, to_element, witness.from)`. Both the text
  output and the JSON must diff cleanly between runs, for the same reason the `.c4` must (§2.2).

**No line numbers, and this is not an oversight.** Measured against the live index: on an `IMPORTS`
edge, `a.line` is empty and `a.start_line` is `0` (the `File` node's own start), while
`b.start_line` *is* populated — 35, 18, 27 for `gateway.py`'s edges. So the graph can point at the
**imported definition in the target file** but not at the **import statement in the offending
file**, which is backwards from what a code annotation wants. The record therefore carries no line
field rather than an invented or misleading one. Getting the offending line would need a separate
mechanism — scanning the source file for the import — which is a later increment, not a field.

### 5.6 Serializers — **decided**

| serializer | phase | notes |
|---|---|---|
| plain text | **Phase 2, default** | what a CI log reader sees |
| `--json` | **Phase 2** | the records verbatim, see §6.2 |
| SARIF | deferred | sketched below so it is visibly considered, not overlooked |
| GitHub annotations | deferred | falls out of SARIF |

**Plain text.** The shape a CI log gets:

```
codeintel c4 --check — layer report
  project: codeintel      depth 4 (auto-fit)      mode: declared
  layers:   cli > server > gateway > providers > core
  coverage: 43 of 58 elements assigned; 15 unassigned

VIOLATIONS (2 gating, 1 allowlisted)
  layer-order  providers -> server    providers is below server
      src/codeintel/graph_resolution.py -> src/codeintel/server.py  (x2)
  layer-order  core -> gateway        core is below gateway
      src/codeintel/searcher.py -> src/codeintel/gateway.py  (x1)
  layer-order  providers -> server    ALLOWED: settle-loop reach-through; tracked in #412
      src/codeintel/graph_resolution.py -> src/codeintel/server.py  (x2)

CYCLES (0)

ADVISORY (14 — never gating)
  14 CALLS/USAGE-only edges point up the stack. These are NOT evidence of a
  dependency (see docs/layers-design.md §1). Use --json for the list.

2 gating finding(s) — exit 2
```

Three formatting rules that matter more than they look:

- **Two lines per finding, maximum:** the rule and the layer pair, then the witness. A CI log is
  skimmed, not read.
- **Advisory is collapsed to a count with a pointer, never enumerated.** It is the largest and least
  trustworthy class (§1), and a report that leads with fourteen advisories teaches people to ignore
  the whole thing. Collapsing it is a correctness property of the report, not a space saving.
- **The gating count and the exit code are on the same last line.** Whoever is reading a failed CI
  step is looking at the bottom.

**SARIF, deferred — the one-line sketch.** `tool.driver.name = "codeintel"`; `rules[].id =
record.rule`; one `result` per record with `level` = `error` for `gating` and `note` for
`advisory`/`info`; `locations[0].physicalLocation.artifactLocation.uri = witness.from` **with no
`region`**, because per §5.5 there is no line to put in one — GitHub would annotate the top of the
file, not the import. `partialFingerprints` from `rule + from_element + to_element` so runs dedupe.
Allowlisted records map to SARIF `suppressions[]` with `justification = allow_reason` — SARIF having
a native concept that fits the allowlist exactly is mild evidence the record shape is right.

---

## 6. CLI and MCP surface

### 6.1 A mode on `c4`, not a new command

It reads the same payload, writes into the same output directory, and shares every filter flag
(`--scope`, `--depth`, `--include-tests`, `--no-index`). A `codeintel layers` command would
duplicate all of them, and — worse — two commands writing into one directory means two writers for
one `.codeintel-c4.json` ownership marker, which is precisely the class of bug `plan_output` and
`write_model` exist to prevent.

### 6.2 Flags

| flag | effect |
|---|---|
| `--layers` | compute layers; emit layer views alongside the existing index view |
| `--check` | implies `--layers`; still writes the model; exit **2** on gating findings |
| `--suggest-config` | print a `[layers]` block from inferred ranks to stdout; **write nothing** (bare, like `--json`) |
| `--layers-from {auto,inferred,declared}` | default `auto` = declared if `[layers]` exists, else inferred. `declared` forces it and exits 1 if no config exists — so CI can assert "there had better be a config" |

`--json` is **reused, not duplicated**: the payload gains a top-level `layers` object
(`{mode, order, ranks, sccs, coverage, degraded}`) and a `findings` list. Anyone scripting against
`c4 --json` today keeps working.

**`--json` emits the §5.5 records verbatim — never a prose summary.** This is the clause that makes
deferring SARIF safe: if the JSON carries every field of every record, then anyone who needs SARIF
or GitHub annotations before we ship them can generate them from `--json` in a dozen lines, and
whoever ships them later is writing a serializer against a stable structure rather than reverse-
engineering a formatter. It also matches how the rest of this CLI behaves — `c4 --json` today prints
the payload, not a description of it, and `--suggest-config` prints pasteable TOML rather than
advice about what to paste.

Concretely: no re-formatting, no field renaming for display, no dropping `null`s to make the output
tidier, and **no filtering — allowlisted and advisory findings appear in `findings` too**, carrying
their own `severity` and `allowlisted` flags. A consumer that wants only the gating set filters on
`severity == "gating"`; a consumer that wants the full picture already has it. Text is the lossy
view; JSON is the record.

Default for `--layers`: **off in v1**, on by default once it has proven itself on more than one
repo. Turning on a new view by default in the same release that introduces it means the first bug
report is "my diagram changed and I don't know why."

### 6.3 MCP: not in v1, and probably never as a new tool

The MCP surface is deliberately four tools — `code.query`, `code.status`, `code.doctor`,
`code.map` — described to agents as a small orientation set. A CI gate is not an agent question, and
adding a fifth tool for it dilutes a surface whose value is partly its size.

The right v2 home is not a new tool but **a section in `code.map`**: `map` already writes a
committable architecture overview, and layers are exactly that. `## Layers` and `## Layer
violations` in the mapper's output is one function, no new tool, no new schema, and it lands the
information where an agent is already looking.

Gate that on Phases 1–3 holding up. Note the seam: `mapper.py` is not in the currently-contended set
of files, but `injector.py` (which `server.py` calls for map injection) **is** — re-check that
boundary before starting Phase 4.

---

## 7. Honest limits

In the spirit of `docs/c4.md`: coverage stated, not claimed.

**Inference can never find a violation.** Proved in §2.3. An empty report from inferred layers means
"no import cycles," and nothing more. It is not a clean bill of health; it is the absence of an
opinion. The report must say this in words, every time it runs in inferred mode.

**`IMPORTS` has low recall on dynamically-dispatched code — measured.** 63 `IMPORTS` file pairs in
`src/` against 256 under `CALLS|USAGE`. `__main__.py` drives fifteen command modules through an
`importlib` dispatch table and statically imports almost none of them, so **`__main__` ranks as a
near-leaf.** Generalised: *a layer diagram of a dynamically-dispatched codebase understates the top
of the stack.* This is the largest limit of the feature and it should be stated in the generated
header, not only here.

**The `CALLS`/`USAGE` overlay is not evidence of direction.** §1 measured why: 54 of 60 returned
edges into `cache.py` are `dict.get(...)` misattributed to `ContentHashCache.get`; two sampled
"mutual pairs" turned out to be a dict lookup and a keyword parameter name. The overlay is useful
for *drawing* an edge that `IMPORTS` missed. It must never gate, and the docs must not let a reader
believe a dashed up-arrow means anything.

**Layering measures direction, not appropriateness.** A `cli` module importing `core` directly is
legal under this model and may still be a terrible idea. The check tells you an import points the
wrong way; it cannot tell you an import that points the right way should not exist.

**Inferred ranks describe what the code does, not what it should do.** A module is rank 0 because
nothing it imports has imports — not because it is a primitive. On this repo, `term.py`, `paths.py`
and `policy.py` share rank 0, and only some of those are primitives.

**Granularity is `--depth`-dependent.** At depth 3, `src/codeintel/commands/` is one box and a
violation between two files inside it is invisible. The check is exactly as fine as the roll-up, and
`--depth` is auto-fit — so the same repo can get a coarser check simply by growing past the
100-element cap. The report should print the depth it checked at, every time.

**Tests are excluded by default,** so a test importing upward is never a finding. Usually right.
`--include-tests` changes the answer, and probably changes it into noise.

**Nothing here detects a missing layer.** It cannot tell you that a repo needs a gateway it does not
have. It only checks the order of the things that exist.

---

## 8. Phased plan

### Phase 0 — probes. No shipped code. (~half a day)

Two probes, both blocking, because committing to an emission shape without them is guessing.

**(a) LikeC4 grammar.** Install LikeC4 (the repo already assumes `npx likec4`) and answer, against
the actual installed version, recording findings the way `c4.py`'s colour comment does:
- Does `group` exist inside a view body? What is the syntax? Does it *position* elements into bands,
  or only draw a box?
- What is the correct tag-filter predicate in `include`? (Not needed if enumeration is used, but
  worth knowing.)
- Does a `view` with zero `include` statements parse?
- Can `deployment` / `instanceOf` hold already-declared model elements in a second hierarchy? Can an
  element be instantiated more than once? Are relations auto-derived into a deployment view?

**(b) Cycles in the wild.** Compute element-level `IMPORTS` SCC count and largest-SCC fraction on at
least three indexed repos. This repo is clean, so §2.4 — the part most likely to be got wrong — is
currently untested against reality. If no available repo has an `IMPORTS` cycle, **construct one** in
a fixture and test against that. Do not ship the degradation path unexercised.

### Phase 1 — inference only. No rendering at all. *(smallest useful increment)*

New module `c4_layers.py`, pure:

```
compute_layers(elements, relations) -> {ranks, sccs, degraded, condensation_stats}
suggest_config(ranks, elements)     -> str   # a pasteable [layers] TOML block
```

Wire-up: one call added in `build_c4_payload`, `layers` added to the payload and to `_EMPTY`,
`--layers`/`--suggest-config` added to the parser, `--json` carries it.

**No DSL changes. No new views. No exit codes.** Chosen deliberately: it ships value on day one (the
ranks alone answer "what is the shape of this repo"), it touches `c4.py` in exactly one place, and
because it renders nothing it **cannot conflict with the concurrent renderer work**.

### Phase 2 — declared layers and the check. Still no rendering.

Config parse + validate, path-glob membership matching (§3.3), split/unassigned handling, violation
computation, allowlist with required reasons and staleness, `--check`, exit code 2.

**Build the finding record (§5.5) before either serializer.** The check produces a list of records;
the text report (§5.6) and `--json` (§6.2) are two functions that consume it and share no formatting
logic. Order matters here: writing the text report first and extracting a record from it afterwards
is the refactor this decision exists to avoid.

Worth stating explicitly: **this phase needs no LikeC4 at all.** The gate is a record list, a text
report and a JSON payload. A team can adopt the entire check before any diagram exists — and that is
the highest-value part of the feature, so it should not wait behind a renderer.

### Phase 3 — rendering.

Option A (tags + explicit enumeration), upgraded to option C if Phase 0's probe supports it. Lands
*after* the concurrent renderer work merges, and reuses that agent's escaping/collision helpers
rather than forking them (§4.4).

### Phase 4 — `code.map` integration, conditional.

`## Layers` and `## Layer violations` sections in the mapper's output, if Phases 1–3 hold up on more
than one repo. Re-check the `injector.py` seam first.

---

## 9. Open questions

**Decided, previously open:** the violation report format. Structured record internally, plain text
as the first serializer, SARIF deferred but sketched — §5.5 and §5.6 carry the decision and its
reasoning. Recorded here because a reader looking for it in this list should find the pointer rather
than conclude it was overlooked.

The rest, listed rather than guessed at.

1. **Does LikeC4 `group` position, or only decorate?** Decides option C vs A, and therefore whether
   the layer view actually reads as a stack. Phase 0(a).
2. **Is the deployment model a legitimate second hierarchy for non-deployment concerns?** Named in
   §4.2 as option D; I do not know its constraints well enough to recommend it either way.
3. **50% is the degradation threshold in §2.4 — calibrated against one measurement.** It should be
   re-checked against Phase 0(b)'s survey rather than treated as settled.
4. **Should `--layers` become the default?** Deferred until it has run on several repos. The answer
   probably depends on how often option A's coloured-graph weakness (§4.2) shows up in practice.
5. **`spread`'s threshold (≥ 3 inferred ranks within one declared layer)** is a guess. It needs one
   real config on one real repo before it means anything.
6. **Monorepos.** Every path glob in §3.2 assumes one source root. A repo with `packages/*/src/`
   probably wants per-package layer configs, and this design does not address that. Out of scope for
   v1; flagged so v1 does not accidentally foreclose it — note that keeping membership keyed on file
   paths (§3.3) rather than element ids is what leaves that door open.
