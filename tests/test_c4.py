"""Tests for `codeintel.c4.build_c4_payload` (the data half) and `render_c4_dsl` (the renderer
half). Fixtures follow `tests/test_grapher.py`: patch `GraphProvider._resolve_project` /
`_query_rows` with the real row shapes (`{"f.file_path": …}`, `{"a.file_path": …, "b.file_path":
…, "count(*)": …}`), never fabricated ones.
"""
from __future__ import annotations

import json
import re

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


# --------------------------------------------------------------------------- cross-language merge

def test_a_cross_language_same_stem_merge_is_reported_not_hidden(monkeypatch):
    """Defect (d): `element_key` strips the extension, so `api/index.ts` and `api/index.js`
    collapse into one element. Kept merged (undoing it needs a second identity axis this design
    does not have), but every path must be named, not just `paths[0]`, and the merge must be
    counted where a reader can see it."""
    _wire(monkeypatch, file_rows=_rows(["src/api/index.ts", "src/api/index.js"]))
    payload = c4.build_c4_payload("/repo")
    (elem,) = payload["elements"]
    assert elem["id"] == "src.api.index"
    assert elem["files"] == 2
    assert "index.ts" in elem["path"] and "index.js" in elem["path"]
    assert "TypeScript" in elem["tech"] and "JavaScript" in elem["tech"]
    assert payload["stats"]["cross_language_merges"] == 1

    dsl = c4.render_c4_dsl(payload)
    assert "index.ts" in dsl and "index.js" in dsl
    assert "cross_language_merges" not in dsl        # the stat name is internal; the prose isn't
    assert "different source languages" in dsl


def test_a_single_language_module_is_never_reported_as_a_merge(monkeypatch):
    _wire(monkeypatch, file_rows=_rows(["src/a.py"]))
    payload = c4.build_c4_payload("/repo")
    assert payload["stats"]["cross_language_merges"] == 0
    dsl = c4.render_c4_dsl(payload)
    assert "different source languages" not in dsl


# --------------------------------------------------------------------------- hotspot ranking

def test_hotspot_ranking_uses_imports_only_fan_in_never_the_contaminated_union(monkeypatch):
    """Defect (g), reconsidered against a live measurement: CALLS|USAGE matches by bare symbol
    name, so on this generator's own repo 54 of 60 incoming edges into `cache.py` were fabricated
    — every `dict.get(...)` call in the codebase attributed to `ContentHashCache.get`. Ranking a
    "hotspot" visual claim against that union would rank the fabrication. `busycalls` here has a
    HIGHER union fan-in than `target` (6 vs up to 5) and would have tripped even the old fixed
    `>= 5` cutoff — but every one of its incoming edges is CALLS|USAGE-only, so it must NOT be
    flagged; `target`'s 3 real IMPORTS-confirmed edges must be enough to flag it instead."""
    files = ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py", "h.py", "i.py", "j.py",
            "k.py", "target.py", "busycalls.py"]
    imports = [{"a.file_path": src, "b.file_path": "target.py", "count(*)": 1}
              for src in ("a.py", "b.py", "c.py")]
    calls = ([{"a.file_path": src, "b.file_path": "target.py", "count(*)": 1}
             for src in ("d.py", "e.py")]
            + [{"a.file_path": src, "b.file_path": "busycalls.py", "count(*)": 1}
              for src in ("f.py", "g.py", "h.py", "i.py", "j.py", "k.py")])
    _wire(monkeypatch, file_rows=_rows(files), import_rows=imports, calls_rows=calls)
    payload = c4.build_c4_payload("/repo")

    target = next(e for e in payload["elements"] if e["id"] == "target")
    busycalls = next(e for e in payload["elements"] if e["id"] == "busycalls")
    assert target["fan_in"] == 5           # union: 3 imports + 2 calls_usage
    assert target["import_fan_in"] == 3
    assert busycalls["fan_in"] == 6        # higher union fan-in than target
    assert busycalls["import_fan_in"] == 0

    assert target["hotspot"] is True
    assert busycalls["hotspot"] is False
    assert payload["stats"]["hotspot_threshold"] == 3
    assert payload["stats"]["hotspot_count"] == 1

    dsl = c4.render_c4_dsl(payload)
    target_block = re.search(r"  target = module '[^']*' \{\n(.*?)\n  \}\n", dsl, re.S).group(1)
    busycalls_block = re.search(r"  busycalls = module '[^']*' \{\n(.*?)\n  \}\n", dsl, re.S).group(1)
    assert "#hotspot" in target_block
    assert "#hotspot" not in busycalls_block
    assert "IMPORTS-only fan-in" in dsl


def test_no_hotspot_is_flagged_when_there_is_no_imports_based_fan_in_at_all(monkeypatch):
    """A repo whose every relation came only from CALLS|USAGE has no trustworthy ranking signal —
    the honest behaviour is to flag nothing, not to fall back to the contaminated union."""
    _wire(monkeypatch, file_rows=_rows(["a.py", "b.py", "c.py", "target.py"]),
         calls_rows=[{"a.file_path": src, "b.file_path": "target.py", "count(*)": 1}
                    for src in ("a.py", "b.py", "c.py")])
    payload = c4.build_c4_payload("/repo")
    assert payload["stats"]["hotspot_threshold"] is None
    assert payload["stats"]["hotspot_count"] == 0
    assert all(not e["hotspot"] for e in payload["elements"])


def test_hotspot_styling_is_a_single_consistent_red(monkeypatch):
    """The old override used the named theme colour `red`, a different hex than the tag's literal
    `#c0392b` — two visually different reds on one signal. The instance override must reference
    the SAME declared colour the tag uses."""
    files = ["a.py", "b.py", "c.py", "target.py"]
    imports = [{"a.file_path": src, "b.file_path": "target.py", "count(*)": 1}
              for src in ("a.py", "b.py", "c.py")]
    _wire(monkeypatch, file_rows=_rows(files), import_rows=imports)
    payload = c4.build_c4_payload("/repo")
    dsl = c4.render_c4_dsl(payload)
    assert "#hotspot" in dsl
    assert "style { color ci_bad }" in dsl
    assert "color red" not in dsl
    assert "color ci_bad" in c4.SPECIFICATION_BLOCK


# --------------------------------------------------------------------------- reserved words (end-to-end)

def test_a_reserved_word_filename_produces_a_valid_identifier(monkeypatch):
    """Defect (c): `src/model.py` used to render `model = module 'model' { … }` *inside* the
    model-scope keyword `model { }` — a LikeC4 1.59.2 parse error. The identifier must be escaped
    while the displayed title stays the real, unescaped name."""
    _wire(monkeypatch, file_rows=_rows(["src/model.py", "src/style.py"]))
    payload = c4.build_c4_payload("/repo", depth=2)
    ids = sorted(e["id"] for e in payload["elements"])
    assert ids == ["src.model_", "src.style_"]
    titles = sorted(e["title"] for e in payload["elements"])
    assert titles == ["model", "style"]        # the display title is never escaped

    dsl = c4.render_c4_dsl(payload)
    assert "model_ = module 'model'" in dsl
    assert "style_ = module 'style'" in dsl


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


# --------------------------------------------------------------------------- views: coarse landscape + drill-downs

def _branching_elements():
    """`scripts` branches immediately (2 children — a landscape root as-is). `src` is a bare
    single-child pass-through into `core`, which itself branches into 3 leaf modules, a 3-file
    `widgets` area and a 1-file `tiny` area — `src.core` should be the collapsed landscape root,
    `widgets` should get its own drill-down, and `tiny` (1 child) should not."""
    def _e(eid):
        return {"id": eid, "path": eid.replace(".", "/") + ".py", "title": eid.rsplit(".", 1)[-1],
               "kind": "module", "tech": "Python", "files": 1, "churn": 0, "fan_in": 0,
               "fan_out": 0, "internal_imports": 0}

    return [
        _e("scripts.one"), _e("scripts.two"),
        _e("src.core.m1"), _e("src.core.m2"), _e("src.core.m3"),
        _e("src.core.widgets.w1"), _e("src.core.widgets.w2"), _e("src.core.widgets.w3"),
        _e("src.core.tiny.t1"),
    ]


def test_the_landscape_view_is_coarse_not_include_star_with_extra_steps():
    """THE MAIN FIX. `include root, root.**` was `include *` with extra steps: every element, every
    relation, one `autoLayout`. The landscape must show each root and its DIRECT children only."""
    dsl = c4.render_c4_dsl(_bare_payload(elements=_branching_elements()))
    assert "include scripts, scripts.*" in dsl
    assert "include src.core, src.core.*" in dsl
    assert ".**" not in dsl                     # no full-depth include anywhere in the file
    assert "include src, src.*" not in dsl      # the bare `src` -> `core` pass-through is collapsed


def test_an_area_with_enough_hidden_children_gets_its_own_drilldown_view():
    dsl = c4.render_c4_dsl(_bare_payload(elements=_branching_elements()))
    assert "view of src.core.widgets {" in dsl
    view = dsl[dsl.index("view of src.core.widgets {"):]
    assert "include *" in view
    assert "autoLayout TopBottom" in view


def test_a_landscape_root_does_not_also_get_a_redundant_drilldown_view():
    """`src.core`'s direct children are already fully shown by the landscape's `include src.core,
    src.core.*` — a dedicated `view of src.core` would duplicate it."""
    dsl = c4.render_c4_dsl(_bare_payload(elements=_branching_elements()))
    assert "view of src.core {" not in dsl
    assert "view of scripts {" not in dsl


def test_a_one_child_area_is_too_small_to_deserve_its_own_view():
    dsl = c4.render_c4_dsl(_bare_payload(elements=_branching_elements()))
    assert "view of src.core.tiny {" not in dsl


def test_every_view_has_a_title_and_a_description():
    dsl = c4.render_c4_dsl(_bare_payload(elements=_branching_elements()))
    view_blocks = re.findall(r"  view (?:index|of \S+) \{\n(.*?)\n  \}\n", dsl, re.S)
    assert len(view_blocks) >= 2
    for block in view_blocks:
        assert "title '" in block
        assert "description '" in block


def test_a_view_over_the_cap_skips_every_view_not_just_the_landscape():
    payload = _bare_payload(elements=_branching_elements(),
                            fit={"depth": 1, "how": "requested", "table": {1: 140},
                                 "over_cap": True, "cap": 100})
    dsl = c4.render_c4_dsl(payload)
    assert "SKIPPED" in dsl
    assert "view index {" not in dsl
    assert "view of" not in dsl


# --------------------------------------------------------------------------- landscape elision for a FLAT root

def _flat_root_elements():
    """`big` IS the landscape root (no single-child chain above it) and has 15 direct children —
    13 flat leaf modules plus 2 branching sub-areas — well past `LANDSCAPE_CHILD_BUDGET`. This is
    this generator's own repo's shape: `src.codeintel` collapsed to 41 direct children (38 flat
    modules + 3 areas), which the OLD landscape logic dumped straight into `include big, big.*`
    with nothing to hide behind — coarser than the original hairball, but still one."""
    def _e(eid):
        return {"id": eid, "path": eid.replace(".", "/") + ".py", "title": eid.rsplit(".", 1)[-1],
               "kind": "module", "tech": "Python", "files": 1, "churn": 0, "fan_in": 0,
               "fan_out": 0, "internal_imports": 0}

    elements = [_e(f"big.m{i}") for i in range(13)]
    elements += [_e("big.sub1.x1"), _e("big.sub1.x2"), _e("big.sub2.y1"), _e("big.sub2.y2")]
    return elements


def test_a_flat_landscape_root_elides_its_leaf_modules_from_the_landscape():
    """THE FOLLOW-UP FIX. Depth-collapsing alone does not help a root with no subdirectories to
    collapse THROUGH — a landscape root that is itself flat and broad must still be cut down."""
    dsl = c4.render_c4_dsl(_bare_payload(elements=_flat_root_elements()))
    view = re.search(r"  view index \{\n(.*?)\n  \}\n", dsl, re.S).group(1)
    assert "include big\n" in view
    for i in range(13):
        assert f"include big.m{i}\n" not in view
    assert "include big.sub1\n" in view
    assert "include big.sub2\n" in view
    assert "include big, big.*" not in dsl


def test_an_elided_landscape_root_gets_its_own_drilldown_view():
    """Being the landscape root does not disqualify an area from having a view — for an elided
    root it is the reason one is required: `view of big { include * }` is the only place its 13
    flat modules are shown at all."""
    dsl = c4.render_c4_dsl(_bare_payload(elements=_flat_root_elements()))
    assert "view of big {" in dsl
    view = dsl[dsl.index("view of big {"):]
    assert "include *" in view


def test_a_landscape_root_under_the_child_budget_is_not_elided():
    """`scripts` (2 children) and `src.core` (5 children) in `_branching_elements` both stay well
    under `LANDSCAPE_CHILD_BUDGET` — the elision path must not fire for an ordinary small repo."""
    dsl = c4.render_c4_dsl(_bare_payload(elements=_branching_elements()))
    assert "include scripts, scripts.*" in dsl
    assert "include src.core, src.core.*" in dsl
    assert "view of scripts {" not in dsl
    assert "view of src.core {" not in dsl


# --------------------------------------------------------------------------- bare area titles (defect (i))

def test_a_bare_synthetic_area_keeps_the_real_unsanitised_directory_name(monkeypatch):
    """`my-app` never materialises as its own group (both its children are two directories deeper
    than the chosen depth), so it only exists as a bare containment `area` `_emit_tree` synthesises
    — that area's title must still read `my-app`, not the sanitised tree-node key `my_app`."""
    _wire(monkeypatch, file_rows=_rows(["my-app/sub1/leaf.py", "my-app/sub2/leaf.py"]))
    payload = c4.build_c4_payload("/repo", depth=2)
    dsl = c4.render_c4_dsl(payload)
    assert "my_app = area 'my-app' {" in dsl
    assert "area 'my_app'" not in dsl


# --------------------------------------------------------------------------- --edges

def _two_kinds(monkeypatch):
    """A repo where one pair is IMPORTS-confirmed and another is CALLS|USAGE-only."""
    _wire(monkeypatch,
          file_rows=_rows(["src/a.py", "src/b.py", "src/c.py"]),
          import_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 2}],
          calls_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/c.py", "count(*)": 7}])


def test_the_union_is_the_default_and_keeps_both_edge_kinds(monkeypatch):
    """Higher recall, so it stays the default: a lower-recall default would silently hide real
    dependencies from anyone who never read the flag."""
    _two_kinds(monkeypatch)
    payload = c4.build_c4_payload("/repo", depth=4)
    kinds = sorted(r["kind"] for r in payload["relations"])
    assert kinds == ["calls_usage", "imports"]
    assert payload["edge_source"] == "union"
    assert payload["stats"]["edges_excluded_by_filter"] == 0


def test_edges_imports_drops_the_calls_usage_only_relations(monkeypatch):
    _two_kinds(monkeypatch)
    payload = c4.build_c4_payload("/repo", depth=4, edges="imports")
    assert [r["kind"] for r in payload["relations"]] == ["imports"]
    assert payload["edge_source"] == "imports"
    assert payload["stats"]["edges_excluded_by_filter"] == 1


def test_an_unrecognised_edge_source_falls_back_to_the_union(monkeypatch):
    """Never-raise: a bad value degrades to the higher-recall default rather than emitting an empty
    or arbitrarily-filtered model."""
    _two_kinds(monkeypatch)
    payload = c4.build_c4_payload("/repo", depth=4, edges="nonsense")
    assert payload["edge_source"] == "union"
    assert len(payload["relations"]) == 2


def test_derived_counts_describe_the_filtered_model_not_the_index(monkeypatch):
    """The filter runs BEFORE fan_in/fan_out, so every number in the file agrees with the edges the
    file contains. Filtering in the renderer instead would leave `fan_in` and the stats describing
    relations the diagram does not draw."""
    _two_kinds(monkeypatch)
    union = c4.build_c4_payload("/repo", depth=4)
    imports = c4.build_c4_payload("/repo", depth=4, edges="imports")

    def fan_out_of(payload, name):
        return next(e["fan_out"] for e in payload["elements"] if e["id"].endswith(name))

    assert fan_out_of(union, "a") == 2        # b via IMPORTS, c via CALLS|USAGE
    assert fan_out_of(imports, "a") == 1      # only b survives
    assert union["stats"]["edges_kept"] == 2
    assert imports["stats"]["edges_kept"] == 1


def test_provenance_counts_survive_the_filter(monkeypatch):
    """They are computed before it, on purpose: what the INDEX found is worth keeping even when the
    model deliberately shows less."""
    _two_kinds(monkeypatch)
    payload = c4.build_c4_payload("/repo", depth=4, edges="imports")
    assert payload["stats"]["edges_from_calls_usage_only"] == 1
    assert payload["stats"]["edges_from_imports_only"] == 1


def test_the_header_never_claims_the_union_after_filtering_it_out(monkeypatch):
    """The header exists to stop the file misrepresenting itself, so it is the one thing that must
    follow the flag."""
    _two_kinds(monkeypatch)
    dsl = c4.render_c4_dsl(c4.build_c4_payload("/repo", depth=4, edges="imports"))
    assert "// edges: IMPORTS ONLY" in dsl
    assert "union of IMPORTS" not in dsl
    assert "coverage is LOWER than the default" in dsl
    assert "1 CALLS|USAGE-only relation(s) excluded by that flag" in dsl


def test_the_header_does_not_contradict_itself_about_dashed_edges(monkeypatch):
    """A first version reported the CALLS|USAGE count with "(dashed `calls_usage` edges below)" one
    line after reporting the filtered relation total — so the header claimed 45 relations and 134
    dashed edges in the same block. Nothing catches a header contradicting itself except reading it.
    """
    _two_kinds(monkeypatch)
    dsl = c4.render_c4_dsl(c4.build_c4_payload("/repo", depth=4, edges="imports"))
    assert "dashed `calls_usage` edges below" not in dsl
    assert "NOT in this file" in dsl
    # And the union still says it, because there they ARE below.
    union_dsl = c4.render_c4_dsl(c4.build_c4_payload("/repo", depth=4))
    assert "dashed `calls_usage` edges below" in union_dsl


def test_the_view_description_follows_the_edge_source(monkeypatch):
    """A view's description is the only provenance that travels with an exported image."""
    _two_kinds(monkeypatch)
    imports_dsl = c4.render_c4_dsl(c4.build_c4_payload("/repo", depth=4, edges="imports"))
    assert "static module-level IMPORTS only" in imports_dsl
    assert "edges are the union" not in imports_dsl

    union_dsl = c4.render_c4_dsl(c4.build_c4_payload("/repo", depth=4))
    assert "edges are the union" in union_dsl


# --------------------------------------------------------------------------- per-source weights

def _both_sources(monkeypatch):
    """One pair confirmed by BOTH queries — 1 import reference and 200 CALLS|USAGE references.

    The shape that made the bug visible on a real repo: brightsky-ai had exactly one (fabricated)
    import edge from `backend/src` into `frontend/src` and 247 CALLS|USAGE references between the
    same two directories, so the edge was labelled `imports` and emitted with `n '248'`.
    """
    _wire(monkeypatch,
          file_rows=_rows(["src/a.py", "src/b.py"]),
          import_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 1}],
          calls_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/b.py", "count(*)": 200}])


def test_edges_imports_reports_the_import_weight_not_the_union_weight(monkeypatch):
    """The bug this fixes. `--edges imports` promises import evidence only, and a weight summed
    across both sources describes references the reader cannot see — one real import statement was
    emitted as 248."""
    _both_sources(monkeypatch)
    rel = c4.build_c4_payload("/repo", depth=4, edges="imports")["relations"][0]
    assert rel["kind"] == "imports"
    assert rel["n"] == 1


def test_the_union_still_reports_the_combined_weight(monkeypatch):
    """The negative control: fixing the filtered case must not quietly change the default. There the
    weight IS the union weight, and both sources are in the model."""
    _both_sources(monkeypatch)
    rel = c4.build_c4_payload("/repo", depth=4)["relations"][0]
    assert rel["kind"] == "imports"
    assert rel["n"] == 201


def test_every_relation_carries_the_split_so_a_label_can_be_weighed(monkeypatch):
    """The most useful thing to know about an edge labelled `imports` is whether that label rests on
    one import statement or on hundreds. A single total cannot say."""
    _both_sources(monkeypatch)
    for edges in ("union", "imports"):
        rel = c4.build_c4_payload("/repo", depth=4, edges=edges)["relations"][0]
        assert rel["n_imports"] == 1, edges
        assert rel["n_calls_usage"] == 200, edges


def test_a_calls_usage_only_pair_keeps_its_weight_in_the_union(monkeypatch):
    """Only the emitted sources are counted, so a `calls_usage` edge must still carry its own weight
    when the union is what is being emitted."""
    _wire(monkeypatch,
          file_rows=_rows(["src/a.py", "src/c.py"]),
          calls_rows=[{"a.file_path": "src/a.py", "b.file_path": "src/c.py", "count(*)": 7}])
    rel = c4.build_c4_payload("/repo", depth=4)["relations"][0]
    assert rel["kind"] == "calls_usage"
    assert rel["n"] == 7
    assert rel["n_imports"] == 0


def test_weights_from_several_file_pairs_still_sum_into_one_element_edge(monkeypatch):
    """Per-source accounting must not lose the roll-up: two files in one element importing two files
    in another is one element edge carrying both weights."""
    _wire(monkeypatch,
          file_rows=_rows(["src/pkg/a.py", "src/pkg/b.py", "src/lib/x.py", "src/lib/y.py"]),
          import_rows=[
              {"a.file_path": "src/pkg/a.py", "b.file_path": "src/lib/x.py", "count(*)": 3},
              {"a.file_path": "src/pkg/b.py", "b.file_path": "src/lib/y.py", "count(*)": 4},
          ])
    rel = next(r for r in c4.build_c4_payload("/repo", depth=2, edges="imports")["relations"]
               if r["from"].endswith("pkg"))
    assert rel["n"] == 7
    assert rel["n_imports"] == 7
