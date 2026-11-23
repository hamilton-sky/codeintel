# Learnings: trust-fixes

Things a fresh worker would otherwise waste time rediscovering: a command that works, a
trap, a quirk of the environment. Append one or two lines, newest last, tagged with your
ticket number. Never edit or delete another line. Keep it short; a human prunes it.

- (01) `tests/conftest.py` stubs `graph_index_age` to `fresh` for every test; request the `real_index_age` fixture in any test that asserts `stale-index`/`index-age-unknown` (or their absence). Uncommitted `changed` tests on a fake root need `monkeypatch.setattr("codeintel.engines.graph.changed_range.in_work_tree", lambda root: True)`.
- (01) `changed_range.uncommitted_paths(root, within)` returns `(changed, untracked, error)`; a non-empty error means git could not list them and must become `index-age-unknown`, never fresh.
