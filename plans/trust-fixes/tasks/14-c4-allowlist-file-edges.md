# Match `[[layers.allow]]` against a violation's file-level import edges, whatever depth auto-fit picks

Type: task
Status: open
Blocked by: —
Test first: yes
Level: 2
Builder: backend
Touches: src/codeintel/c4/model.py, src/codeintel/c4/check.py, docs/c4.md, tests/test_c4_check.py, tests/test_c4.py

This ticket fixes the gap that stopped ticket 03's gate. It is not a red-team finding. The bar's `codeintel c4 . --check --out /tmp/codeintel-c4-check` (commands.md, Lint) fails on the branch for a reason no ticket's code causes, and CI's `c4-validate` job (`.github/workflows/ci.yml`, `python -m codeintel c4 . --check --no-index --out /tmp/c4-check`) would fail the same way once the branch merges. The user decided to fix c4 itself (decision card, 2026-11-23: "Add a ticket to fix c4").

**What breaks.** Evidence recorded by the conductor, with backend codebase-memory-mcp 0.10.8 (CI pins 0.10.*):

- On eb0902f, auto-fit picks depth 4. The false backend edge (the module `engines.graph.render` resolved to the method `Metrics.render`, see the comment in `.codeintel.toml`) is the element pair `src.codeintel.engines.graph -> src.codeintel.transports.metrics`. Allowlist entry 0 (`from = "src/codeintel/engines/graph/**"`, `to = "src/codeintel/transports/metrics.py"`) matches it, and the check reports 0 gating findings.
- On the branch, `plans/trust-fixes/acceptance/` adds elements, and `fit_depth` in `src/codeintel/c4/model.py` drops to depth 3. The same file edge rolls up to `src.codeintel.engines -> src.codeintel.transports`. The `transports` element's path is now the directory `src/codeintel/transports/`, so `_match_allow` in `src/codeintel/c4/check.py` no longer matches the file-level `to`. Result: a gating `layer-order` violation, a gating `import-cycle` (engines, gateway, transports, which exists only through that edge), and "STALE ALLOWLIST: entry 0 matches no current violation". 2 gating findings, exit 2.
- Cause: `check_layers` matches an entry against the element's own paths, and those paths depend on the depth. docs/c4.md promises the opposite for membership: "Element ids depend on `--depth`, which auto-fits and moves when a repo crosses the element cap", which is why membership matches file paths. The allowlist must follow the same rule.

**Approach.** Option A: carry each relation's file-level IMPORTS pairs in the payload and match the allowlist against those pairs in `check_layers`. Option B: pin the depth (`--depth 4` in commands.md and CI, or a depth field in `.codeintel.toml`). Chosen: A. It fixes the matching for every repo and every depth, which is what the user asked for. Rejected: B. It hides the bug only until the repo grows again, and it changes commands.md and CI.

1. **Carry the file edges.** In `build_c4_payload` (`src/codeintel/c4/model.py`), each relation gains `import_files`: a sorted list of `[from_path, to_path, n]`, one per IMPORTS row that rolled up into that element pair. CALLS/USAGE rows are not listed, because only `imports` relations can gate. A `calls_usage`-only relation carries `[]`. The list survives the `--edges imports` filter, because the filter keeps or drops whole relations. Add no new query: the rows are already in hand in the loop that fills `rel_weights`.
2. **Match per file edge.** In `check_layers` (`src/codeintel/c4/check.py`), for an offending `imports` relation that carries a non-empty `import_files`:
   - A file edge is allowlisted when some entry with a reason matches it: `from` matches the edge's from-file and `to` matches its to-file, by equality or by `glob_match`, the same matcher used today.
   - An entry that names both element ids (`from == a and to == b`) still allows the whole relation, as `tests/test_c4_check.py::test_an_allowlist_entry_may_use_a_glob_and_an_element_id` pins.
   - The violation is ALLOWED (`allowlisted: true`, `severity: info`, `allow_reason`/`allow_index` from the first matching entry) only when every one of its file edges is allowlisted. One file edge that no entry matches keeps it gating.
   - On a gating violation, `witness` names the first file edge no entry matches, so the report points at the import still to be fixed. On an allowed one it names the first file edge. `witnesses_total` is the number of file edges.
   - A relation with no `import_files` key (a hand-built payload, an older `--json`) keeps today's element-path matching unchanged. `tests/test_c4_check.py`'s existing allowlist tests and its `witness == {"from": "src/bot.py", "to": "src/top.py", "n": 3}` assertion pass unchanged.
3. **Stale detection uses the same matching.** An entry counts as used when it matched at least one file edge of any offending `imports` relation (or the element ids, as above), whether or not that violation ended up fully allowed. `stale-allow` is reported only for entries that matched nothing.
4. **Cycles.** A cycle finding is computed as today (`compute_layers` over the relations). Then compute the cycles again over the relations minus every relation whose file edges are all allowlisted (the same rule as step 2). A cycle that is in the first set and not in the second exists only through allowlisted edges: it stays in the list, with `allowlisted: true`, `severity: info` and `allow_reason`/`allow_index` from the entry that allowed the edge which breaks it. Every other cycle stays gating. Reuse `compute_layers`; do not change `src/codeintel/c4/layers.py`.
5. **Record shape.** Add no field to a finding record: `test_every_record_carries_every_field_so_no_consumer_branches_on_key_existence` pins the field set. `render_report` prints an allowed cycle with `ALLOWED: <reason>`, the way it prints an allowed violation.
6. **Docs.** `docs/c4.md`: in the `[[layers.allow]]` section, say that an entry is matched against the file-level import edges behind a violation, so it keeps matching whatever depth auto-fit picks; that a violation is allowed only when all of its file edges are; and that a cycle made only of allowlisted edges is allowed. Next to the `n_imports`/`n_calls_usage` paragraph, name the new `import_files` field.
7. **`.codeintel.toml` stays unchanged.** Do not widen or add an allowlist entry to get the self-check green. If the self-check still gates after this fix because an engines -> transports file edge outside entry 0 exists, that is a real finding: write it in the Answer with the edge and stop, rather than allowlisting it.

## Not in this ticket

- The CHANGELOG line for this fix is written by ticket 13.
- Fixing the backend's false `render` edge itself: it is codebase-memory-mcp's, and `.codeintel.toml` already documents it.
- `layers` (the inferred section of the payload) and `--suggest-config`: neither reads the allowlist.

## Done when

- These tests fail on main and pass here:
  - `tests/test_c4_check.py`: the same file edge (`src/codeintel/engines/graph/render.py -> src/codeintel/transports/metrics.py`) checked in two payloads, one at depth 4 (elements `src.codeintel.engines.graph`, `src.codeintel.transports.metrics`) and one at depth 3 (elements `src.codeintel.engines`, `src.codeintel.transports` with directory paths), with the allow entry `src/codeintel/engines/graph/** -> src/codeintel/transports/metrics.py`: the violation is ALLOWED at both depths, gating is 0 at both, and there is no `stale-allow` finding at either.
  - `tests/test_c4_check.py`: at depth 3, adding a second file edge `src/codeintel/engines/semantic/x.py -> src/codeintel/transports/http.py` to the same relation keeps the violation gating, and its witness names that edge; entry 0 is still not stale.
  - `tests/test_c4_check.py`: a three-element cycle (engines -> transports -> gateway -> engines) whose only upward edge is the allowlisted one is reported as an allowlisted `import-cycle` with severity `info`; the same cycle with a non-allowlisted file edge on the upward relation stays gating.
  - `tests/test_c4.py`: `build_c4_payload` (through `_wire`) gives each `imports` relation an `import_files` list of its IMPORTS file pairs with their counts, also under `edges="imports"`, and a `calls_usage`-only relation an empty list.
- `.venv/bin/pytest tests/test_c4_check.py tests/test_c4.py tests/test_c4_layers.py tests/test_c4_model.py tests/test_c4_output.py tests/test_summary_integrity.py tests/test_architecture.py -q -o addopts=` passes. The files after `tests/test_c4.py` are not on Touches and pass unchanged.
- `.venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check` on the branch exits 0 and prints `0 gating finding(s)`, with entry 0 not reported stale. The Answer records the backend version it ran against (`codebase-memory-mcp --version`) and the depth it auto-fit. If the graph backend is unavailable in the container, the Answer says so and the conductor's gate runs it.
- `.codeintel.toml` is unchanged (`git diff --stat` shows no change to it).

```check
$ .venv/bin/pytest tests/test_c4_check.py tests/test_c4.py tests/test_c4_layers.py tests/test_c4_model.py tests/test_c4_output.py tests/test_summary_integrity.py tests/test_architecture.py -q -o addopts=
prints passed
$ .venv/bin/codeintel c4 . --check --out /tmp/codeintel-c4-check
prints 0 gating finding(s)
```

## Reference

- Ticket 03's "Review findings (round 2, gate (full))": the failing report
- `.codeintel.toml`, the `[[layers.allow]]` entry and its comment
- docs/c4.md, "Membership matches file paths, not element ids" and the allowlist mechanism
- docs/layers-design.md §3.3 and §5.4

## Answer
