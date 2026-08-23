"""GraphProvider tests: never-raise invariant and key behavioral guarantees."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

from codeintel.graph_backend import BackendClient
from codeintel.graph_resolution import ProjectResolver
from codeintel.providers.graph import GraphProvider, ProjectLookup, ProjectResolution
from codeintel.server import code_status_handler


def test_match_project_resolves_a_relative_path(tmp_path, monkeypatch):
    # The backend stores absolute root_paths, so a relative project_root (e.g. `codeintel map .`)
    # must be normalized before matching — otherwise resolution fails from inside the repo and the
    # map/graph query silently reports "not indexed" (the map-stub bug).
    real = os.path.realpath(str(tmp_path))
    raw = {"projects": [{"name": "myrepo", "root_path": real}]}
    monkeypatch.chdir(tmp_path)
    assert GraphProvider._match_project(raw, ".").name == "myrepo"      # relative resolves now
    assert GraphProvider._match_project(raw, real).name == "myrepo"     # absolute still works
    assert GraphProvider._match_project(raw, os.path.join(real, "src")).name == "myrepo"  # subdir prefix


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
    p._project_cache[""] = ProjectResolution(name="myproject", matched_root="", scope="exact")
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
    assert GraphProvider._match_project(raw, root).name == "fresh-path-slug"

    # ...and independently of the order the backend happens to list them in.
    reversed_raw = {"projects": list(reversed(raw["projects"]))}
    assert GraphProvider._match_project(reversed_raw, root).name == "fresh-path-slug"


def test_match_project_keeps_the_first_listed_when_there_is_nothing_to_choose_between(tmp_path):
    """With no completeness signal — equal counts, or a backend that omits `nodes` entirely — fall
    back to the original first-listed rule rather than inventing an ordering."""
    root = str(tmp_path)
    raw = _projects(
        {"name": "aaa", "root_path": root, "nodes": 10},
        {"name": "zzz", "root_path": root, "nodes": 10},
    )
    assert GraphProvider._match_project(raw, root).name == "aaa"
    assert GraphProvider._match_project({"projects": list(reversed(raw["projects"]))}, root).name == "zzz"


def test_match_project_survives_a_missing_or_junk_node_count(tmp_path):
    """`nodes` comes from an untrusted backend payload; a missing or non-numeric one must not
    take out project resolution entirely."""
    root = str(tmp_path)
    raw = _projects(
        {"name": "no-count", "root_path": root},
        {"name": "junk-count", "root_path": root, "nodes": "lots"},
        {"name": "real-count", "root_path": root, "nodes": 5},
    )
    assert GraphProvider._match_project(raw, root).name == "real-count"


def test_match_project_still_prefers_exact_over_a_richer_parent(tmp_path):
    """Completeness only breaks ties among EXACT matches — a huge parent-directory index must
    never outrank the repo's own."""
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = _projects(
        {"name": "parent", "root_path": str(tmp_path), "nodes": 999_999},
        {"name": "repo", "root_path": str(repo), "nodes": 12},
    )
    assert GraphProvider._match_project(raw, str(repo)).name == "repo"


# --------------------------------------------------------------------------- failure reasons

def test_every_dispatched_op_is_declared_supported():
    """`_GRAPH_OPS` gates the unsupported-op reply, so an op wired into _dispatch but missing from
    the set would be reported unsupported while being perfectly implemented.

    The two sets are not identical, and the difference is derived rather than allowed for by hand:
    a WITHDRAWN op stays in `_GRAPH_OPS` so that asking for it still explains itself, while having
    no dispatch route because it has no implementation left. Anything else in the difference is
    drift."""
    import inspect

    from codeintel.providers.graph import _GRAPH_OPS, _WITHDRAWN_OPS

    source = inspect.getsource(GraphProvider._dispatch)
    dispatched = set(re.findall(r'op == "([a-z]+)"', source))
    assert dispatched - set(_GRAPH_OPS) == set(), (
        f"dispatched but not declared, so it answers as unsupported: {dispatched - set(_GRAPH_OPS)}")
    unrouted = set(_GRAPH_OPS) - dispatched
    assert unrouted == set(_WITHDRAWN_OPS), (
        f"declared with no dispatch route and no withdrawal entry, so it silently answers "
        f"`unsupported-op`: {sorted(unrouted - set(_WITHDRAWN_OPS))}")
    for op in unrouted:
        assert not hasattr(GraphProvider, f"_op_{op}"), (
            f"{op} still has an implementation but no route — withdraw it at the gate, not by "
            f"deleting its dispatch line, or the escape hatch reports `unsupported-op`")


def _provider_answering(dispatch_result):
    """A GraphProvider with the backend seam stubbed: available, resolving to a project, and
    dispatching to a fixed result — so the reason-mapping is tested, not the subprocess."""
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp.available = True                                            # type: ignore[attr-defined]
    gp._lookup_project = lambda root: ProjectLookup(               # type: ignore[method-assign]
        ProjectResolution(name="proj", matched_root="/repo", scope="exact"), "ok")
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
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "Users-alice-Documents-project-myrepo", "total_nodes": 5, "total_edges": 4})

    out = gp._op_overview("", "Users-alice-Documents-project-myrepo", 1000, str(repo))
    assert out.splitlines()[0] == "## Architecture: myrepo"
    assert "Users-alice" not in out


def test_overview_resolves_a_relative_root_before_naming_it(tmp_path, monkeypatch):
    """`codeintel map .` passes "." — whose basename is "." — and that titled the committed file
    with a dot."""
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {
        "project": "backend-id", "total_nodes": 5, "total_edges": 4})
    monkeypatch.chdir(tmp_path)

    out = gp._op_overview("", "backend-id", 1000, ".")
    assert out.splitlines()[0] == f"## Architecture: {tmp_path.resolve().name}"


def test_overview_falls_back_to_the_backend_name_without_a_root(monkeypatch):
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
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


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """(first line number, whole statement) for each logical line — continuations joined.

    Both source-enumeration checks below broke the moment a call was wrapped across two lines, which
    is a property of the formatter rather than of the code. A check that a line break can silence is
    not a check."""
    out: list[tuple[int, str]] = []
    depth = 0
    start = 0
    buf: list[str] = []
    for n, line in enumerate(source.splitlines(), 1):
        if not buf:
            start = n
        buf.append(line.strip())
        depth += line.count("(") + line.count("[") - line.count(")") - line.count("]")
        if depth <= 0:
            out.append((start, " ".join(buf)))
            buf = []
            depth = 0
    if buf:
        out.append((start, " ".join(buf)))
    return out


def test_no_renderer_anywhere_emits_a_raw_qualified_name():
    """Fixing these one at a time did not work. `_display` was fixed first; `_render_scan` was
    missed and shipped; a test was then added asserting "both renderers strip the prefix", and
    `chain` and `pattern` turned out to be a third and fourth — found only by running the tool on
    a real repo. So enumerate the MODULE, not a list of functions someone remembered."""
    import re

    source = pathlib.Path("src/codeintel/providers/graph.py").read_text()
    statements = _logical_lines(source)
    # Every site that READS the field off a backend row. `_display` is a sanctioned renderer that
    # strips internally, so passing it the key name is fine; anything else must strip the value —
    # either in the same statement, or into a local that the next few statements strip.
    offenders = []
    for i, (n, stmt) in enumerate(statements):
        if not re.search(r'\.get\(\s*["\']qualified_name', stmt):
            continue
        if "_strip_project_prefix" in stmt or "_label_of" in stmt:
            continue
        assigned = re.match(r"(\w+)\s*=", stmt)
        nearby = " ".join(s for _, s in statements[i + 1:i + 5])
        if assigned and f"_strip_project_prefix({assigned.group(1)}" in nearby:
            continue
        offenders.append(f"{n}: {stmt}")
    assert not offenders, "raw qualified name reaches output:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------- scan accuracy

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
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
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
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp.available = True                                        # type: ignore[attr-defined]
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {"projects": [
        {"name": "my-app", "root_path": str(tmp_path), "nodes": 100}]})

    probe = gp.probe(str(tmp_path))
    assert "NOT indexed" not in probe["detail"]
    assert probe["remediation"] is None


@pytest.mark.parametrize("name", [
    "use-toast.ts", "1731900000000-CreateInitialTables.ts", "1-tool-interfaces.md",
    "EC-1.1: Empty workflow plan", "my-component.spec.ts",
])
def test_a_kebab_case_filename_is_not_mistaken_for_a_project_slug(name):
    """"hyphen ⇒ project slug" was wrong: kebab-case FILENAMES are the dominant TS/JS convention,
    so `use-toast.ts` rendered as `ts` — 985 names in one real repo, 811 in another. The `changed`
    op takes exactly this path, because its rows carry `name` and no `qualified_name`."""
    from codeintel.providers.graph import _strip_project_prefix

    assert _strip_project_prefix(name) == name


@pytest.mark.parametrize("qn,expected", [
    ("Users-alice-Documents-project-myrepo.src.pkg.fn", "src.pkg.fn"),
    ("my-repo.src.pkg.fn", "src.pkg.fn"),
])
def test_a_real_project_slug_is_still_stripped(qn, expected):
    from codeintel.providers.graph import _strip_project_prefix

    assert _strip_project_prefix(qn) == expected


def test_probe_picks_the_same_duplicate_entry_the_resolver_did(monkeypatch, tmp_path):
    """`_project_root_of` returned the FIRST entry with that name rather than the one
    `_match_project` chose, so a stale duplicate registration produced a false "not indexed on its
    own" warning about a correctly-indexed repo."""
    from codeintel.providers.graph import GraphProvider

    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp.available = True                                        # type: ignore[attr-defined]
    monkeypatch.setattr(gp, "_run", lambda *a, **k: {"projects": [
        {"name": "myrepo", "root_path": "/elsewhere/myrepo", "nodes": 10},     # stale, listed first
        {"name": "myrepo", "root_path": str(tmp_path), "nodes": 900},          # the one chosen
    ]})

    probe = gp.probe(str(tmp_path))
    assert "NOT indexed" not in probe["detail"], "false alarm on a correctly-indexed repo"
    assert probe["remediation"] is None


def test_same_dir_is_case_insensitive_where_the_filesystem_is(tmp_path):
    """`realpath` resolves symlinks but does not canonicalise CASE, and macOS APFS is
    case-insensitive — so one directory under two spellings compared as two, and a correctly
    indexed repo read as unindexed."""
    from codeintel.graph_resolution import _same_dir

    assert _same_dir(str(tmp_path), str(tmp_path)) is True
    assert _same_dir(str(tmp_path), str(tmp_path) + "/") is True
    assert _same_dir(str(tmp_path), str(tmp_path / "..")) is False


# --------------------------------------------------------------------------- #
# Ancestor-project resolution: the answer path, not just the diagnostic
#
# 0.15.1 fixed this in `probe()` only. `doctor` warned that a repo was not indexed on its own
# while `code.query` — the surface an agent actually calls — went on answering from the project
# that contains it, silently. The two disagreed because they were two implementations of the same
# question; they now share one `ProjectResolution`, and these tests pin the behaviour of the half
# that was missing.
# --------------------------------------------------------------------------- #

def _provider_resolving(scope, dispatch_result="## Answer\nrow"):
    """A provider whose resolution lands on `scope` ("exact" or "ancestor")."""
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp.available = True                                            # type: ignore[attr-defined]
    gp._lookup_project = lambda root: ProjectLookup(               # type: ignore[method-assign]
        ProjectResolution(name="parent-project", matched_root="/repos", scope=scope), "ok")
    gp._dispatch = lambda *a, **k: dispatch_result                 # type: ignore[method-assign]
    return gp


@pytest.mark.parametrize("op", ["deadcode", "hotspots", "overview", "changed"])
def test_repo_scan_ops_refuse_to_answer_from_a_containing_project(op):
    """A repo-scan answer is DEFINED BY the repo boundary. "The monorepo's dead code" is not a
    lower-confidence answer to "this repo's dead code", it is the answer to a different question —
    and a symbol dead in one repo is routinely live in its sibling, so answering here is how
    `deadcode` tells an agent to delete working code."""
    res = _provider_resolving("ancestor").build_result(op, "", [], 5000, "/repos/my-app")
    assert res["result"] is None
    assert res["reason"] == "project-not-indexed-standalone"
    assert "codeintel index /repos/my-app" in res["hint"]
    # Distinguishable from "nothing is indexed at all", which has its own reason and remedy.
    assert res["reason"] != "project-not-indexed"


@pytest.mark.parametrize("op", ["callers", "callees", "impact", "chain"])
def test_symbol_scoped_ops_still_answer_from_an_ancestor_but_say_so(op):
    """For a genuine subdirectory of a monorepo the containing index is exactly where a symbol's
    callers live, so refusing would break the common case. Answer — but disclose the scope in the
    result text, the only channel that reaches the model."""
    res = _provider_resolving("ancestor").build_result(op, "foo", [], 5000, "/repos/my-app")
    assert res["result"] is not None
    assert "## Answer" in res["result"]
    assert "not indexed on its own" in res["result"]
    assert "codeintel index /repos/my-app" in res["result"]


@pytest.mark.parametrize("op", ["overview", "callers"])
def test_an_exact_match_is_never_caveated_or_refused(op):
    """The guard must not fire on a correctly-indexed repo — that would make every ordinary answer
    carry a scope warning, which trains the reader to ignore it."""
    res = _provider_resolving("exact").build_result(op, "foo", [], 5000, "/repos/my-app")
    assert res["result"] is not None
    assert "not indexed on its own" not in res["result"]


def test_the_not_in_graph_hint_does_not_leak_the_backends_project_id():
    """For a path-slug registration the project id IS the flattened absolute path of the repo, so
    naming it in a hint discloses the server's directory layout. The renderer sweep that fixed the
    home-path leaks grepped for `qualified_name` and never covered this channel."""
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp.available = True                                            # type: ignore[attr-defined]
    gp._lookup_project = lambda root: ProjectLookup(               # type: ignore[method-assign]
        ProjectResolution(name="Users-alice-Documents-work-secret-repo", matched_root="/x",
                          scope="exact"), "ok")
    gp._dispatch = lambda *a, **k: None                            # type: ignore[method-assign]
    res = gp.build_result("callers", "nope", [], 5000, "/x")
    assert res["reason"] == "not-in-graph"
    assert "Users-alice" not in res["hint"]
    assert "secret-repo" not in res["hint"]


# --------------------------------------------------------------------------- #
# Backend wire-format compatibility
#
# codebase-memory-mcp 0.9.x answers query_graph/search_graph with {"columns", "rows"}. 0.10.x
# replaced that with a compact text format while KEEPING list_projects as JSON — so resolution and
# doctor kept working while every actual query returned nothing, and the tool reported "not in the
# graph index" about a fully indexed repository. No CI job installs the backend, so nothing caught
# it; the one live test that would have failed skipped instead, on a condition this bug produced.
# --------------------------------------------------------------------------- #

def _provider_speaking(stdout_bytes):
    """A provider whose backend subprocess returns *stdout_bytes* verbatim."""
    import subprocess as _sp

    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp._resolver = ProjectResolver.__new__(ProjectResolver)         # type: ignore[attr-defined]
    gp._resolver._backend = gp._backend                             # type: ignore[attr-defined]
    gp.available = True                                            # type: ignore[attr-defined]
    gp._cmd = "/fake/codebase-memory-mcp"                          # type: ignore[attr-defined]
    gp._saw_unparsable = False                                     # type: ignore[attr-defined]
    gp._project_cache = {}                                         # type: ignore[attr-defined]
    gp._negative_until = {}                                        # type: ignore[attr-defined]
    gp._project_cache_lock = __import__("threading").Lock()        # type: ignore[attr-defined]

    def _fake(cmd, **kwargs):
        return _sp.CompletedProcess(cmd, 0, stdout=stdout_bytes, stderr=b"")

    return gp, _fake


def test_a_text_speaking_backend_is_reported_as_incompatible_not_as_missing_data(monkeypatch):
    """The exact 0.10.x behaviour: exit 0, and a human-readable body where JSON was promised."""
    text = b'rows: 2  (cols: a.name)\n  "codeintel"\n  "codeintel.viewer"\ntotal: 2\n'
    gp, fake = _provider_speaking(text)
    monkeypatch.setattr("codeintel.providers.graph.subprocess.run", fake)
    gp._lookup_project = lambda root: ProjectLookup(                # type: ignore[method-assign]
        ProjectResolution(name="proj", matched_root="/repo", scope="exact"), "ok")

    res = gp.build_result("callers", "thing", [], 5000, "/repo")
    assert res["ok"] is True
    assert res["result"] is None
    # The critical assertion: it must NOT claim the symbol is absent from the index.
    assert res["reason"] == "backend-incompatible"
    assert res["reason"] != "not-in-graph"
    assert "0.9" in res["hint"]
    assert "NOT a statement about whether your repository is indexed" in res["hint"]


def test_a_json_speaking_backend_is_not_flagged_incompatible(monkeypatch):
    """The guard against a false positive: the supported dialect must stay silent."""
    body = b'{"columns":["a.name","a.qualified_name","a.file_path","type(c)"],' \
           b'"rows":[["bar","pkg.bar","bar.py","CALLS"]],"total":1}'
    gp, fake = _provider_speaking(body)
    monkeypatch.setattr("codeintel.providers.graph.subprocess.run", fake)
    gp._lookup_project = lambda root: ProjectLookup(                # type: ignore[method-assign]
        ProjectResolution(name="proj", matched_root="/repo", scope="exact"), "ok")

    res = gp.build_result("callers", "thing", [], 5000, "/repo")
    assert res.get("reason") is None
    assert res["result"] is not None and "pkg.bar" in res["result"]
    assert gp._saw_unparsable is False


def test_a_slow_backend_is_not_reported_as_an_unindexed_project(monkeypatch):
    """Resolution used a hardcoded 3000ms budget while the real backend measured ~5.8s per call,
    so EVERY query timed out during resolution and was reported as `project-not-indexed` with the
    advice to re-index a repository that was already indexed. A timeout is a fact about the
    backend, not about the repository."""
    gp = GraphProvider.__new__(GraphProvider)
    gp._backend = BackendClient.__new__(BackendClient)              # type: ignore[attr-defined]
    gp._resolver = ProjectResolver.__new__(ProjectResolver)         # type: ignore[attr-defined]
    gp._resolver._backend = gp._backend                             # type: ignore[attr-defined]
    gp.available = True                                            # type: ignore[attr-defined]
    gp._cmd = "/fake/codebase-memory-mcp"                          # type: ignore[attr-defined]
    gp._saw_unparsable = False                                     # type: ignore[attr-defined]
    gp._project_cache = {}                                         # type: ignore[attr-defined]
    gp._negative_until = {}                                        # type: ignore[attr-defined]
    gp._project_cache_lock = __import__("threading").Lock()        # type: ignore[attr-defined]
    gp._run = lambda method, payload, timeout_ms: None             # type: ignore[method-assign]

    res = gp.build_result("callers", "thing", [], 5000, "/repo")
    assert res["reason"] == "backend-unreachable"
    assert res["reason"] != "project-not-indexed"
    # And a timeout must not be cached as a miss, or a slow moment is remembered as "no index".
    assert "/repo" not in gp._negative_until


@pytest.mark.parametrize("qualified", [
    "private-tmp-codeintel-corpus-requests.src.requests.models.Response.json",
    "Users-alice-Documents-project-myrepo.src.pkg.Renderer.html",
    "my-repo.src.pkg.Config.toml",
])
def test_a_symbol_named_like_a_file_extension_still_loses_the_project_prefix(qualified):
    """`Response.json` is a method — the most-called one in `requests` — and the filename guard read
    it as a `.json` file and returned the name unstripped, leaking the backend's project id (the
    flattened absolute path, so in normal use the user's home directory) into a rendered hotspots
    row. Found by adding a second repository to the corpus, not by reasoning about the string:
    `my-component.spec.ts` and `Response.json` are the same shape, and no rule on the string can
    separate them. The FIELD separates them — a filename arrives in `name`, a module path in
    `qualified_name` — so the guard is now the caller's to claim."""
    from codeintel.providers.graph import _strip_project_prefix

    assert _strip_project_prefix(qualified, may_be_filename=False) == qualified.split(".", 1)[1]
    # And the guard still protects the case it was added for, on the path that needs it.
    assert _strip_project_prefix(qualified) == qualified


def test_no_renderer_passes_a_qualified_name_through_the_filename_guard():
    """The population is the module, not a list of renderers someone remembered — the same reasoning
    as `test_no_renderer_anywhere_emits_a_raw_qualified_name`, which this complements: that one
    catches a renderer that never strips, this one catches a renderer that strips with the filename
    guard left on and so silently keeps the prefix for any symbol named `json`, `html` or `toml`."""
    import re

    source = pathlib.Path("src/codeintel/providers/graph.py").read_text()
    offenders = [
        f"{n}: {stmt}"
        for n, stmt in _logical_lines(source)
        if re.search(r"_strip_project_prefix\(\s*(?:str\(\s*)?\w+\.get\(\s*[\"']?\w*\.?qualified_name",
                     stmt)
        and "may_be_filename=False" not in stmt
    ]
    assert not offenders, (
        "a qualified name is being stripped with the filename guard on:\n" + "\n".join(offenders))
