# Learnings: trust-fixes

Things a fresh worker would otherwise waste time rediscovering: a command that works, a
trap, a quirk of the environment. Append one or two lines, newest last, tagged with your
ticket number. Never edit or delete another line. Keep it short; a human prunes it.

- (01) `tests/conftest.py` stubs `graph_index_age` to `fresh` for every test; request the `real_index_age` fixture in any test that asserts `stale-index`/`index-age-unknown` (or their absence). Uncommitted `changed` tests on a fake root need `monkeypatch.setattr("codeintel.engines.graph.changed_range.in_work_tree", lambda root: True)`.
- (01) `changed_range.uncommitted_paths(root, within)` returns `(changed, untracked, error)`; a non-empty error means git could not list them and must become `index-age-unknown`, never fresh.
- (01) Never read commit data with plain `git log`/`show`: `log.showSignature` (even from the repo's own `.git/config`) puts gpg text on stdout and runs its `gpg.program`; pass `--no-show-signature`. A fake signed commit for tests: `git hash-object -t commit -w` on a body with a `gpgsig` header (see `tests/test_index_age.py`).
- (02) A gap added with a VARIABLE section is counted as a possible `callees` gap by `test_graph_failure_population.py::test_every_gap_callees_can_report_states_its_numbers_in_the_body_too`, which then needs a scenario producing it; cross-cutting gaps use a constant section (`index`, `scope`, `backend`).
- (03) `Reindexer.maybe_reindex` returns False for a root that is not a directory: tests that drive the real one must pass an existing dir (`tmp_path`). In `Gateway._query` never resolve an empty `project_root` (realpath/load_config read cwd): `tests/test_rbac.py` patches `os.getcwd` and fails on it.
- (03) `git ls-files -m` runs the repo's own `filter.<x>.clean` (from .git/config + .gitattributes) on a same-size, stat-dirty tracked file, even with `core.fsmonitor=false`; `ls-files -c -o` does not. For a cheap tree fingerprint, stat the `-c -o` listing instead.
