# Contributing to codeintel

Thanks for your interest. codeintel is a small, well-tested codebase with a strict contract; the
bar for a change is that it keeps that contract and comes with tests.

## Development setup

```bash
git clone https://github.com/hamilton-sky/codeintel.git
cd codeintel
pip install -e .[dev]
pytest tests/ -q          # full suite
```

## The one rule: never raise

Providers, the gateway, and the HTTP handlers **must never propagate an exception** to a caller.
Every path returns the fixed safe-null envelope (`{ok, op, target, result, engine, cached,
reason?}`) or a well-formed HTTP JSON error. When you add a code path, add its fault-injection case
to `tests/test_never_raise.py`. If you must swallow an error, route it through
`codeintel.provider.log_swallowed(where, exc)` so `CODEINTEL_DEBUG=1` can surface it.

## Guidelines

- **Tests are required.** Match the existing style — real boundaries over mocks where practical
  (see `tests/test_reset.py`, `tests/test_http_auth.py`). Keep the suite green and fast.
- **Backends are contracts, not assumptions.** The graph (`codebase-memory-mcp`) and LSP (`serena`)
  shapes are verified live and documented in comments; if you change a query, verify it and update
  the comment.
- **Config is validated** in `config.py` — a bad value must degrade to a default, never crash.
- **Docs travel with code.** Update the README / `docs/` and `CHANGELOG.md` in the same PR.
- **Style.** Type hints, `from __future__ import annotations`, comments explain *why* not *what*.

## Pull requests

1. Branch from `main`, keep the change focused.
2. `pytest tests/ -q` green; add tests for new behavior.
3. Add a `CHANGELOG.md` entry under a new `Unreleased` section.
4. Open the PR with a clear description of the behavior change and how you verified it.

## Cutting a release

Releases are cut by pushing a `vX.Y.Z` tag, which triggers the PyPI publish workflow (PyPI Trusted
Publishing — no tokens in the repo). **The tag is the only thing that ships a release** — bumping
the version and writing the CHANGELOG does not, and forgetting the tag is silent: CI stays green
and the version simply never reaches users. Three releases were lost that way before the check
below existed.

1. Bump `__version__` in `src/codeintel/__init__.py` and add the matching `## [X.Y.Z]` CHANGELOG
   section (the newest entry must equal `__version__`).
2. `python scripts/check_release_consistency.py` — green, and it prints the exact tag command.
3. Merge to `main` and wait for CI.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. Confirm the release actually landed: the publish workflow is green and the new version appears
   on [PyPI](https://pypi.org/project/codecortex/). The publish job re-runs the release canary
   against the built wheel, so a broken artifact fails here rather than on a user's machine.

`scripts/check_release_consistency.py` runs in CI as its own check and fails when a documented
version has no tag, so a forgotten release is caught on the next push rather than months later.
