# Scoping: splitting `GraphProvider` (SOLID)

> **Status: done.** The split described here shipped — `graph_backend.py`, `graph_resolution.py` and `graph_render.py` exist, and `wire_text.py` was added later at the transport seam this refactor created. Kept for the reasoning behind the seams.

`providers/graph.py` is 1,976 lines and `GraphProvider` is **41 methods / ~1,362 lines** — the
largest class in the codebase by a factor of two (next: `Indexer`, 22/608). It violates SRP by owning
four unrelated concerns. This is the plan to split it, ordered **safest-first** so momentum is built
on green, low-risk moves before the ones entangled with the test suite.

## The four responsibilities (the target seams)

| Concern | What it does | Representative members |
|---|---|---|
| **Pure text / model** | classify paths & labels, model an edge answer, render rows | `_strip_project_prefix`, `_lang_family`, `_is_module_scope_node`, `_SymbolTarget`, `_EdgeGroup`, `_group_edges`, `_display`, `_collapse_module_scope`, `_drop_edge_collisions` |
| **Backend transport** | speak the codebase-memory-mcp wire protocol, never raise, name the failure | `_run`, `_run_stdin`, `_run_rawjson`, `_query_rows`, `_search_symbols`, `_probe_wire_format`, `available`, `_cmd`, `_saw_unparsable`, `_last_failure` |
| **Project resolution** | root → backend project; not-indexed vs unreachable vs ancestor; caching | `_lookup_project`, `_resolve_project`, `_match_project`, `_project_root_of`, `probe`, `ProjectResolution`, `ProjectLookup`, the caches |
| **Op orchestration** (stays) | dispatch op → resolve → query → render; own per-query gap state | `build_result`, `_dispatch`, `_op_*`, `_render_edge_answer`, `_add_gap` |

Dependency direction (DIP): the orchestrator depends on a `BackendClient` and a `ProjectResolver`
via **constructor injection**, so the ops can be tested with fakes instead of monkeypatching private
methods.

## The real obstacle: test construction, not production code

Production coupling is mild — the ops call `self._run`/`self._query_rows` in ~11 places, absorbed by
thin delegators. The cost is in the tests:

- **`GraphProvider.__new__(GraphProvider)` is used in 5 test files** to build a provider that skips
  `__init__`, then sets `gp.available`, `gp._cmd`, `gp._saw_unparsable`, `gp._last_failure`,
  `gp._project_cache` directly. Moving any of that state into a sub-object breaks these unless the
  sub-object is also constructed there.
- **~18 seam stubs**: `._run =` (7), `._lookup_project =` (6), plus `_run_stdin`, `_run_rawjson`,
  `_query_rows`, `_search_symbols`, `_resolve_project`. As long as the orchestrator keeps these as
  overridable methods that internal code calls, a stub still intercepts — so **keep thin delegators**
  during transition (Strangler/Facade), migrate stubs to the collaborator in a later pass.
- External consumers of these on a provider: `grapher.py` (2), `mapper.py` (3), `reindexer.py` (1).
  Delegators keep them working.

## Phased migration (each phase: pure move, no behavior change, full suite green)

1. **Pure text/model → `codeintel/graph_render.py`.** Move the provider-independent free functions,
   constants and the `_SymbolTarget`/`_EdgeGroup` dataclasses out. **Home is top-level `codeintel/`,
   NOT `providers/`** — `providers/` is enforced engines-only: `test_loc_census` and
   `test_cold_process` glob `providers/*.py` and treat every module there as a backend, so a helper
   placed there fails both (as this phase's first attempt did). All later extractions
   (`BackendClient`, `ProjectResolver`) live top-level for the same reason. Consumers import from the
   new module directly; the handful of tests importing these names are updated in the same commit.
   **This is the first commit, shipped.**
2. **Backend transport → `codeintel/graph_backend.py::BackendClient`.** **Shipped.** `BackendClient`
   owns `available`/`_cmd`/`_saw_unparsable`/`_last_failure` + the transport + wire-format probe;
   `GraphProvider` exposes those four as get/set properties over it and keeps
   `_run`/`_run_stdin`/`_run_rawjson`/`_probe_wire_format`/`_clear_failure`/`_reset_wire_format_cache`/
   `_any_project_name` as thin delegators. The 17 `__new__` test sites each got a `gp._backend = ...`
   line; `_run_only_provider` and its two tests were repointed at `BackendClient`. Two deviations held
   the "no behavior change" line: `_query_rows`/`_search_symbols` stay on `GraphProvider`, calling
   `self._run` (the overridable delegator) + module-level `_parse_query_rows`/`_parse_search_results`,
   so the `_run` stub-seam is honoured and parse logic isn't duplicated; and `graph.py` keeps
   `import shutil`/`import subprocess` (`# noqa`) as the anchors out-of-scope tests monkeypatch.
   Verified by 829 tests + ruff + mypy + corpus 26, and by dogfooding codeintel's own `code.query`.
3. **Project resolution → `codeintel/graph_resolution.py::ProjectResolver`.** In progress. `ProjectResolver`
   is constructed with the `BackendClient` (it calls `list_projects`), owns
   `_lookup_project`/`_resolve_project`/`_match_project`/`_project_root_of` + the caches
   (`_project_cache`/`_negative_until`/lock) + the `ProjectResolution`/`ProjectLookup` value types.
   `GraphProvider` keeps `_lookup_project`/`_resolve_project` as overridable delegators (6 test stubs +
   grapher.py/mapper.py call them) and the caches as properties (tests set them); `probe` stays on
   `GraphProvider` as the doctor facade, calling into the resolver + backend.
4. **(Optional) renderer object.** Fold the `_add_gap`-coupled render methods into an
   `EdgeAnswerRenderer` that returns `(text, gaps)` instead of mutating `_pending_gaps`.

Secondary targets, same pattern, lower priority: `Indexer` (22/608), then `LspProvider` (17/467).

## Guardrails

- The 829-test suite + the opt-in corpus harness are the behavioral net; run both between phases.
- The extensive inline comments in `graph.py` are load-bearing institutional memory — move them
  **with** their code, never drop them.
- No `__version__` bump per phase; this refactor ships in a later release as one reviewed unit.
