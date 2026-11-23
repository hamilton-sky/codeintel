# Run the bar end to end, prove each regression test fails on eb0902f, and write the CHANGELOG

Type: task
Status: open
Blocked by: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12
Test first: no
Level: 1
Touches: CHANGELOG.md

The acceptance ticket. It proves the map's bar and records the change for users.

1. Run the whole gate from commands.md: the Test command (with its one deselect), `.venv/bin/ruff check src tests bench` (CI's lint command, which replaces the brief's `ruff check .` by the user's decision; spec.md Decisions, "The lint bar"), `.venv/bin/mypy`, and `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check`.
2. Prove each regression test against the old code.
   - In a scratch worktree outside the repo (`git worktree add /tmp/trust-fixes-main eb0902f`), copy in the new test files and the fixtures under `tests/fixtures/blindspots/`.
   - Run each regression test listed in tickets 01-12's Answers against eb0902f's `src`, using the repo venv with `PYTHONPATH` pointed at the worktree's `src`, and record that each one fails.
   - Remove the worktree afterwards.
   - Put the table (finding id → test → fails on eb0902f / passes on branch) in this ticket's Answer.
   - A test that passes on eb0902f is a finding not proven: reopen the ticket that claimed it.
3. Write CHANGELOG entries under `## [Unreleased]`: one line per T-group under Fixed (and Security for T6 and T7's leak), each naming the user-visible change. Do not bump the version.

## Not in this ticket

- The acceptance tests under `plans/trust-fixes/acceptance/` are written by the tester from spec.md and run by the conductor.
- No tag or release: the user tags from their Mac.

## Done when

- `.venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read` passes.
- `.venv/bin/ruff check src tests bench` prints `All checks passed!` and `.venv/bin/mypy` prints `Success`.
- `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check` exits 0 and prints `0 gating finding(s)`.
- The Answer holds the 34-row table, and every row reads "fails on eb0902f, passes on branch" (the reviewer checks this).
- `grep -c "" CHANGELOG.md` grows, and `## [Unreleased]` holds a Fixed section naming T1-T10's changes (the reviewer checks this).

```check
$ .venv/bin/pytest tests/ -q --deselect tests/test_doctor.py::test_semantic_deep_probe_warns_when_indexed_source_cannot_be_read
prints passed
$ .venv/bin/ruff check src tests bench
prints All checks passed!
$ .venv/bin/mypy
prints Success
$ .venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check
prints 0 gating finding(s)
```

## Reference

- map.md, "The bar"
- spec.md, Goal and the bar

## Answer
