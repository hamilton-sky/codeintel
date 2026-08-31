# Protecting `main`

`main` is where the release tag is cut from, and a `v*` tag publishes to PyPI **irreversibly** — a
version number, once uploaded, can never be reused even after a yank. So the two things worth
protecting here are `main` and the `v*` tags, and the tags matter at least as much.

Branch protection is a **GitHub setting**, not a file in the repo: nothing committed here turns it
on. What *is* committed is the exact configuration, as two importable rulesets, so the settings are
reviewable and reproducible instead of living only in one person's memory of which toggles they
clicked.

- [`.github/rulesets/main.json`](../.github/rulesets/main.json) — the `main` branch
- [`.github/rulesets/release-tags.json`](../.github/rulesets/release-tags.json) — `v*` tags

## Apply them

**Settings → Rules → Rulesets → New ruleset → Import a ruleset**, then upload each file. Review
what the form shows and **Create**. Repeat for the second file.

Rulesets are free on public repositories (this one is public). On a *private* repo they need Pro or
Team — that is the only plan gate that applies.

Both files ship with an **empty bypass list**, which means the rules apply to you too. If you want
to keep an escape hatch, add one in the UI after import: *Bypass list → Add bypass → Repository
admin*, and set it to **Always** (bypasses silently) or **For pull requests only** (must still open
a PR, but can merge it without the checks). Adding it in the UI rather than in JSON avoids pinning
a numeric role ID in a file that nobody will re-check.

## What `main.json` does, and why each rule is there

| Rule | Why |
|---|---|
| **Require a pull request**, 0 approvals | The change gets a diff, a CI run and a revert point. Approvals are set to **0** deliberately: GitHub does not let you approve your own PR, so on a single-maintainer repo any non-zero count means every merge needs a bypass — a rule you route around every day is not a rule. Raise it to 1 the day a second maintainer appears. |
| Dismiss stale approvals on push | Only bites once approvals are in use; harmless now, correct later. |
| Require conversation resolution | Review comments (including bot ones) have to be answered or resolved, not scrolled past. |
| **Require status checks** (list below) | The point of the whole exercise. Without it a PR is just a formality — you can merge red. |
| **Require linear history** | `CONTRIBUTING.md` cuts releases from a commit on `main`; a linear history means "the tag points at exactly the commit CI ran against" stays true. Merge methods are therefore limited to **squash** and **rebase** (a merge commit would violate this rule). |
| **Block force pushes** (`non_fast_forward`) | A force push to `main` can silently orphan a commit that a published tag points at. |
| **Block deletion** | Cheap; no legitimate use. |

### The required checks — and the two that are deliberately *not* required

Required checks are matched by **job name**, exactly as it appears in the Checks tab. From
`ci.yml`:

```
Lint (ruff)
Types (mypy)
Release consistency
test (3.11)   test (3.12)   test (3.13)
Graph backend contract (live, 0.9.*)
Graph backend contract (live, 0.10.*)
Build + verify the package
```

Two CI jobs are **omitted on purpose**:

- `LSP backend contract (live)`
- `LikeC4 model validates (live)`

Both are declared `continue-on-error: true`, and their comments in `ci.yml` say exactly why: they
fetch serena from a git HEAD this project does not control, and LikeC4 from npm. Making them
required would convert someone else's outage into an unmergeable `main` — the failure mode those
jobs were explicitly written to avoid. They still run, still show, and are still worth reading
before a release; they just do not gate the merge button. If either becomes reliable enough to
block on, drop its `continue-on-error` **and** add its context here in the same change.

The list also has to be re-checked whenever a job is renamed or the Python matrix moves: a required
check whose name no longer exists is **never reported**, and the PR waits forever. Renaming
`test (3.13)` without updating this file is the way that happens.

### "Require branches to be up to date before merging" is off

(`strict_required_status_checks_policy: false`.) Turning it on forces every PR to re-run all 11
jobs after every merge into `main`. With one maintainer merging serially, the semantic risk it
guards against — two PRs that are each green alone but broken together — is small, and the cost is
a full re-run per merge. Turn it on if concurrent PRs become normal.

> **Related CI cost.** `ci.yml` triggers on a bare `on: push:` with no branch filter, so once you
> are pushing feature branches for every change, each one runs the full matrix twice: once for the
> push, once for the `pull_request`. Narrowing the push trigger to `main` (and tags) halves it.

## What `release-tags.json` does

Applies to `refs/tags/v*` — the tags `publish.yml` publishes from:

- **Block deletion** and **block force update**: a `v*` tag cannot be moved to a different commit
  or deleted after the fact, so "what shipped as 0.22.0" stays answerable from the repo.
- **Tag name must match `^v[0-9]+\.[0-9]+\.[0-9]+$`**: `publish.yml` fires on `v*`, so a typo like
  `v0.22` or `v.0.22.0` starts a real publish run that then fails somewhere inside it. This rejects
  the malformed tag at push time instead.

This does not stop a *wrong-but-well-formed* tag; `scripts/check_release_consistency.py` already
covers that by refusing a tag that disagrees with `__version__`.

## Two settings worth doing at the same time

Neither is a ruleset, and both sit closer to the irreversible action than `main` does:

1. **`pypi` environment reviewers.** Settings → Environments → `pypi` → *Required reviewers* (add
   yourself) and *Deployment branches and tags* → restrict to the `v*` tag pattern. The publish job
   then pauses for a human click before an upload that can never be undone, and cannot be run from
   an arbitrary branch.
2. **Actions permissions.** Settings → Actions → General → set *Workflow permissions* to **Read
   repository contents**, and require approval for workflow runs from **all external
   contributors**. This is a public repo: anyone can open a PR, and a PR that edits a workflow file
   is a PR that edits what CI executes.

## When you need to get around it

Do not delete the ruleset — set **Enforcement: Disabled** at the top of it, do the thing, and set
it back to **Active**. It leaves the configuration intact and the gap short. (`Evaluate` mode, which
logs violations without blocking, is Enterprise-only and not available here.)

## Verifying it took

```bash
gh api repos/hamilton-sky/codeintel/branches/main --jq .protected      # -> true
gh api repos/hamilton-sky/codeintel/rulesets --jq '.[].name'
```

Or push a trivial commit straight to `main` and watch it get rejected — that is the only test that
proves the rule is actually load-bearing.
