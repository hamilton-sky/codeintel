"""Tests for the pure `codeintel.c4` functions — `keep_source`, `fit_depth`, `element_key`, and
the shadow-merge / collision-suffix rules that decide element identity. No backend involved: every
input here is a plain list of path strings, per this module's documented contract.
"""
from __future__ import annotations

from codeintel import c4


def test_noise_directories_are_dropped_at_any_path_depth():
    """`packages/api/node_modules/x.js` is dropped, not just top-level `node_modules/`."""
    paths = ["packages/api/node_modules/x.js", "node_modules/y.js", "packages/api/src/z.js"]
    kept = c4.keep_source(paths)
    assert kept == ["packages/api/src/z.js"]


def test_a_bin_directory_keeps_its_cli_entry_points_but_not_compiled_output():
    """`bin/` is where an npm package publishes CLI entry points (`package.json#bin`), not only
    where compilers drop output. Treating it as noise deleted token-tracker's three entry points
    (`src/bin/lum.ts`, `guard.js`, `statusline.js`) — its most load-bearing files.

    Nothing is lost by keeping it: compiled artifacts carry non-source extensions, so the SRC_EXT
    allowlist already rejects them before any directory check runs.
    """
    kept = c4.keep_source([
        "src/bin/lum.ts", "src/bin/guard.js", "bin/cli.js",   # entry points — source
        "bin/Debug/net8.0/App.dll", "bin/App.class",          # compiled — non-source extension
        "bin/mytool",                                         # extensionless Go binary
        "dist/bundle.js",                                     # still noise: dist IS build output
    ])
    assert kept == ["src/bin/lum.ts", "src/bin/guard.js", "bin/cli.js"]


def test_test_directories_are_excluded_by_default_and_restorable():
    """token-tracker's 35 test files do not outvote its 19 source files."""
    src = [f"src/mod_{i}.ts" for i in range(19)]
    tests = [f"tests/mod_{i}.spec.ts" for i in range(35)]
    paths = src + tests
    default = c4.keep_source(paths)
    assert len(default) == 19
    assert all("tests/" not in p for p in default)

    restored = c4.keep_source(paths, include_tests=True)
    assert len(restored) == 19 + 35


def test_generated_declaration_files_are_not_architecture():
    paths = ["src/a.ts", "src/a.d.ts", "dist/bundle.min.js", "src/b.js"]
    kept = c4.keep_source(paths)
    assert kept == ["src/a.ts", "src/b.js"]


def test_fit_depth_picks_the_deepest_depth_under_the_cap():
    # 6 files at d1, growing to 11/15/22/125 raw GROUPS as depth increases — engineered so the
    # design's worked example's group counts still match exactly.
    paths = []
    # depth 1: 6 top areas
    paths.extend(f"area{i}/leaf.py" for i in range(6))
    # deepen area0 progressively so each depth adds distinct elements without changing the d1 count
    paths.extend(f"area0/sub{i}/leaf.py" for i in range(5))
    paths.extend(f"area0/sub0/mid{i}/leaf.py" for i in range(4))
    paths.extend(f"area0/sub0/mid0/deep{i}/leaf.py" for i in range(7))
    paths.extend(f"area0/sub0/mid0/deep0/n{i}/leaf.py" for i in range(103))

    fit = c4.fit_depth(paths, cap=100)
    table = fit["table"]
    # `table` counts what actually RENDERS (defect (h)): the raw groups above (6, 11, 15, 22, 125)
    # plus every bare synthetic containment `area` `_emit_tree` has to synthesise for a shared dot
    # prefix that is not itself a group — e.g. at d2, `area0.leaf` and `area0.sub0` share `area0`,
    # which is not a group in its own right but still costs one rendered node.
    assert table[1] == 6
    assert table[2] == 17
    assert table[3] == 26
    assert table[4] == 37
    assert table[5] == 147
    assert fit["depth"] == 4
    assert fit["how"] == "auto-fit"
    assert fit["over_cap"] is False


def test_fit_depth_reports_the_whole_table_not_just_the_pick():
    paths = [f"pkg{i}/mod{i}/leaf{i}.py" for i in range(3)]
    fit = c4.fit_depth(paths, cap=100)
    # every candidate depth up to the longest path is present, not merely the chosen one
    assert set(fit["table"]) == {1, 2, 3}


def test_fit_depth_emits_depth_one_over_cap_rather_than_nothing():
    paths = [f"dir{i}/leaf.py" for i in range(500)]   # 500 distinct top-level dirs > any small cap
    fit = c4.fit_depth(paths, cap=10)
    assert fit["depth"] == 1
    assert fit["over_cap"] is True
    assert fit["how"] == "auto-fit"


def test_a_requested_depth_wins_and_is_labelled_requested():
    paths = [f"a/b/c/{i}/leaf.py" for i in range(50)]
    fit = c4.fit_depth(paths, cap=100, requested=2)
    assert fit["depth"] == 2
    assert fit["how"] == "requested"
    # auto-fit would have picked something deeper — confirm requested overrides it, not merely
    # coincides with it
    auto = c4.fit_depth(paths, cap=100)
    assert auto["depth"] != 2


def test_element_key_returns_the_file_when_the_path_is_shorter_than_depth():
    assert c4.element_key("main.py", 4) == "main"
    assert c4.element_key("a/b/c/d.py", 3) == "a.b.c"


def test_a_file_shadowing_a_sibling_directory_merges_into_it():
    paths = ["api.ts", "api/routes.ts", "api/handlers.ts"]
    depth = 1
    keys = {p: c4.element_key(p, depth) for p in paths}
    assert keys["api.ts"] == keys["api/routes.ts"] == keys["api/handlers.ts"] == "api"

    grouped = c4.group_elements(paths, depth)
    assert grouped["shadowed_files"] == 1
    assert len(grouped["groups"]) == 1
    (group,) = grouped["groups"].values()
    assert len(group["paths"]) == 3
    assert grouped["element_of"]["api.ts"] == "api"


def test_sanitisation_collisions_are_suffixed_not_merged():
    paths = ["foo-bar/a.py", "foo_bar/b.py"]
    grouped = c4.group_elements(paths, 1)
    ids = sorted(grouped["groups"])
    assert ids == ["foo_bar", "foo_bar__2"]
    assert grouped["id_collisions"] == 1


def test_a_collision_deeper_in_the_tree_only_suffixes_the_colliding_segment():
    """Defect (e): the old rule suffixed the whole dotted id (`my_app.core` /
    `my_app.core__2`), which falsely claimed both files live under one shared `my_app` area and
    left nothing representing `my-app` in the tree at all. Only the segment that actually
    collided (`my-app` vs `my_app`) should be disambiguated — `core`, which never collided, must
    read identically under both."""
    paths = ["my-app/core/a.ts", "my_app/core/b.ts"]
    grouped = c4.group_elements(paths, 2)
    ids = sorted(grouped["groups"])
    assert ids == ["my_app.core", "my_app__2.core"]
    assert grouped["id_collisions"] == 1
    # the uncontested segment is untouched in both branches
    assert all(eid.endswith(".core") for eid in ids)
    # the real (unsanitised) directory names survive for display, keyed by the ambiguous
    # segment's OWN resolved id path, not by the final leaf id
    assert grouped["segment_titles"]["my_app"] == "my-app"
    assert grouped["segment_titles"]["my_app__2"] == "my_app"


def test_reserved_words_are_escaped_but_the_display_title_is_untouched():
    """LikeC4 1.59.2 reserves `model`/`style`/`this` (and others) at identifier position — a file
    literally named `model.ts` broke the parser before this escape existed (defect (c))."""
    assert c4._ident("model") == "model_"
    assert c4._ident("style") == "style_"
    assert c4._ident("this") == "this_"
    # a non-reserved word is untouched
    assert c4._ident("cache") == "cache"
    # case-insensitive: `Model` still collides with the keyword once lower-cased by a parser
    assert c4._ident("Model") == "Model_"

    grouped = c4.group_elements(["src/model.py", "src/style.py"], 2)
    assert sorted(grouped["groups"]) == ["src.model_", "src.style_"]


def test_hotspot_threshold_is_relative_to_the_populations_own_spread():
    # a fixed `>= 5` cutoff would tag half of a population that tops out at 4
    assert c4._hotspot_threshold([1, 2, 3, 4]) is None or c4._hotspot_threshold([1, 2, 3, 4]) >= 3
    assert c4._hotspot_threshold([]) is None
    # floored so a population that never exceeds 1-2 does not get flagged just because it is the
    # top of its own (trivial) distribution
    assert c4._hotspot_threshold([1, 1, 1, 2]) >= c4.MIN_HOTSPOT_FAN_IN
    # the busiest of a spread-out population clears the floor and is at/above the percentile cut
    values = list(range(1, 21))            # 1..20
    threshold = c4._hotspot_threshold(values)
    assert threshold is not None
    flagged = [v for v in values if v >= threshold]
    assert 0 < len(flagged) < len(values)  # a real minority, not everything and not nothing
