# `pathly/` — pre-implementation planning record

> **Status: historical, and pre-implementation.** Eighty-five markdown files written *before* this
> codebase existed in its current form, last touched at 0.16.0. They describe a project that was
> then hypothetical: `pathly/project/SPEC.md` calls itself a "greenfield project spec", names the
> PyPI distribution `codecortex`, and says the names are placeholders.
>
> The placeholder claim is the part that stopped being true, and not in the direction this banner
> used to say. It asserted "the distribution is `codeintel`" — it is not, and never was:
> `pyproject.toml`, PyPI and the README's own `pip install codecortex` all agree the distribution
> shipped as **`codecortex`**, exactly as the spec named it, while the CLI and the import name are
> `codeintel`. No version is quoted here any more either; it drifted twice and had to be corrected
> by hand each time. For what is current, see the [CHANGELOG](../CHANGELOG.md).
>
> **Nothing here is a description of the shipped system.** For that, read [`docs/`](../docs/README.md),
> which indexes every reference doc, or the top-level [README](../README.md).

## Why it is kept

The same reason the dated evaluations in `docs/` are kept: the reasoning is worth having, and
correcting a planning document after the fact would falsify the record of what was actually planned.
These files show which features were sequenced in which order, what each was for, and what the
acceptance criteria were before any of it was built — which is genuinely useful when asking why a
seam is where it is.

## What is in here

| Path | What it is |
|---|---|
| `project/SPEC.md` | The original greenfield project spec, decomposed below into features. |
| `project/SEQUENCING.md` | The order the features were meant to be built in. |
| `project/artifacts/BOARD_EVAL.md` | An evaluation of the planning board itself. |
| `features/<nn>-<slug>/` | One directory per planned feature, each holding a `SPEC.md`. |
| `features/<nn>-<slug>/goals/<goal>/` | Per-goal planning: `PLAN_ARCHITECTURE`, `IMPLEMENTATION_PLAN`, `HAPPY_FLOW`, `FLOW_DIAGRAM`, `USER_STORIES`, `VERIFY`, `REVIEW`. |

Not to be confused with `pathly-adapters`, the unrelated private repository the call-edge benchmark
measures against (see [`bench/README.md`](../bench/README.md)). The shared name is a coincidence of
the planning tool, not a relationship between the two.
