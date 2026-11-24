# Commands: trust-fixes

The real commands for this project, read from its own config (`pyproject.toml`, `.github/workflows/ci.yml`). Approved by a human when the plan is approved. Workers read this file and must not change it.

Tools live in the repo venv and PATH is not set for subagents, so every command names `.venv/bin/` explicitly.

The Test command deselects `tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read`. It fails on main eb0902f only when tests run as root, as they do in this container (CI does not run as root). The reason is recorded in map.md under Decisions so far. The full suite takes about 4 minutes here, and the project has no faster marker or folder subset, so there is no `Quick:` line.

The Build line is a stand-in (brief, `Decided:`). The project has no compile step: `pyproject.toml` defines no build command, and CI's `build` job (`python -m build`, `twine check`, a wheel install and a canary in `.github/workflows/ci.yml`) packages a release rather than checking a change, and needs `pip install build twine`, which the venv lacks. So the flow's Build is `python -m compileall -q src/codeintel`, a syntax check that is in neither `pyproject.toml` nor CI. It exits non zero on a syntax error and proves nothing else.

The Lint line uses CI's own `ruff check src tests bench` (the `ruff` job in `.github/workflows/ci.yml`), not the brief's `ruff check .`. The user decided this (brief, `Decided:`); spec.md records it under Decisions. Acceptance tests under `plans/` are not ruff-gated.

`codeintel c4 . --check` writes its model to `--out`. Without `--out` it writes `codeintel-c4/` into the repo, so it is pointed at /tmp.

Mode: `feature`
Build: `.venv/bin/python -m compileall -q src/codeintel`
Test: `.venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read`
Lint: `.venv/bin/ruff check src tests bench && .venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check`
Typecheck: `.venv/bin/mypy`
Smoke: `.venv/bin/codeintel --version && .venv/bin/pytest tests/test_architecture.py -q -o addopts=`
Shared: `CHANGELOG.md, README.md, src/codeintel/core/provider.py`
Sensitive: `src/codeintel/core/containment.py, src/codeintel/core/redact.py, src/codeintel/gateway/auth.py, src/codeintel/gateway/policy.py, src/codeintel/transports/http.py, src/codeintel/setup/injector.py, src/codeintel/mapping/mapper.py`
Acceptance: `.venv/bin/pytest plans/trust-fixes/acceptance -q -o addopts=`
