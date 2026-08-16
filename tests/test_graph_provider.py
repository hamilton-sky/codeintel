"""GraphProvider tests: never-raise invariant and key behavioral guarantees."""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from codeintel.providers.graph import GraphProvider
from codeintel.server import code_status_handler


def test_match_project_resolves_a_relative_path(tmp_path, monkeypatch):
    # The backend stores absolute root_paths, so a relative project_root (e.g. `codeintel map .`)
    # must be normalized before matching — otherwise resolution fails from inside the repo and the
    # map/graph query silently reports "not indexed" (the map-stub bug).
    real = os.path.realpath(str(tmp_path))
    raw = {"projects": [{"name": "myrepo", "root_path": real}]}
    monkeypatch.chdir(tmp_path)
    assert GraphProvider._match_project(raw, ".") == "myrepo"      # relative resolves now
    assert GraphProvider._match_project(raw, real) == "myrepo"     # absolute still works
    assert GraphProvider._match_project(raw, os.path.join(real, "src")) == "myrepo"  # subdir prefix


# ---------------------------------------------------------------------------
# Group 1 — Never-raise: None args
# ---------------------------------------------------------------------------

def test_graph_provider_none_args():
    p = GraphProvider()
    r = p.build_result(None, None, None, None, None)
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 2 — Never-raise: wrong types
# ---------------------------------------------------------------------------

def test_graph_provider_wrong_types():
    p = GraphProvider()
    r = p.build_result(123, [], {}, "bad", object())
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 3 — Backend unavailable: safe-null with reason
# ---------------------------------------------------------------------------

def test_graph_provider_unavailable(monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    p = GraphProvider()
    assert p.available is False
    r = p.build_result("symbol", "x", [], 0, "")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "engine-unavailable"


# ---------------------------------------------------------------------------
# Group 4 — Project not indexed: safe-null with reason
# ---------------------------------------------------------------------------

def test_graph_provider_project_not_indexed(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: [])
    r = p.build_result("impact", "fn", [], 0, "/my/repo")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "project-not-indexed"


# ---------------------------------------------------------------------------
# Group 5 — Subprocess issues
# ---------------------------------------------------------------------------

def test_graph_provider_subprocess_timeout(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()

    def _raise_timeout(method, payload, timeout_ms):
        raise subprocess.TimeoutExpired(cmd="codebase-memory-mcp", timeout=5)

    monkeypatch.setattr(p, "_run", _raise_timeout)
    r = p.build_result("impact", "fn", [], 0, "/repo")
    assert r["ok"] is True


def test_graph_provider_subprocess_crash(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: None)
    r = p.build_result("impact", "fn", [], 0, "/repo")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Group 6 — Unknown op
# ---------------------------------------------------------------------------

def test_graph_provider_unknown_op(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    p = GraphProvider()
    p._project_cache[""] = "myproject"
    r = p.build_result("nonexistent-op", "x", [], 0, "")
    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "unsupported-op"


# ---------------------------------------------------------------------------
# Group 7 — engine field when available
# ---------------------------------------------------------------------------

def test_graph_provider_engine_field(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )

    def _fake_run(method, payload, timeout_ms):
        if method == "list_projects":
            return {"projects": [{"root_path": "/repo", "name": "myproj"}]}
        if method == "query_graph":
            # Real backend shape: value-arrays aligned to columns (not a list of dicts).
            return {
                "columns": ["a.name", "a.qualified_name", "a.file_path", "type(c)"],
                "rows": [["bar", "pkg.bar", "bar.py", "CALLS"]],
                "total": 1,
            }
        return None

    p = GraphProvider()
    monkeypatch.setattr(p, "_run", _fake_run)
    r = p.build_result("callers", "x", [], 0, "/repo")
    assert r["ok"] is True
    assert r["engine"] == "graph"
    # The real-shape row must actually be parsed into the rendered result.
    assert r["result"] is not None
    assert "pkg.bar" in r["result"]


# ---------------------------------------------------------------------------
# Group 8 — Server status reflects graph availability
# ---------------------------------------------------------------------------

def test_code_status_with_graph(monkeypatch):
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which",
        lambda x: "/fake/codebase-memory-mcp",
    )
    r = code_status_handler({})
    assert r["ok"] is True
    assert "graph" in r["engines"]


def test_code_status_without_graph(monkeypatch):
    monkeypatch.setattr("codeintel.providers.graph.shutil.which", lambda x: None)
    r = code_status_handler({})
    assert "graph" not in r["engines"]


# --------------------------------------------------------------------------- duplicate projects

def _projects(*entries) -> dict:
    return {"projects": [dict(e) for e in entries]}


def test_match_project_prefers_the_most_complete_of_duplicate_registrations(tmp_path):
    """The backend can hold two projects for ONE root — a short name and a path slug — that drift
    apart independently. Taking the first match answered queries from a stale index while a
    complete one sat beside it: observed as 1475 nodes vs 2631 for this repo, which is how
    `callers` reported a function's pre-refactor shape hours after the refactor landed.
    """
    root = str(tmp_path)
    raw = _projects(
        {"name": "stale-short-name", "root_path": root, "nodes": 1475, "edges": 2809},
        {"name": "fresh-path-slug", "root_path": root, "nodes": 2631, "edges": 8303},
    )
    assert GraphProvider._match_project(raw, root) == "fresh-path-slug"

    # ...and independently of the order the backend happens to list them in.
    reversed_raw = {"projects": list(reversed(raw["projects"]))}
    assert GraphProvider._match_project(reversed_raw, root) == "fresh-path-slug"


def test_match_project_keeps_the_first_listed_when_there_is_nothing_to_choose_between(tmp_path):
    """With no completeness signal — equal counts, or a backend that omits `nodes` entirely — fall
    back to the original first-listed rule rather than inventing an ordering."""
    root = str(tmp_path)
    raw = _projects(
        {"name": "aaa", "root_path": root, "nodes": 10},
        {"name": "zzz", "root_path": root, "nodes": 10},
    )
    assert GraphProvider._match_project(raw, root) == "aaa"
    assert GraphProvider._match_project({"projects": list(reversed(raw["projects"]))}, root) == "zzz"


def test_match_project_survives_a_missing_or_junk_node_count(tmp_path):
    """`nodes` comes from an untrusted backend payload; a missing or non-numeric one must not
    take out project resolution entirely."""
    root = str(tmp_path)
    raw = _projects(
        {"name": "no-count", "root_path": root},
        {"name": "junk-count", "root_path": root, "nodes": "lots"},
        {"name": "real-count", "root_path": root, "nodes": 5},
    )
    assert GraphProvider._match_project(raw, root) == "real-count"


def test_match_project_still_prefers_exact_over_a_richer_parent(tmp_path):
    """Completeness only breaks ties among EXACT matches — a huge parent-directory index must
    never outrank the repo's own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = _projects(
        {"name": "parent", "root_path": str(tmp_path), "nodes": 999_999},
        {"name": "repo", "root_path": str(repo), "nodes": 12},
    )
    assert GraphProvider._match_project(raw, str(repo)) == "repo"


# --------------------------------------------------------------------------- failure reasons

def test_every_dispatched_op_is_declared_supported():
    """`_GRAPH_OPS` gates the unsupported-op reply, so an op wired into _dispatch but missing from
    the set would be reported unsupported while being perfectly implemented."""
    import inspect

    from codeintel.providers.graph import _GRAPH_OPS

    source = inspect.getsource(GraphProvider._dispatch)
    dispatched = set(re.findall(r'op == "([a-z]+)"', source))
    assert dispatched == set(_GRAPH_OPS), f"drift: {dispatched ^ set(_GRAPH_OPS)}"


def _provider_answering(dispatch_result):
    """A GraphProvider with the backend seam stubbed: available, resolving to a project, and
    dispatching to a fixed result — so the reason-mapping is tested, not the subprocess."""
    gp = GraphProvider.__new__(GraphProvider)
    gp.available = True                                            # type: ignore[attr-defined]
    gp._resolve_project = lambda root: "proj"                      # type: ignore[method-assign]
    gp._dispatch = lambda *a, **k: dispatch_result                 # type: ignore[method-assign]
    return gp


def test_a_supported_op_that_finds_nothing_is_not_reported_unsupported(tmp_path):
    """The most misleading string the never-raise envelope produced: `callers` — a documented,
    implemented op — came back `unsupported-op` whenever the symbol was absent, which sends an
    agent hunting for another tool when the real fix is `codeintel index`.
    """
    res = _provider_answering(None).build_result(
        "callers", "a_symbol_nobody_added_yet", None, 1000, str(tmp_path))

    assert res["result"] is None
    assert res["reason"] == "not-in-graph"
    assert "codeintel index" in res["hint"]
    assert "a_symbol_nobody_added_yet" in res["hint"]


def test_a_genuinely_unknown_op_is_still_reported_unsupported(tmp_path):
    res = _provider_answering(None).build_result("frobnicate", "x", None, 1000, str(tmp_path))
    assert res["reason"] == "unsupported-op"


def test_overview_titles_with_the_repo_name_not_the_backend_project_id(tmp_path, monkeypatch):
    """The backend's project id is often a flattened absolute path
    (`Users-alice-Documents-project-myrepo`). This heading lands in CODE_INTEL.md, which gets
    committed and pushed — so an internal id there publishes the author's home directory layout.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    gp = GraphProvider.__new__(GraphProvider)
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "Users-alice-Documents-project-myrepo", "total_nodes": 5, "total_edges": 4})

    out = gp._op_overview("", "Users-alice-Documents-project-myrepo", 1000, str(repo))
    assert out.splitlines()[0] == "## Architecture: myrepo"
    assert "Users-alice" not in out


def test_overview_resolves_a_relative_root_before_naming_it(tmp_path, monkeypatch):
    """`codeintel map .` passes "." — whose basename is "." — and that titled the committed file
    with a dot."""
    gp = GraphProvider.__new__(GraphProvider)
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "backend-id", "total_nodes": 5, "total_edges": 4})
    monkeypatch.chdir(tmp_path)

    out = gp._op_overview("", "backend-id", 1000, ".")
    assert out.splitlines()[0] == f"## Architecture: {tmp_path.resolve().name}"


def test_overview_falls_back_to_the_backend_name_without_a_root(monkeypatch):
    gp = GraphProvider.__new__(GraphProvider)
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "backend-id", "total_nodes": 5, "total_edges": 4})

    assert gp._op_overview("", "backend-id", 1000, "").splitlines()[0] == "## Architecture: backend-id"


def test_result_lines_do_not_carry_the_backends_project_id(tmp_path):
    """The backend prefixes every qualified name with its project id — for a path-slug
    registration, the flattened ABSOLUTE PATH. Every result line therefore began
    `Users-alice-Documents-project-myrepo.src.pkg.fn`: the author's home directory repeated per
    row, on results that run to a hundred lines. Noise for a human, wasted tokens for the agent
    this tool exists to serve."""
    from codeintel.providers.graph import _strip_project_prefix

    assert _strip_project_prefix(
        "Users-alice-Documents-project-myrepo.src.pkg.fn") == "src.pkg.fn"
    assert _strip_project_prefix("my-repo.src.pkg.fn") == "src.pkg.fn"


def test_stripping_never_eats_a_real_module_path():
    """A hyphen cannot appear in a Python package name, which is what makes the slug detectable —
    so a genuine dotted module path must pass through untouched."""
    from codeintel.providers.graph import _strip_project_prefix

    for qn in ("src.codeintel.gateway.query", "codeintel.server.run", "main", "", "pkg.mod"):
        assert _strip_project_prefix(qn) == qn


def test_every_renderer_strips_the_project_prefix_not_just_one():
    """The first fix touched `_display` (callers/callees) and missed `_render_scan`, which renders
    hotspots/deadcode — so the noisiest, longest results kept the full home path on every row.
    Found by pointing the tool at a repo neither the author nor the reviewers had seen."""
    import inspect
    import re

    from codeintel.providers.graph import GraphProvider

    for fn in (GraphProvider._display, GraphProvider._render_scan):
        source = inspect.getsource(fn)
        # Any qualified_name read must pass through the stripper before becoming a label.
        for line in source.splitlines():
            if re.search(r"qualified_name|qn_key", line) and "=" in line and "def " not in line:
                assert "_strip_project_prefix" in line, (
                    f"{fn.__name__} renders a raw qualified name: {line.strip()}")


# --------------------------------------------------------------------------- scan accuracy

def test_deadcode_drops_candidates_that_are_referenced_in_source(tmp_path):
    """`deadcode` asks the graph for functions with IN-DEGREE 0, and a function passed as a
    REFERENCE rather than called has in-degree 0 — every React handler, every
    `addEventListener('keydown', onKeyDown)`, every framework callback. On a real TypeScript repo
    that made 181 of 181 sampled candidates false, and an agent acting on it would delete live
    code. The graph cannot see those edges, so verify against the source.
    """
    from codeintel.providers.graph import _drop_referenced_symbols

    (tmp_path / "hook.ts").write_text(
        "function onKeyDown(e) { return e }\n"
        "document.addEventListener('keydown', onKeyDown)\n"          # referenced, not called
    )
    (tmp_path / "orphan.ts").write_text("function reallyUnused() { return 1 }\n")

    rows = [{"name": "onKeyDown"}, {"name": "reallyUnused"}]
    kept, state = _drop_referenced_symbols(rows, str(tmp_path))

    assert state == "ok"
    assert [r["name"] for r in kept] == ["reallyUnused"]


def test_deadcode_verification_reports_when_it_could_not_run(tmp_path, monkeypatch):
    """Without the source pass these are raw in-degree-0 rows. Returning them unlabelled would
    imply a confidence the check never earned."""
    import codeintel.providers.graph as g

    monkeypatch.setattr(g, "_VERIFY_FILE_CAP", 0)
    (tmp_path / "a.py").write_text("def f(): pass\n")

    kept, state = g._drop_referenced_symbols([{"name": "f"}], str(tmp_path))
    assert state == "capped"
    assert len(kept) == 1, "an unverifiable repo must return rows unfiltered, not empty"


def test_deadcode_verification_is_word_boundary_accurate(tmp_path):
    """A substring match would hide a genuinely dead `run` behind any `runtime` in the repo."""
    from codeintel.providers.graph import _drop_referenced_symbols

    (tmp_path / "a.ts").write_text("function run() {}\nconst runtime = 1\nconst rerun = 2\n")
    kept, state = _drop_referenced_symbols([{"name": "run"}], str(tmp_path))

    assert state == "ok"
    assert [r["name"] for r in kept] == ["run"], "substring hits must not count as references"


def test_scan_ops_hide_archived_code(tmp_path):
    """A repo-scan ranks by complexity and fan-in, and archived code scores well on both: an 8MB
    `.archive/` tree put a retired 507-line component THIRD in a repo's refactor hotspots, a
    near-duplicate of the live one. Pointing an agent at dead code as the thing most worth
    refactoring is worse than returning nothing."""
    from codeintel.providers.graph import GraphProvider

    assert GraphProvider._is_noise({"file_path": "pathly/features/.archive/x/Old.tsx"}) is True
    assert GraphProvider._is_noise({"file_path": "studio/src/components/Editor/index.tsx"}) is False
    # .github holds live workflows, not archives.
    assert GraphProvider._is_noise({"file_path": ".github/workflows/ci.yml"}) is False


# --------------------------------------------------------------------------- verification limits

def test_two_dead_functions_sharing_a_name_do_not_hide_each_other(tmp_path):
    """A GLOBAL occurrence count against a fixed allowance of 1 meant each definition counted as
    the other's "use" and both vanished. The allowance must be the number of definitions."""
    from codeintel.providers.graph import _drop_referenced_symbols

    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    (tmp_path / "a" / "mod.py").write_text("def helper(): pass\n")
    (tmp_path / "b" / "mod.py").write_text("def helper(): pass\n")

    rows = [{"name": "helper", "file_path": "a/mod.py"}, {"name": "helper", "file_path": "b/mod.py"}]
    kept, state = _drop_referenced_symbols(rows, str(tmp_path))

    assert state == "ok"
    assert len(kept) == 2, "same-named dead functions cancelled each other out"


def test_generated_bundles_do_not_count_as_references(tmp_path):
    """A 6.7MB minified `out/` bundle supplied 46 occurrences of a `toJSON` that appears zero
    times in hand-written source — enough to hide a genuinely dead function."""
    from codeintel.providers.graph import _drop_referenced_symbols

    (tmp_path / "out").mkdir(); (tmp_path / "node_modules").mkdir()
    (tmp_path / "out" / "bundle.js").write_text("toJSON toJSON toJSON\n")
    (tmp_path / "node_modules" / "dep.js").write_text("toJSON\n")
    (tmp_path / "live.py").write_text("def toJSON(): pass\n")

    kept, state = _drop_referenced_symbols([{"name": "toJSON"}], str(tmp_path))
    assert state == "ok"
    assert [r["name"] for r in kept] == ["toJSON"]


def test_the_verifier_scans_live_dot_directories(tmp_path):
    """The walk skipped every dot-directory while `_is_archived_path` counted `.github` as live —
    so a CI helper referenced only from `.github/scripts` still read as dead."""
    from codeintel.providers.graph import _drop_referenced_symbols

    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / ".github" / "scripts" / "gen.py").write_text(
        "def build_matrix(): pass\nbuild_matrix()\n")

    kept, state = _drop_referenced_symbols([{"name": "build_matrix"}], str(tmp_path))
    assert state == "ok"
    assert kept == [], "a reference inside a live dot-directory was not seen"


def test_an_archived_directory_is_not_scanned_for_references(tmp_path):
    """Symmetry with `_is_archived_path`: retired code must not vouch for a symbol's liveness."""
    from codeintel.providers.graph import _drop_referenced_symbols

    (tmp_path / ".archive").mkdir()
    (tmp_path / ".archive" / "old.py").write_text("dead_thing()\ndead_thing()\n")
    (tmp_path / "live.py").write_text("def dead_thing(): pass\n")

    kept, _ = _drop_referenced_symbols([{"name": "dead_thing"}], str(tmp_path))
    assert [r["name"] for r in kept] == ["dead_thing"]


@pytest.mark.parametrize("root,expected", [("", "no-root"), ("/no/such/dir/anywhere", "no-root")])
def test_a_missing_project_root_is_reported_as_such_not_as_a_size_limit(root, expected):
    """The MCP tool defaults `project_root` to "", so this is the DEFAULT call path — and the one
    note blamed the file cap unconditionally, telling users their repo was too big when the real
    cause was a missing argument."""
    from codeintel.providers.graph import _drop_referenced_symbols

    assert _drop_referenced_symbols([{"name": "x"}], root)[1] == expected


def test_each_verification_outcome_has_its_own_note():
    from codeintel.providers.graph import _VERIFY_NOTES

    assert set(_VERIFY_NOTES) == {"ok", "no-root", "capped"}
    assert "cap" in _VERIFY_NOTES["capped"] and "cap" not in _VERIFY_NOTES["no-root"]
    assert "project_root" in _VERIFY_NOTES["no-root"]
    # The verified note must still disclose what a name scan cannot see.
    assert "getattr" in _VERIFY_NOTES["ok"] or "registr" in _VERIFY_NOTES["ok"]


@pytest.mark.parametrize("path,archived", [
    ("x/.archive/Old.tsx", True), ("app/.next/page.js", True), ("x/.backup/a.py", True),
    (".claude/hooks/on_stop.py", False), (".storybook/preview.ts", False),
    (".husky/lint.js", False), ("src/.internal/util.ts", False),
    (".github/workflows/ci.yml", False), (".eslintrc.js", False),
])
def test_only_named_archive_directories_are_hidden(path, archived):
    """Excluding EVERY dot-directory swept up live automation — `.claude/hooks`, `.storybook`,
    `.husky`, `.server`, `src/.internal`. An unknown dot-directory is source until proven retired."""
    from codeintel.providers.graph import _is_archived_path

    assert _is_archived_path(path) is archived


@pytest.mark.parametrize("label,expected", [
    ("A.EditorHeader.EditorHeader.EditorHeader", "A.EditorHeader"),
    ("CHANGELOG.1.1.0-—-2026-05-11", "CHANGELOG.1.1.0-—-2026-05-11"),
    ('__route__ANY__f"http://127.0.0.1:{}/x"', '__route__ANY__f"http://127.0.0.1:{}/x"'),
])
def test_collapsing_repeats_never_rewrites_a_number(label, expected):
    """Splitting on "." also splits version numbers and dotted quads, where consecutive equal
    parts are meaningful: `CHANGELOG.1.1.0` became `CHANGELOG.1.0` — a different real release."""
    from codeintel.providers.graph import _collapse_repeats

    assert _collapse_repeats(label) == expected


@pytest.mark.parametrize("path,excluded", [
    ("studio/out/renderer/assets/index-D4C.js", True),
    ("app/dist/bundle.js", True),
    ("vendor/lib/x.go", True),
    ("third_party/dep/mod.py", True),
    ("src/output/handler.py", False),      # "output" is not "out"
    ("src/components/Editor/index.tsx", False),
])
def test_generated_output_is_not_a_refactor_target(path, excluded):
    """A checked-in minified bundle took the top TWO hotspot slots on a real repo (cx:586,
    cog:1145) — a webpack chunk is by far the most "complex" function in any tree containing one.
    The first version excluded only dot-directories, so a plain `out/` or `dist/` sailed through."""
    from codeintel.providers.graph import _is_archived_path

    assert _is_archived_path(path) is excluded


def test_probe_discloses_when_the_index_belongs_to_a_containing_project(monkeypatch, tmp_path):
    """Resolution falls back to the nearest indexed ANCESTOR — right for a subdirectory of an
    indexed repo, badly wrong for a repo that merely sits inside one. Asking about
    `~/projects/my-app` when only `~/projects` is indexed reported "ready", then answered from a
    graph spanning every repo on the machine."""
    from codeintel.providers.graph import GraphProvider

    gp = GraphProvider.__new__(GraphProvider)
    gp.available = True                                        # type: ignore[attr-defined]
    inner = tmp_path / "my-app"
    inner.mkdir()
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {"projects": [
        {"name": "umbrella", "root_path": str(tmp_path), "nodes": 50000}]})

    probe = gp.probe(str(inner))

    assert probe["repo_indexed"] is True                        # an index exists...
    assert "NOT indexed on its own" in probe["detail"]          # ...but not for what was asked
    assert probe["remediation"] == f"codeintel index {inner}"


def test_probe_stays_quiet_when_the_project_root_matches(monkeypatch, tmp_path):
    """The disclosure must be rare enough to mean something — a normal indexed repo says nothing."""
    from codeintel.providers.graph import GraphProvider

    gp = GraphProvider.__new__(GraphProvider)
    gp.available = True                                        # type: ignore[attr-defined]
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {"projects": [
        {"name": "my-app", "root_path": str(tmp_path), "nodes": 100}]})

    probe = gp.probe(str(tmp_path))
    assert "NOT indexed" not in probe["detail"]
    assert probe["remediation"] is None
