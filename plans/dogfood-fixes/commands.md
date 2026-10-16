# Commands: dogfood-fixes

The real commands for this project, read from its own config (pyproject.toml,
.github/workflows/ci.yml). Approved by a human when the plan is approved.
Workers read this file and must not change it.

The flow runs Smoke before every ticket and Build, Test and Lint after every
ticket, so each must run without asking anything and exit non zero on failure.

Build: `pip install -e .[dev]`
Test: `pytest tests/ -q`
Lint: `ruff check src tests bench && mypy`
Smoke: `codeintel --version`

No `Quick:` line: the project's own config has no faster subset of Test, so
the full suite runs after every ticket.
