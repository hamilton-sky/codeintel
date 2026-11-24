"""Known graph blind spots add a gap, so `safe_for_destructive` is never stamped on what the graph cannot see.

The findings behind this file (`.feature-flow/state/redblue-findings.md`):

* `complete-callees-miss-aliased-import`: `from pkg import helper as h` then `h(2)` gives no edge to
  `helper`, and `callees entry` / `callers helper` still said complete and safe.
* `nested-fn-false-verified-caller`: `outer` defines its own nested `inner` and calls it; the index
  merged the two `inner`s, so `outer` came back as a verified (`lsp_direct`) caller of the module
  `inner`, and the footer said it was "the only symbol named `inner`".
* `ts-safe-for-destructive-wrong-half`: the same in TypeScript, where the false row carried
  `lsp_ts_local`, which was taken as a language-server resolution on its own.

The rows below are shaped like the 0.9.0 (and, where they differ, 0.10.x) rows those repros
returned. The source the rules read is the fixture tree under `tests/fixtures/blindspots/`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from codeintel.engines.graph import qualifier_scan
from codeintel.engines.graph.provider import GraphProvider

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "blindspots")
PY_ROOT = os.path.realpath(os.path.join(FIXTURES, "py"))
TS_ROOT = os.path.realpath(os.path.join(FIXTURES, "ts"))
PROJECT = "blindspots"


def _row(caller: str, caller_file: str, callee: str, callee_file: str, *, edge: str = "CALLS",
         strategy: str | None = None, conf: str | None = None, labels: str = "Function",
         caller_name: str | None = None) -> dict:
    row = {
        "a.name": caller_name or caller.rsplit(".", 1)[-1],
        "a.qualified_name": f"{PROJECT}.{caller}",
        "a.file_path": caller_file,
        "labels(a)": labels,
        "type(c)": edge,
        "b.name": callee.rsplit(".", 1)[-1],
        "b.qualified_name": f"{PROJECT}.{callee}",
        "b.file_path": callee_file,
        "labels(b)": "Function",
    }
    if strategy is not None:
        row["strategy"] = strategy
    if conf is not None:
        row["c.confidence"] = conf
    return row


def _module_scope(file: str, callee: str, callee_file: str) -> dict:
    """A 0.9.0 USAGE row from a file's module scope: the import line, hung off the file node."""
    stem = file.rsplit("/", 1)[-1]
    module = file[:-3].replace("/", ".")
    return _row(f"{module}.py.__file__", file, callee, callee_file, edge="USAGE",
                labels='["File"]', caller_name=stem)


def _ask(monkeypatch, root: str, op: str, target: str, rows: list[dict]) -> dict:
    monkeypatch.setattr(
        "codeintel.engines.graph.provider.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    listing = {"projects": [{"name": PROJECT, "root_path": root}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))
    # Only the edge query answers with the rows; every lookup beyond it (bases, owners, `self.`
    # hierarchy) finds nothing, as it would for these plain functions.
    monkeypatch.setattr(p, "_query_rows", lambda cypher, project, timeout_ms: (
        list(rows) if "-[c:" in cypher and "count(*)" not in cypher else []))
    return p.build_result(op, target, [], 30000, root)


def _gaps(env: dict, kind: str) -> list[dict]:
    return [g for g in env.get("gaps") or [] if g["kind"] == kind]


def _row_named(env: dict, name: str) -> dict:
    return next(r for r in env["rows"] if r["name"] == name)


# ---- rows as 0.9.0 returned them for the Python repro ---------------------------------------

HELPER_CALLERS_09 = [
    _row("pkg.sub.impl.process", "pkg/sub/impl.py", "pkg.sub.impl.helper", "pkg/sub/impl.py",
         strategy="lsp_direct", conf="0.95"),
    _module_scope("main.py", "pkg.sub.impl.helper", "pkg/sub/impl.py"),
    _module_scope("alias_direct.py", "pkg.sub.impl.helper", "pkg/sub/impl.py"),
    _module_scope("pkg/__init__.py", "pkg.sub.impl.helper", "pkg/sub/impl.py"),
]

# 0.10.x binds the direct alias and returns no module-scope rows at all, so on main this answer
# was complete and safe while `main.entry`'s `h(2)` was missing.
HELPER_CALLERS_010 = [
    _row("pkg.sub.impl.process", "pkg/sub/impl.py", "pkg.sub.impl.helper", "pkg/sub/impl.py",
         strategy="lsp_direct", conf="0.95"),
    _row("alias_direct.via_alias_direct", "alias_direct.py", "pkg.sub.impl.helper",
         "pkg/sub/impl.py", strategy="lsp_callable_alias", conf="0.95"),
]

ENTRY_CALLEES = [
    _row("main.entry", "main.py", "pkg.sub.impl.process", "pkg/sub/impl.py",
         strategy="import_map_suffix", conf="0.95"),
]

INNER_CALLERS_PY = [
    _row("pkg.shapes.outer", "pkg/shapes.py", "pkg.shapes.inner", "pkg/shapes.py",
         strategy="lsp_direct", conf="0.95"),
    _row("pkg.shapes.calls_module_inner", "pkg/shapes.py", "pkg.shapes.inner", "pkg/shapes.py",
         strategy="lsp_direct", conf="0.95"),
]

INNER_CALLERS_TS = [
    _row("src.shapes.outer", "src/shapes.ts", "src.shapes.inner", "src/shapes.ts",
         strategy="lsp_ts_local", conf="0.95"),
]

HELPER_CALLERS_TS = [
    _row("src.lib.impl.process", "src/lib/impl.ts", "src.lib.impl.helper", "src/lib/impl.ts",
         strategy="lsp_ts_local", conf="0.95"),
]


# ---- aliased imports ------------------------------------------------------------------------

@pytest.mark.parametrize("rows", [HELPER_CALLERS_09, HELPER_CALLERS_010], ids=["0.9.0", "0.10.x"])
def test_callers_of_a_symbol_imported_under_an_alias_is_not_safe(monkeypatch, rows):
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", rows)

    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    detail = " ".join(g["detail"] for g in gaps)
    assert "alias_direct.py" in detail and "`h2`" in detail, detail
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False
    # Said in the body too, where a reader looks.
    assert "h2" in env["result"], env["result"]


def test_the_alias_gap_names_the_file_and_the_alias_of_a_module_scope_row(monkeypatch):
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", HELPER_CALLERS_09)
    detail = " ".join(g["detail"] for g in _gaps(env, "aliased-import-unbound"))
    assert "main.py" in detail and "`h`" in detail, detail


def test_callees_of_a_body_calling_an_import_alias_with_no_row_is_not_complete(monkeypatch):
    env = _ask(monkeypatch, PY_ROOT, "callees", "entry", ENTRY_CALLEES)

    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    assert "`h`" in gaps[0]["detail"] and "main.py" in gaps[0]["detail"], gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


def test_callees_whose_alias_call_has_its_row_raises_no_alias_gap(monkeypatch):
    """`via_alias_direct` calls `h2(1)` and the backend bound it to `helper`: nothing is missing."""
    rows = [_row("alias_direct.via_alias_direct", "alias_direct.py", "pkg.sub.impl.helper",
                 "pkg/sub/impl.py", strategy="lsp_callable_alias", conf="0.95")]
    env = _ask(monkeypatch, PY_ROOT, "callees", "via_alias_direct", rows)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["confidence"] == "complete", env.get("gaps")


def test_a_file_that_does_not_exist_adds_nothing(monkeypatch):
    """Positive evidence only: the rule fires on text it read. The target is one no file in the
    fixture imports under an alias (`helper` is, and the tree search now finds that wherever it is)."""
    rows = [_row("pkg.gone.caller", "pkg/gone.py", "main.entry", "main.py",
                 strategy="lsp_direct", conf="0.95")]
    env = _ask(monkeypatch, PY_ROOT, "callers", "entry", rows)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_root_that_does_not_exist_adds_nothing(monkeypatch):
    env = _ask(monkeypatch, "/nonexistent/blindspots/root", "callers", "helper", HELPER_CALLERS_010)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_file_the_budget_could_not_read_adds_the_gap_as_could_not_check(monkeypatch):
    monkeypatch.setattr(qualifier_scan, "_MAX_FILES", 0)
    env = _ask(monkeypatch, PY_ROOT, "callers", "helper", HELPER_CALLERS_010)

    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    assert "could not check" in " ".join(g["detail"] for g in gaps), gaps
    assert env["evidence"]["safe_for_destructive"] is False


# ---- aliases in files the answer has no row from --------------------------------------------
#
# The acceptance fakes' shape: the file that imports the target under an alias has a node of its own
# but no caller row, because the backend never bound the alias. The rule above reads only the rows'
# files and the target's file, so it has to search the tree for the alias.

def _repo(root, files: dict[str, str], *, git: bool = True) -> str:
    root.mkdir()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if git:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "--no-gpg-sign", "-m", "c")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, env=env,
                           check=True, capture_output=True)
    return os.path.realpath(str(root))


_PY_ALIAS_FILES = {
    "core.py": "def helper():\n    return 1\n",
    "a.py": "from core import helper\n\n\ndef use_a():\n    return helper()\n",
    "b.py": "from core import helper as h\n\n\ndef use_b():\n    return h()\n",
}
_PY_ALIAS_ROWS = [_row("a.use_a", "a.py", "core.helper", "core.py", strategy="import_map",
                       conf="0.95")]
_TS_ALIAS_ROWS = [_row("src.lib.impl.process", "src/lib/impl.ts", "src.lib.impl.helper",
                       "src/lib/impl.ts", strategy="lsp_ts_direct", conf="0.95")]
_TS_IMPL = ("export function helper(x: number) { return x; }\n"
            "export function process(x: number) { return helper(x); }\n")


def _assert_alias_gap_names(env: dict, file: str, alias: str) -> None:
    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    detail = " ".join(g["detail"] for g in gaps)
    assert file in detail and f"`{alias}`" in detail, detail
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


@pytest.mark.parametrize("git", [True, False], ids=["git", "not-a-repository"])
def test_python_alias_in_a_file_with_no_row_is_found(monkeypatch, tmp_path, git):
    root = _repo(tmp_path / "repo", _PY_ALIAS_FILES, git=git)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)
    _assert_alias_gap_names(env, "b.py", "h")


def test_ts_alias_import_in_a_file_with_no_row_is_found(monkeypatch, tmp_path):
    root = _repo(tmp_path / "repo", {
        "src/lib/impl.ts": _TS_IMPL,
        "src/main.ts": 'import { helper as h } from "./lib/impl";\n'
                       "export function entry() { return h(2); }\n",
    })
    env = _ask(monkeypatch, root, "callers", "helper", _TS_ALIAS_ROWS)
    _assert_alias_gap_names(env, "src/main.ts", "h")


def test_ts_reexport_under_an_alias_in_a_file_with_no_row_is_found(monkeypatch, tmp_path):
    root = _repo(tmp_path / "repo", {
        "src/lib/impl.ts": _TS_IMPL,
        "src/lib/index.ts": 'export { helper as runHelper } from "./impl";\n',
        "src/main.ts": 'import { runHelper } from "./lib";\n'
                       "export function entry() { return runHelper(2); }\n",
    })
    env = _ask(monkeypatch, root, "callers", "helper", _TS_ALIAS_ROWS)
    _assert_alias_gap_names(env, "src/lib/index.ts", "runHelper")


def test_a_tree_walk_that_stops_at_its_limit_says_could_not_check(monkeypatch, tmp_path):
    from codeintel.engines.graph import blindspots

    monkeypatch.setattr(blindspots, "_WALK_MAX_FILES", 0)
    root = _repo(tmp_path / "repo", _PY_ALIAS_FILES, git=False)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)

    detail = " ".join(g["detail"] for g in _gaps(env, "aliased-import-unbound"))
    assert "could not check" in detail and "file limit" in detail, detail
    assert env["evidence"]["safe_for_destructive"] is False


def test_files_aliasing_searches_nothing_it_cannot_name(tmp_path):
    from codeintel.engines.graph.blindspots import AliasSearch, files_aliasing

    root = _repo(tmp_path / "repo", _PY_ALIAS_FILES)
    assert files_aliasing(root, "helper", "python") == AliasSearch(("b.py",))
    assert files_aliasing(root, "-e", "python") == AliasSearch()          # not an identifier
    assert files_aliasing(root, "helper", "go") == AliasSearch()          # no alias reader for it
    assert files_aliasing(str(tmp_path / "missing"), "helper", "python") == AliasSearch()


def test_a_tree_with_no_alias_of_the_target_stays_complete(monkeypatch, tmp_path):
    files = dict(_PY_ALIAS_FILES, **{"b.py": "from core import helper\n\n\ndef use_b():\n"
                                             "    return helper()\n"})
    root = _repo(tmp_path / "repo", files)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_an_alias_in_a_file_of_another_language_is_not_counted(monkeypatch, tmp_path):
    files = dict(_PY_ALIAS_FILES, **{
        "b.py": "from core import helper\n",
        "web/main.ts": 'import { helper as h } from "./lib";\n'})
    root = _repo(tmp_path / "repo", files)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)
    assert not _gaps(env, "aliased-import-unbound"), env.get("gaps")


# ---- the tree walk on a stalled mount, and what the gaps say -------------------------------------

def _stall(monkeypatch, module, attr: str, blocks):
    """Make `module.attr` block (up to 10 s) on the calls *blocks* picks out; the event releases it."""
    import threading

    release = threading.Event()
    real = getattr(module, attr)

    def call(*args, **kwargs):
        if blocks(*args):
            release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attr, call)
    return release


def _let_go(release) -> None:
    """Release a stalled call and wait for its thread, so no later test sees a stalled read."""
    import time

    release.set()
    deadline = time.monotonic() + 10
    while qualifier_scan._stalled_reads() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_walk_whose_read_stalls_gives_up_in_time_and_says_could_not_check(monkeypatch, tmp_path):
    import time

    from codeintel.engines.graph import blindspots

    monkeypatch.setattr(blindspots, "_WALK_MAX_SECONDS", 0.3)
    root = _repo(tmp_path / "repo", _PY_ALIAS_FILES, git=False)
    release = _stall(monkeypatch, qualifier_scan, "_read", lambda real, path, *a: path.endswith("b.py"))
    try:
        began = time.monotonic()
        search = blindspots.files_aliasing(root, "helper", "python")
        took = time.monotonic() - began
    finally:
        _let_go(release)
    assert took < 2.0, took
    assert search.shortfall and "b.py" not in search.files, search


def test_a_walk_whose_directory_listing_stalls_gives_up_in_time(monkeypatch, tmp_path):
    import time

    from codeintel.engines.graph import blindspots

    monkeypatch.setattr(blindspots, "_WALK_MAX_SECONDS", 0.3)
    files = dict(_PY_ALIAS_FILES, **{"sub/c.py": "from core import helper as hh\n"})
    root = _repo(tmp_path / "repo", files, git=False)
    release = _stall(monkeypatch, blindspots, "_list_dir", lambda path: path.endswith("sub"))
    try:
        began = time.monotonic()
        search = blindspots.files_aliasing(root, "helper", "python")
        took = time.monotonic() - began
    finally:
        _let_go(release)
    assert took < 2.0, took
    assert search.shortfall and "sub/c.py" not in search.files, search


def test_a_walk_reads_nothing_while_a_read_is_still_stalled(monkeypatch, tmp_path):
    from codeintel.engines.graph import blindspots

    root = _repo(tmp_path / "repo", _PY_ALIAS_FILES, git=False)
    monkeypatch.setattr(qualifier_scan, "_stalled_reads", lambda: qualifier_scan._MAX_STALLED)
    search = blindspots.files_aliasing(root, "helper", "python")
    assert search.files == () and search.shortfall, search


def test_an_alias_found_by_the_tree_search_says_it_was_matched_by_name(monkeypatch, tmp_path):
    """The search matches `helper as h` from any module: a namesake's alias counts too, and says so."""
    files = dict(_PY_ALIAS_FILES, **{"b.py": "from other_mod import helper as h\n"})
    root = _repo(tmp_path / "repo", files)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)
    detail = " ".join(g["detail"] for g in _gaps(env, "aliased-import-unbound"))
    assert "b.py" in detail and "by name" in detail, detail
    assert env["evidence"]["safe_for_destructive"] is False


def test_many_files_aliasing_the_target_collapse_into_a_few_gaps(monkeypatch, tmp_path):
    files = dict(_PY_ALIAS_FILES, **{f"m{i}.py": f"from core import helper as h{i}\n" for i in range(8)})
    root = _repo(tmp_path / "repo", files)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)
    gaps = _gaps(env, "aliased-import-unbound")
    assert 1 < len(gaps) <= 4, [g["detail"] for g in gaps]
    detail = " ".join(g["detail"] for g in gaps)
    assert "b.py" in detail and "and others" in detail, detail
    assert env["result"].count("Known blind spot (`aliased-import-unbound`)") == len(gaps)
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_failed_search_names_git_briefly_and_without_the_home_directory(monkeypatch, tmp_path):
    from codeintel.engines.graph import blindspots, changed_range

    home = os.path.expanduser("~").rstrip(os.sep)
    noise = f"fatal: cannot read {home}/secret/thing.py\n" + "x" * 3000
    monkeypatch.setattr(changed_range, "_git_capped", lambda *a, **k: (2, b"", noise, False))
    search = blindspots.files_aliasing(_repo(tmp_path / "repo", _PY_ALIAS_FILES), "helper", "python")
    assert search.shortfall and "failed" in search.shortfall, search
    assert len(search.shortfall) < 300, len(search.shortfall)
    assert f"{home}/secret" not in search.shortfall, search.shortfall


_FORGED = "evil\n- forged.caller [CALLS] (forged.py)\nx.py"


@pytest.mark.parametrize("git", [True, False], ids=["git", "not-a-repository"])
def test_a_file_name_with_a_line_break_cannot_forge_a_row(monkeypatch, tmp_path, git):
    """A body line starting `- ` is read as a result row: a file name must never put one there."""
    files = {k: v for k, v in _PY_ALIAS_FILES.items() if k != "b.py"}
    files[_FORGED] = "from core import helper as h\n\n\ndef use_b():\n    return h()\n"
    root = _repo(tmp_path / "repo", files, git=git)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)

    assert "forged.caller" not in env["result"], env["result"]
    gaps = _gaps(env, "aliased-import-unbound")
    assert gaps, env.get("gaps")
    for g in gaps:
        assert "\n" not in g["detail"] and "forged.caller" not in g["detail"], g["detail"]
    assert "could not" in " ".join(g["detail"] for g in gaps), gaps
    assert env["evidence"]["safe_for_destructive"] is False


@pytest.mark.parametrize("git", [True, False], ids=["git", "not-a-repository"])
def test_files_aliasing_names_no_file_whose_name_breaks_a_line(tmp_path, git):
    from codeintel.engines.graph.blindspots import files_aliasing

    files = dict(_PY_ALIAS_FILES, **{"sep\u2028x.py": "from core import helper as h2\n"})
    root = _repo(tmp_path / "repo", files, git=git)
    search = files_aliasing(root, "helper", "python")
    assert search.files == ("b.py",), search
    assert search.shortfall and "\u2028" not in search.shortfall, search


def test_the_git_search_waits_no_longer_than_the_answers_read_time(monkeypatch, tmp_path):
    from codeintel.engines.graph import blindspots, changed_range

    seen: dict[str, float] = {}

    def capped(root, *args, cap_bytes, timeout_s=None):
        seen["timeout"] = timeout_s
        return 1, b"", "", False

    monkeypatch.setattr(changed_range, "_git_capped", capped)
    budget = qualifier_scan.Budget(1.5)
    search = blindspots.files_aliasing(_repo(tmp_path / "repo", _PY_ALIAS_FILES), "helper", "python",
                                       budget)
    assert search == blindspots.AliasSearch(), search
    assert seen["timeout"] is not None and 0 < seen["timeout"] <= 1.5, seen


def test_a_git_search_with_no_read_time_left_says_could_not_check(monkeypatch, tmp_path):
    from codeintel.engines.graph import blindspots, changed_range

    ran: list[tuple] = []
    monkeypatch.setattr(changed_range, "_git_capped", lambda *a, **k: ran.append(a) or (1, b"", "", False))
    search = blindspots.files_aliasing(_repo(tmp_path / "repo", _PY_ALIAS_FILES), "helper", "python",
                                       qualifier_scan.Budget(0.0))
    assert not ran, ran
    assert search.shortfall and "time" in search.shortfall, search


def test_git_capped_kills_a_command_at_the_timeout_it_is_given(monkeypatch, tmp_path):
    import time

    from codeintel.engines.graph import changed_range

    monkeypatch.setattr(changed_range, "_GIT", ("sh", "-c", "sleep 5", "sh"))
    began = time.monotonic()
    code, out, err, cut = changed_range._git_capped(str(tmp_path), "grep", cap_bytes=100, timeout_s=0.3)
    took = time.monotonic() - began
    assert took < 3, took
    assert code < 0 and out == b"" and not cut, (code, out, err)
    assert "did not finish within 0.3s" in err, err


@pytest.mark.parametrize("git", [True, False], ids=["git", "not-a-repository"])
def test_a_candidate_file_too_large_to_read_says_could_not_check(monkeypatch, tmp_path, git):
    files = dict(_PY_ALIAS_FILES, **{"b.py": "from core import helper as h\n" + "#" * 4000 + "\n"})
    root = _repo(tmp_path / "repo", files, git=git)
    monkeypatch.setattr(qualifier_scan, "MAX_SOURCE_BYTES", 1000)
    env = _ask(monkeypatch, root, "callers", "helper", _PY_ALIAS_ROWS)

    detail = " ".join(g["detail"] for g in _gaps(env, "aliased-import-unbound"))
    assert "b.py" in detail and "could not" in detail, detail
    assert env["evidence"]["safe_for_destructive"] is False


# ---- nested shadowing -----------------------------------------------------------------------

@pytest.mark.parametrize(("root", "rows"), [(PY_ROOT, INNER_CALLERS_PY), (TS_ROOT, INNER_CALLERS_TS)],
                         ids=["python", "typescript"])
def test_a_caller_that_defines_its_own_nested_namesake_is_not_resolved(monkeypatch, root, rows):
    env = _ask(monkeypatch, root, "callers", "inner", rows)

    outer = _row_named(env, "outer")
    assert outer["verified"] is False, outer
    assert outer["evidence"] != "resolved", outer
    ambiguous = _gaps(env, "target-ambiguous")
    assert ambiguous, env.get("gaps")
    assert "outer" in ambiguous[0]["detail"] and "inner" in ambiguous[0]["detail"], ambiguous
    assert "only symbol named" not in env["result"], env["result"]
    assert env["evidence"]["safe_for_destructive"] is False


def test_the_caller_without_a_nested_namesake_stays_resolved(monkeypatch):
    env = _ask(monkeypatch, PY_ROOT, "callers", "inner", INNER_CALLERS_PY)
    assert _row_named(env, "calls_module_inner")["verified"] is True


# ---- lsp_ts_local ---------------------------------------------------------------------------

def test_an_lsp_ts_local_row_is_possible_not_verified(monkeypatch):
    env = _ask(monkeypatch, TS_ROOT, "callers", "helper", HELPER_CALLERS_TS)

    row = _row_named(env, "process")
    assert row["verified"] is False, row
    assert row["evidence"] == "name-matched", row
    assert "lsp_ts_local" in row["why"] and "not" in row["why"], row["why"]
    assert env["evidence"]["possible"] == 1 and env["evidence"]["verified"] == 0, env["evidence"]
    assert env["evidence"]["safe_for_destructive"] is False


def test_lsp_ts_local_is_name_matched_wherever_it_is_fake_rooted(monkeypatch):
    """The strategy alone decides, with no file to read: the made-up root of most graph tests."""
    env = _ask(monkeypatch, "/nonexistent/ts/root", "callers", "helper", HELPER_CALLERS_TS)
    assert _row_named(env, "process")["verified"] is False
    assert env["evidence"]["safe_for_destructive"] is False


# ---- sibling and interface dispatch ----------------------------------------------------------
#
# `dynamic-dispatch-asymmetric`: the backend binds the duck-typed `s.area()` in `total_area` to
# `Circle.area` by suffix match, so `callers Square.area` used to answer complete and safe without it.
# `ts-safe-for-destructive-wrong-half`: in TypeScript `Square implements Shape`, and a call written
# against `Shape` reaches `Square.area` with no edge to it at all.

def _method_row(caller: str, caller_file: str, callee: str, callee_file: str, *, strategy: str,
                written: str, conf: str | None = None, caller_labels: str = "Function") -> dict:
    row = _row(caller, caller_file, callee, callee_file, strategy=strategy, conf=conf,
               labels=caller_labels)
    row["labels(b)"] = "Method"
    row["callee"] = written
    return row


def _definer(cls: str, file: str, method: str, *, bases: str = "[]") -> dict:
    return {"c.qualified_name": f"{PROJECT}.{cls}", "c.name": cls.rsplit(".", 1)[-1],
            "c.file_path": file, "c.base_classes": bases,
            "m.qualified_name": f"{PROJECT}.{cls}.{method}", "m.file_path": file}


def _interface(qn: str, file: str) -> dict:
    return {"i.name": qn.rsplit(".", 1)[-1], "i.qualified_name": f"{PROJECT}.{qn}",
            "i.file_path": file, "labels(i)": '["Interface"]'}


def _ask_dispatch(monkeypatch, root: str, target: str, edges: list[dict], definers: list[dict],
                  interfaces: list[dict] | None = None, *, fail_edge_call: int = 0,
                  methods: list[dict] | None = None, seen: list[str] | None = None) -> dict:
    """Like `_ask`, with a router that answers the way the backend does for a method: every edge row
    of the bare name for an edge query, the classes defining the name, and the interfaces."""
    monkeypatch.setattr(
        "codeintel.engines.graph.provider.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    p = GraphProvider()
    listing = {"projects": [{"name": PROJECT, "root_path": root}]}
    monkeypatch.setattr(p, "_run", lambda method, payload, timeout_ms: (
        listing if method == "list_projects" else None))
    edge_calls = [0]

    def route(cypher: str, project: str, timeout_ms: int) -> list[dict]:
        if seen is not None:
            seen.append(cypher)
        if "count(*)" in cypher:
            return []
        if "-[c:" in cypher:
            edge_calls[0] += 1
            if fail_edge_call and edge_calls[0] >= fail_edge_call:
                raise RuntimeError("backend went away")
            return list(edges)
        if ":Interface" in cypher:
            return list(interfaces or [])
        if "(m:Method)" in cypher and "DEFINES_METHOD" not in cypher:
            return list(methods or [])
        if "DEFINES_METHOD" in cypher and "m.name=" in cypher:
            return list(definers)
        return []

    monkeypatch.setattr(p, "_query_rows", route)
    return p.build_result("callers", target, [], 30000, root)


PY_AREA_EDGES = [
    _method_row("pkg.shapes.Square.describe", "pkg/shapes.py", "pkg.shapes.Square.area",
                "pkg/shapes.py", strategy="lsp_method", written="self.area", conf="0.90",
                caller_labels="Method"),
    _method_row("pkg.shapes.only_square", "pkg/shapes.py", "pkg.shapes.Square.area",
                "pkg/shapes.py", strategy="lsp_method", written="sq.area", conf="0.90"),
    _method_row("pkg.shapes.total_area", "pkg/shapes.py", "pkg.shapes.Circle.area",
                "pkg/shapes.py", strategy="suffix_match", written="s.area", conf="0.55"),
]
PY_AREA_DEFINERS = [_definer("pkg.shapes.Circle", "pkg/shapes.py", "area"),
                    _definer("pkg.shapes.Square", "pkg/shapes.py", "area")]

TS_AREA_EDGES = [
    _method_row("src.shapes.Square.describe", "src/shapes.ts", "src.shapes.Square.area",
                "src/shapes.ts", strategy="lsp_ts_method", written="this.area", conf="0.90",
                caller_labels="Method"),
    _method_row("src.shapes.onlySquare", "src/shapes.ts", "src.shapes.Square.area",
                "src/shapes.ts", strategy="lsp_ts_method", written="sq.area", conf="0.90"),
]
TS_AREA_DEFINERS = [_definer("src.shapes.Circle", "src/shapes.ts", "area", bases='["Shape"]'),
                    _definer("src.shapes.Square", "src/shapes.ts", "area", bases='["Shape"]')]
TS_INTERFACES = [_interface("src.shapes.Shape", "src/shapes.ts")]


def test_callers_of_a_method_lists_the_call_a_sibling_was_bound_to_by_name(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_AREA_EDGES, PY_AREA_DEFINERS)

    total = _row_named(env, "total_area")
    assert total["evidence"] == "name-matched" and total["verified"] is False, total
    assert "Circle.area" in total["why"], total["why"]
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "Circle.area" in gaps[0]["detail"] and "total_area" in gaps[0]["detail"], gaps
    assert gaps[0]["section"] != "callees", gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False
    assert "total_area" in env["result"] and "dispatch-sibling-unbound" in env["result"]
    # The two callers the backend did bind to `Square.area` are still there, as they were.
    assert _row_named(env, "only_square")["verified"] is True


def test_callers_of_the_method_the_call_was_bound_to_is_unchanged(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Circle.area", PY_AREA_EDGES, PY_AREA_DEFINERS)

    assert [r["name"] for r in env["rows"]] == ["total_area"], env["rows"]
    assert env["confidence"] == "partial"
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_sibling_lookup_that_finds_only_the_target_adds_nothing(monkeypatch):
    edges = [r for r in PY_AREA_EDGES if r["a.name"] != "total_area"]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", edges, PY_AREA_DEFINERS[1:])
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["confidence"] == "complete", env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_sibling_with_only_bound_callers_adds_nothing(monkeypatch):
    """`Circle.area`'s callers here are all language-server bindings: no call was guessed by name."""
    edges = [dict(r, strategy="lsp_method") if r["a.name"] == "total_area" else r
             for r in PY_AREA_EDGES]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", edges, PY_AREA_DEFINERS)
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_sibling_whose_callers_could_not_be_fetched_says_could_not_check(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_AREA_EDGES, PY_AREA_DEFINERS,
                        fail_edge_call=2)
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "could not check" in gaps[0]["detail"] and "Circle.area" in gaps[0]["detail"], gaps
    assert env["evidence"]["safe_for_destructive"] is False


# The acceptance fakes' shape: the backend holds the same-named `*.area` METHOD nodes and the
# `suffix_match` edge, and no class node at all, so no class defines the method. The call text is the
# bare method name, as a backend that records no receiver gives it.

def _method_node(qn: str, file: str) -> dict:
    return {"m.qualified_name": f"{PROJECT}.{qn}", "m.file_path": file}


PY_CLASSLESS_EDGES = [
    _method_row("pkg.shapes.Square.describe", "pkg/shapes.py", "pkg.shapes.Square.area",
                "pkg/shapes.py", strategy="lsp_method", written="area", conf="0.95",
                caller_labels="Method"),
    _method_row("pkg.shapes.only_square", "pkg/shapes.py", "pkg.shapes.Square.area",
                "pkg/shapes.py", strategy="lsp_method", written="area", conf="0.95"),
    _method_row("pkg.shapes.total_area", "pkg/shapes.py", "pkg.shapes.Circle.area",
                "pkg/shapes.py", strategy="suffix_match", written="area", conf="0.55"),
]
PY_CLASSLESS_METHODS = [_method_node("pkg.shapes.Circle.area", "pkg/shapes.py"),
                        _method_node("pkg.shapes.Square.area", "pkg/shapes.py")]

TS_CLASSLESS_EDGES = [
    _method_row("src.shapes.Square.describe", "src/shapes.ts", "src.shapes.Square.area",
                "src/shapes.ts", strategy="lsp_ts_method", written="area", conf="0.95",
                caller_labels="Method"),
    _method_row("src.shapes.onlySquare", "src/shapes.ts", "src.shapes.Square.area",
                "src/shapes.ts", strategy="lsp_ts_method", written="area", conf="0.95"),
    _method_row("src.shapes.totalArea", "src/shapes.ts", "src.shapes.Shape.area",
                "src/shapes.ts", strategy="suffix_match", written="area", conf="0.55"),
]
TS_CLASSLESS_METHODS = [_method_node("src.shapes.Shape.area", "src/shapes.ts"),
                        _method_node("src.shapes.Square.area", "src/shapes.ts")]


def test_a_sibling_method_with_no_class_node_lists_its_name_matched_caller(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_CLASSLESS_EDGES, [],
                        methods=PY_CLASSLESS_METHODS)

    total = _row_named(env, "total_area")
    assert total["evidence"] == "name-matched" and total["verified"] is False, total
    assert "Circle.area" in total["why"], total["why"]
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "Circle.area" in gaps[0]["detail"] and "total_area" in gaps[0]["detail"], gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False
    assert _row_named(env, "only_square")["verified"] is True


def test_a_protocol_member_with_no_class_node_lists_its_name_matched_caller(monkeypatch):
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.area", TS_CLASSLESS_EDGES, [],
                        methods=TS_CLASSLESS_METHODS)

    total = _row_named(env, "totalArea")
    assert total["evidence"] == "name-matched" and total["verified"] is False, total
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert any("Shape.area" in g["detail"] and "totalArea" in g["detail"] for g in gaps), gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_classless_sibling_whose_callers_are_all_bound_adds_nothing(monkeypatch):
    edges = [dict(r, strategy="lsp_method") if r["a.name"] == "total_area" else r
             for r in PY_CLASSLESS_EDGES]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", edges, [],
                        methods=PY_CLASSLESS_METHODS)
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_classless_sibling_call_through_self_is_not_listed(monkeypatch):
    """`self.area()` inside `Circle` runs `Circle`'s own method, whichever class the backend saw."""
    edges = [dict(r, callee="self.area", **{"labels(a)": "Method"}) if r["a.name"] == "total_area"
             else r for r in PY_CLASSLESS_EDGES]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", edges, [],
                        methods=PY_CLASSLESS_METHODS)
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")


def test_a_classless_namesake_is_not_called_a_sibling_for_certain(monkeypatch):
    """With no class node, `Circle.area` may be a base's method as well as a sibling's: say so."""
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_CLASSLESS_EDGES, [],
                        methods=PY_CLASSLESS_METHODS)
    why = _row_named(env, "total_area")["why"]
    detail = " ".join(g["detail"] for g in _gaps(env, "dispatch-sibling-unbound"))
    for text in (why, detail):
        assert "a sibling, a base or a subclass" in text, text
        assert "not known" in text, text
    assert "on a sibling class" not in why, why


# A Python target and a TypeScript namesake: the backend's `suffix_match` bound a `.ts` call to the
# `.ts` method, which no call in the Python tree can reach.
PY_AND_TS_EDGES = [
    *PY_CLASSLESS_EDGES[:2],
    _method_row("web.shapes.tsTotal", "web/shapes.ts", "web.shapes.Tri.area", "web/shapes.ts",
                strategy="suffix_match", written="area", conf="0.55"),
]
PY_AND_TS_METHODS = [_method_node("web.shapes.Tri.area", "web/shapes.ts"),
                     _method_node("pkg.shapes.Square.area", "pkg/shapes.py")]


def test_a_namesake_method_in_another_language_is_not_listed(monkeypatch):
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_AND_TS_EDGES, [],
                        methods=PY_AND_TS_METHODS)
    assert "tsTotal" not in [r["name"] for r in env["rows"]], env["rows"]
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")
    assert "tsTotal" not in env["result"], env["result"]
    assert env["evidence"]["safe_for_destructive"] is True


def test_a_namesake_in_the_same_language_is_still_listed_beside_one_in_another(monkeypatch):
    methods = [*PY_CLASSLESS_METHODS, PY_AND_TS_METHODS[0]]
    env = _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", [*PY_CLASSLESS_EDGES, PY_AND_TS_EDGES[2]],
                        [], methods=methods)
    names = [r["name"] for r in env["rows"]]
    assert "total_area" in names and "tsTotal" not in names, names
    assert _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")


def test_the_namesake_lookup_asks_only_for_methods_of_the_targets_language(monkeypatch):
    """Filtered in the query, so a name common in another language does not fill its row limit."""
    import re

    seen: list[str] = []
    _ask_dispatch(monkeypatch, PY_ROOT, "Square.area", PY_CLASSLESS_EDGES, [],
                  methods=PY_CLASSLESS_METHODS, seen=seen)
    lookup = next(c for c in seen if "(m:Method)" in c and "DEFINES_METHOD" not in c)
    found = re.search(r'm\.file_path\s*=~\s*"((?:[^"\\]|\\.)*)"', lookup)
    assert found, lookup
    pattern = found.group(1).replace("\\\\", "\\")
    for path in ("pkg/shapes.py", "pkg/stubs.pyi", "pkg/SHAPES.PY"):
        assert re.fullmatch(pattern, path), (pattern, path)
    for path in ("web/shapes.ts", "src/main.go", "pkg/shapes.py.bak"):
        assert not re.fullmatch(pattern, path), (pattern, path)


def test_ts_callers_of_a_method_an_interface_declares_has_the_gap(monkeypatch):
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.area", TS_AREA_EDGES, TS_AREA_DEFINERS,
                        TS_INTERFACES)

    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "`Shape`" in gaps[0]["detail"] and "area" in gaps[0]["detail"], gaps
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


def test_ts_an_interface_that_does_not_declare_the_method_adds_nothing(monkeypatch):
    edges = [dict(r, **{"b.name": "describe",
                        "b.qualified_name": f"{PROJECT}.src.shapes.Square.describe"})
             for r in TS_AREA_EDGES if r["a.name"] == "onlySquare"]
    definers = [_definer("src.shapes.Square", "src/shapes.ts", "describe", bases='["Shape"]')]
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.describe", edges, definers, TS_INTERFACES)
    assert not _gaps(env, "dispatch-sibling-unbound"), env.get("gaps")


def test_ts_an_interface_file_the_budget_could_not_read_says_could_not_check(monkeypatch):
    monkeypatch.setattr(qualifier_scan, "_MAX_FILES", 0)
    env = _ask_dispatch(monkeypatch, TS_ROOT, "Square.area", TS_AREA_EDGES, TS_AREA_DEFINERS,
                        TS_INTERFACES)
    gaps = _gaps(env, "dispatch-sibling-unbound")
    assert gaps, env.get("gaps")
    assert "could not check" in " ".join(g["detail"] for g in gaps), gaps
    assert env["evidence"]["safe_for_destructive"] is False


# ---- the pure functions ---------------------------------------------------------------------

def test_alias_imports_finds_every_alias_form():
    from codeintel.engines.graph.blindspots import alias_imports

    assert alias_imports("from pkg import run_process, helper as h\n", "helper") == ["h"]
    assert alias_imports("from pkg.sub.impl import (\n    helper as h2,\n)\n", "helper") == ["h2"]
    assert alias_imports("import pkg.helper as hh\n", "helper") == ["hh"]
    assert alias_imports('import { runProcess, helper as h } from "./lib";\n', "helper") == ["h"]
    assert alias_imports('export { process as runProcess, helper } from "./impl";\n',
                         "process") == ["runProcess"]
    assert alias_imports('export { helper as X };\n', "helper") == ["X"]
    assert alias_imports("from pkg import helper\n", "helper") == []
    assert alias_imports("from pkg import other as helper\n", "helper") == []


def test_alias_imports_reads_python_that_does_not_parse_by_regex():
    from codeintel.engines.graph.blindspots import alias_imports

    assert alias_imports("from pkg import helper as h\ndef broken(:\n", "helper") == ["h"]


def test_nested_shadow_in_python_uses_the_ast():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    with open(os.path.join(PY_ROOT, "pkg", "shapes.py"), encoding="utf-8") as fh:
        text = fh.read()
    outer = function_span(text, "outer", "pkg/shapes.py")
    module_caller = function_span(text, "calls_module_inner", "pkg/shapes.py")
    assert outer is not None and module_caller is not None
    assert nested_shadow(text, outer, "inner") is True
    assert nested_shadow(text, module_caller, "inner") is False


def test_nested_shadow_falls_back_to_the_regex_when_python_does_not_parse():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    text = ("def outer():\n    def inner():\n        return 1\n    return inner()\n\n"
            "def broken(:\n    pass\n")
    span = function_span(text, "outer", "x.py")
    assert span is not None
    assert nested_shadow(text, span, "inner") is True


def test_nested_shadow_in_typescript_uses_the_regex():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    with open(os.path.join(TS_ROOT, "src", "shapes.ts"), encoding="utf-8") as fh:
        text = fh.read()
    outer = function_span(text, "outer", "src/shapes.ts")
    total = function_span(text, "totalArea", "src/shapes.ts")
    assert outer is not None and total is not None
    assert nested_shadow(text, outer, "inner") is True
    assert nested_shadow(text, total, "inner") is False


def test_a_call_before_the_nested_definition_is_not_shadowed():
    from codeintel.engines.graph.blindspots import function_span, nested_shadow

    text = "def outer():\n    x = inner()\n    def inner():\n        return 1\n    return x\n"
    span = function_span(text, "outer", "x.py")
    assert span is not None
    assert nested_shadow(text, span, "inner") is False


def test_interface_declares_reads_the_interface_body():
    from codeintel.engines.graph.blindspots import interface_declares

    with open(os.path.join(TS_ROOT, "src", "shapes.ts"), encoding="utf-8") as fh:
        text = fh.read()
    assert interface_declares(text, "Shape", "area") is True
    assert interface_declares(text, "Shape", "describe") is False
    assert interface_declares(text, "Missing", "area") is False
    assert interface_declares("interface A<T> extends B {\n  readonly area?: () => T;\n}\n",
                              "A", "area") is True


# ---- live -----------------------------------------------------------------------------------

@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None,
    reason="codebase-memory-mcp backend not installed",
)
def test_live_index_and_query_on_the_python_fixture(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(PY_ROOT, repo, ignore=shutil.ignore_patterns("__pycache__"))
    # The semantic half of `index` needs a model download that a sandbox cannot make; offline,
    # it fails at once instead of retrying, and the graph half is what this test reads.
    env = dict(os.environ, HF_HUB_OFFLINE="1")
    cli = [sys.executable, "-m", "codeintel"]
    subprocess.run([*cli, "index", "-q", str(repo)], cwd=repo, env=env, capture_output=True,
                   timeout=300, check=False)

    for target in ("helper", "inner"):
        done = subprocess.run(
            [*cli, "query", "--op", "callers", "--target", target, "--json", "--engine", "graph"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=120, check=False)
        answer = json.loads(done.stdout)
        assert answer.get("result") is not None, (
            f"codebase-memory-mcp is on PATH but the graph backend did not answer "
            f"`callers {target}`: {answer.get('reason')}; stderr: {done.stderr[-2000:]}")
        assert answer["evidence"]["safe_for_destructive"] is False, answer
        kinds = {g["kind"] for g in answer.get("gaps") or []}
        expected = "aliased-import-unbound" if target == "helper" else "target-ambiguous"
        assert expected in kinds, answer
