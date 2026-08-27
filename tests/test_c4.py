"""Tests for `codeintel.c4.build_c4_payload` (the data half) and `render_c4_dsl` (the renderer
half). Fixtures follow `tests/test_grapher.py`: patch `GraphProvider._resolve_project` /
`_query_rows` with the real row shapes (`{"f.file_path": …}`, `{"a.file_path": …, "b.file_path":
…, "count(*)": …}`), never fabricated ones.
"""
from __future__ import annotations

import json

from codeintel import c4
from codeintel.providers.graph import GraphProvider, ProjectResolution


def _wire(monkeypatch, *, available=True, project="proj", is_ancestor=False,
         file_rows=None, churn_rows=None, import_rows=None, calls_rows=None,
         query_raises=None, churn_raises=False, capture_cypher=None):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which",
                        lambda x: "/fake/cm" if available else None)
    resolution = (ProjectResolution(name=project, matched_root="/repo",
                                    scope="ancestor" if is_ancestor else "exact")
                 if (available and project) else None)
    monkeypatch.setattr(GraphProvider, "_resolve_project", lambda self, r: resolution)

    def _query_rows(self, cypher, proj, timeout_ms):
        if capture_cypher is not None:
            capture_cypher.append(cypher)
        if "change_count" in cypher:
            if churn_raises:
                raise RuntimeError("churn exploded")
            if churn_rows is not None:
                return churn_rows
            return [{"f.file_path": r["f.file_path"], "f.change_count": 0} for r in (file_rows or [])]
        if query_raises is not None:
            raise RuntimeError(query_raises)
        if "IMPORTS" in cypher:
            return import_rows or []
        if "CALLS" in cypher:
            return calls_rows or []
        return file_rows or []

    monkeypatch.setattr(GraphProvider, "_query_rows", _query_rows)


def _rows(paths):
    return [{"f.file_path": p} for p in paths]


# --------------------------------------------------------------------------- build_c4_payload

def test_build_payload_engine_unavailable_is_safe(monkeypatch):
    _wire(monkeypatch, available=False)
    payload = c4.build_c4_payload("/repo")
    assert payload["reason"] == "engine-unavailable"
    assert payload["elements"] == [] and payload["relations"] == []


def test_build_payload_never_raises_when_the_backend_explodes(monkeypatch):
    _wire(monkeypatch, query_raises="backend exploded")
    payload = c4.build_c4_payload("/repo")
    assert payload["reason"] == "error"
    assert payload["elements"] == []


def test_the_error_reason_never_carries_the_exception_text(monkeypatch):
    _wire(monkeypatch, query_raises="/Users/alice/x")
    payload = c4.build_c4_payload("/repo")
    blob = json.dumps(payload)
    assert "/Users/alice" not in blob
    assert payload["reason"] == "error"


def test_build_payload_refuses_an_ancestor_match(monkeypatch):
    _wire(monkeypatch, is_ancestor=True)
    payload = c4.build_c4_payload("/repo")
    assert payload["reason"] == "project-not-indexed-standalone"
    assert payload["elements"] == []


def test_the_model_never_carries_the_backends_project_id(monkeypatch, tmp_path):
    _wire(monkeypatch, project="Users-alice-Documents-project-app",
         file_rows=_rows(["src/a.py"]))
    payload = c4.build_c4_payload(str(tmp_path))
    blob = json.dumps(payload)
    assert "Users-alice" not in blob
    assert payload["project"] == tmp_path.name
    dsl = c4.render_c4_dsl(payload)
    assert "Users-alice" not in dsl


def test_the_dsl_contains_no_absolute_path(monkeypatch, tmp_path):
    _wire(monkeypatch, file_rows=_rows(["src/a.py", "src/b.py"]),
         import_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 1}])
    payload = c4.build_c4_payload(str(tmp_path))
    dsl = c4.render_c4_dsl(payload)
    assert "/Users/" not in dsl
    assert "'/" not in dsl                      # no quoted literal begins with a leading slash


def test_an_ancestor_edge_is_annotated_in_the_dsl_not_dropped_silently(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["main.py", "main/sub/x.py"]),
         import_rows=[{"a.file_path": "main.py", "b.file_path": "main/sub/x.py", "count(*)": 1}])
    payload = c4.build_c4_payload("/repo", depth=2)
    assert payload["stats"]["edges_dropped_ancestor"] == 1
    assert payload["relations"] == []
    dsl = c4.render_c4_dsl(payload)
    assert "main" in dsl and "main.sub" in dsl
    assert "DROPPED" in dsl


def test_an_out_of_scope_import_is_counted_not_forgotten(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["src/a.py"]),
         import_rows=[{"a.file_path": "src/a.py",
                       "b.file_path": "node_modules/pkg/index.js", "count(*)": 3}])
    payload = c4.build_c4_payload("/repo")
    assert payload["stats"]["edges_dropped_out_of_scope"] == 1
    dsl = c4.render_c4_dsl(payload)
    assert "out-of-scope" in dsl
    assert "1" in dsl


def test_intra_element_imports_are_reported_as_cohesion_not_discarded(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["pkg/a.py", "pkg/b.py"]),
         import_rows=[{"a.file_path": "pkg/a.py", "b.file_path": "pkg/b.py", "count(*)": 5}])
    payload = c4.build_c4_payload("/repo", depth=1)
    assert payload["stats"]["edges_internal"] == 5
    assert payload["relations"] == []
    elem = next(e for e in payload["elements"] if e["id"] == "pkg")
    assert elem["internal_imports"] == 5
    dsl = c4.render_c4_dsl(payload)
    assert "cohesion" in dsl


def test_rolled_up_relation_weights_are_summed_across_file_pairs(monkeypatch):
    files = ["svcA/a.py", "svcA/b.py", "svcA/c.py", "svcB/x.py", "svcB/y.py", "svcB/z.py"]
    imports = [
        {"a.file_path": "svcA/a.py", "b.file_path": "svcB/x.py", "count(*)": 2},
        {"a.file_path": "svcA/b.py", "b.file_path": "svcB/y.py", "count(*)": 3},
        {"a.file_path": "svcA/c.py", "b.file_path": "svcB/z.py", "count(*)": 4},
    ]
    _wire(monkeypatch, file_rows=_rows(files), import_rows=imports)
    payload = c4.build_c4_payload("/repo", depth=1)
    assert len(payload["relations"]) == 1
    assert payload["relations"][0]["n"] == 9


# --------------------------------------------------------------------------- CALLS|USAGE recall

def test_calls_usage_recovers_an_edge_imports_alone_misses(monkeypatch):
    """The measured recall defect: IMPORTS only sees module-level static imports (48% recall on
    this repo), and lazy/function-body imports never reach it. CALLS|USAGE is unioned in and, on
    its own here, is the only source that found this pair."""
    _wire(monkeypatch, file_rows=_rows(["src/a.py", "src/b.py"]),
         calls_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 4}])
    payload = c4.build_c4_payload("/repo", depth=2)
    assert len(payload["relations"]) == 1
    rel = payload["relations"][0]
    assert rel["n"] == 4
    assert rel["kind"] == "calls_usage"
    assert payload["stats"]["edges_from_calls_usage_only"] == 1
    assert payload["stats"]["edges_from_imports_only"] == 0
    assert payload["stats"]["edges_from_both"] == 0
    dsl = c4.render_c4_dsl(payload)
    assert "-[calls_usage]->" in dsl
    assert "relationship calls_usage" in dsl


def test_a_relation_confirmed_by_both_sources_is_labelled_imports_and_counted_once(monkeypatch):
    """A pair found by BOTH sources sums their weight but is labelled `imports` — never a
    stronger, unearned claim than what was actually found, and never double the evidence bucket."""
    _wire(monkeypatch, file_rows=_rows(["src/a.py", "src/b.py"]),
         import_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 1}],
         calls_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 2}])
    payload = c4.build_c4_payload("/repo", depth=2)
    assert len(payload["relations"]) == 1
    rel = payload["relations"][0]
    assert rel["n"] == 3
    assert rel["kind"] == "imports"
    assert payload["stats"]["edges_from_both"] == 1
    assert payload["stats"]["edges_from_imports_only"] == 0
    assert payload["stats"]["edges_from_calls_usage_only"] == 0


def test_calls_usage_row_count_is_recorded_in_stats(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["src/a.py"]),
         calls_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/a.py", "count(*)": 7}])
    payload = c4.build_c4_payload("/repo")
    assert payload["stats"]["calls_usage_seen"] == 1


def test_calls_usage_row_limit_truncation_is_reported(monkeypatch):
    monkeypatch.setattr(c4, "MAX_CALLS_ROWS", 2)
    _wire(monkeypatch, file_rows=_rows(["a.py", "b.py"]),
         calls_rows=[{"a.file_path": "a.py", "b.file_path": "b.py", "count(*)": 1}
                    for _ in range(5)])
    payload = c4.build_c4_payload("/repo")
    assert payload["stats"]["truncated"] is True


def test_a_src_to_tests_fabricated_edge_is_excluded_regardless_of_edge_source(monkeypatch):
    """Verified live against this repo's own graph index: both IMPORTS and CALLS|USAGE carry the
    edges `src/codeintel/server.py -> tests/test_http_server.py` and
    `src/codeintel/http_server.py -> tests/test_http_server.py`, although the string
    `test_http_server` never appears under `src/codeintel` — a backend bare-name-collision
    artifact, not a real import. `keep_source`'s default test-directory exclusion already drops
    both; this pins that the union of the two edge sources does not reintroduce it."""
    _wire(monkeypatch,
         file_rows=_rows(["src/codeintel/server.py", "src/codeintel/http_server.py",
                          "tests/test_http_server.py"]),
         import_rows=[{"a.file_path": "src/codeintel/server.py",
                       "b.file_path": "tests/test_http_server.py", "count(*)": 1}],
         calls_rows=[{"a.file_path": "src/codeintel/http_server.py",
                      "b.file_path": "tests/test_http_server.py", "count(*)": 2}])
    payload = c4.build_c4_payload("/repo")
    assert payload["relations"] == []
    assert payload["stats"]["edges_dropped_out_of_scope"] == 2
    dsl = c4.render_c4_dsl(payload)
    # Excluded from the MODEL (no `-[imports]->`/`-[calls_usage]->` edge asserts it happened) —
    # but disclosed, not silently dropped, matching this module's "never omit an edge silently"
    # contract (design section 6.2).
    assert "-[imports]->" not in dsl and "-[calls_usage]->" not in dsl
    assert "test_http_server" in dsl and "out-of-scope" in dsl


def test_the_header_states_the_edge_source_and_does_not_overstate_completeness():
    payload = _bare_payload()
    dsl = c4.render_c4_dsl(payload)
    assert "IMPORTS" in dsl
    assert "CALLS" in dsl and "USAGE" in dsl
    assert "not complete" in dsl.lower()


def test_zero_source_files_is_loud_not_an_empty_model(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["README.md", "docs/a.md"]))
    payload = c4.build_c4_payload("/repo")
    assert payload["reason"] == "no-source-files"
    assert payload["elements"] == []
    exts = dict(payload["stats"].get("top_extensions") or [])
    assert exts.get(".md") == 2


def test_an_explicit_scope_matching_nothing_is_an_error_not_a_fallback(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["src/a.py", "src/b.py"]))
    payload = c4.build_c4_payload("/repo", scope=("lib",))
    assert payload["reason"] == "scope-not-found"
    assert payload["stats"].get("scope_available_dirs") == ["src"]


def test_the_scope_string_is_never_interpolated_into_cypher(monkeypatch):
    captured: list[str] = []
    _wire(monkeypatch, file_rows=_rows(["src/a.py"]), capture_cypher=captured)
    injection = "x' OR '1'='1"
    c4.build_c4_payload("/repo", scope=(injection,))
    assert captured, "no cypher was captured — fixture wiring broke"
    assert all(injection not in cy for cy in captured)


def test_a_directory_element_carries_file_count_and_summed_churn(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["pkg/a.py", "pkg/b.py", "pkg/c.py"]),
         churn_rows=[{"f.file_path": "pkg/a.py", "f.change_count": 1},
                     {"f.file_path": "pkg/b.py", "f.change_count": 2},
                     {"f.file_path": "pkg/c.py", "f.change_count": 3}])
    payload = c4.build_c4_payload("/repo", depth=1)
    (elem,) = payload["elements"]
    assert elem["files"] == 3
    assert elem["churn"] == 6
    assert elem["path"].endswith("/")


def test_a_quote_in_a_filename_does_not_break_the_dsl(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["it's.py"]))
    payload = c4.build_c4_payload("/repo")
    dsl = c4.render_c4_dsl(payload)
    assert "it\\'s" in dsl                      # the apostrophe is escaped, not a raw break
    # every single-quoted literal is balanced: an odd count would mean an unescaped quote broke
    # out of a string literal somewhere in the emitted DSL.
    unescaped = dsl.replace("\\'", "")
    assert unescaped.count("'") % 2 == 0


def test_churn_query_failure_costs_only_churn(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["a.py"]), churn_raises=True)
    payload = c4.build_c4_payload("/repo")
    assert payload["reason"] == ""
    (elem,) = payload["elements"]
    assert elem["churn"] == 0


def test_row_limit_truncation_is_reported(monkeypatch):
    monkeypatch.setattr(c4, "MAX_FILE_ROWS", 3)
    _wire(monkeypatch, file_rows=_rows([f"src/f{i}.py" for i in range(5)]))
    payload = c4.build_c4_payload("/repo")
    assert payload["stats"]["truncated"] is True
    dsl = c4.render_c4_dsl(payload)
    assert "may be" in dsl.lower() or "truncat" in dsl.lower() or "cap" in dsl.lower()


# --------------------------------------------------------------------------- render_c4_dsl

def _bare_payload(**overrides):
    base = {
        "project": "demo", "engine": "graph", "op": "c4",
        "fit": {"depth": 1, "how": "auto-fit", "table": {1: 1}, "over_cap": False, "cap": 100},
        "elements": [{"id": "a", "path": "a.py", "title": "a", "kind": "module", "tech": "Python",
                      "files": 1, "churn": 0, "fan_in": 0, "fan_out": 0, "internal_imports": 0}],
        "relations": [], "dropped": [], "stats": dict(c4._EMPTY_STATS), "reason": "",
    }
    base.update(overrides)
    return base


def test_the_chosen_depth_and_the_full_fit_table_are_in_the_header():
    payload = _bare_payload(fit={"depth": 4, "how": "auto-fit",
                                 "table": {1: 6, 2: 11, 3: 15, 4: 22, 5: 125},
                                 "over_cap": False, "cap": 100})
    dsl = c4.render_c4_dsl(payload)
    assert "depth 4" in dsl
    assert "d5=125" in dsl


def test_a_view_over_the_cap_is_skipped_with_a_stated_reason():
    payload = _bare_payload(fit={"depth": 1, "how": "requested", "table": {1: 140},
                                 "over_cap": True, "cap": 100})
    dsl = c4.render_c4_dsl(payload)
    assert "SKIPPED" in dsl
    assert "100" in dsl
    assert "view index {" not in dsl
    assert "views {" in dsl


def test_the_palette_is_the_viewers_own_tokens_not_named_theme_colours():
    """A generated diagram and the interactive call-graph viewer are one tool's output. The three
    hexes are copied from `viewer/graph_template.html` (`--accent`, `--muted`, `--bad`), not picked
    to look similar — so a token change there is a visible diff here rather than silent drift.
    """
    dsl = c4.render_c4_dsl(_bare_payload())
    assert "color ci_accent  #0c8ba6" in dsl      # graph_template.html --accent (light)
    assert "color ci_chrome  #586472" in dsl      # graph_template.html --muted
    assert "#c0392b" in dsl                       # graph_template.html --bad, on the hotspot tag
    # the named-theme approximation these replaced must not linger in any colour slot
    assert "color slate" not in dsl
    assert "color sky" not in dsl


def test_the_view_states_that_codeintel_generated_it_and_how_far_to_trust_it():
    """`//` header comments are visible only to whoever opens the `.c4`; `description` is what a
    person looking at the rendered diagram sees."""
    dsl = c4.render_c4_dsl(_bare_payload())
    assert "description 'Generated by codeintel" in dsl
    assert "IMPORTS" in dsl and "CALLS|USAGE" in dsl
    # sits inside the view block, not at model scope
    view = dsl[dsl.index("view index {"):]
    assert "description 'Generated by codeintel" in view


def test_render_returns_empty_string_on_a_malformed_payload():
    assert c4.render_c4_dsl({"elements": None}) == ""
