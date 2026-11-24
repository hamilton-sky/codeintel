"""Story: an agent working right after an edit.

Every graph answer says when the index is older than the code. "When the index was written" is the
mtime of the graph backend's `<project id>.db` under `CODEBASE_MEMORY_HOME`, which the tests set
with `os.utime` (spec Risks: "The test uses `os.utime` to set exact times").
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from _fakegraph import Repo, gap_kinds, safe_for_destructive

from codeintel.gateway.gateway import Gateway
from codeintel.gateway.reindexer import Reindexer
from codeintel.transports import mcp as server

_STALE_KINDS = {"stale-index", "index-age-unknown"}


def _helper_repo(tmp_path, monkeypatch, *, git: bool = True) -> Repo:
    repo = Repo(tmp_path, monkeypatch, git=git)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/core.py",
               "def helper():\n    return 1\n\n\ndef compute():\n    return helper()\n")
    repo.write("pkg/app.py",
               "from pkg.core import helper\n\n\ndef main():\n    return helper()\n")
    if git:
        repo.commit(when=time.time() - 600)
    g = repo.graph
    helper = g.node("pkg.core.helper", "pkg/core.py")
    compute = g.node("pkg.core.compute", "pkg/core.py")
    main = g.node("pkg.app.main", "pkg/app.py")
    g.edge(compute, helper, strategy="same_module", confidence="0.90")
    g.edge(main, helper, strategy="import_map", confidence="0.95")
    g.edge(compute, g.node("pkg.app.main", "pkg/app.py"), strategy="import_map", confidence="0.95")
    repo.fresh_index()
    return repo


def _assert_stale(env: dict, kinds=frozenset({"stale-index"})) -> None:
    assert set(gap_kinds(env)) & set(kinds), f"no {sorted(kinds)} gap: {gap_kinds(env)}"
    assert env.get("confidence") != "complete", env.get("confidence")


# --------------------------------------------------------------------------- changed (no target)

# Spec: `changed` with no target, after an edit with no reindex, has a `stale-index` gap (or
# `index-age-unknown`) and is at best `partial`.
def test_uncommitted_changed_after_an_edit_without_reindex_has_a_stale_index_gap(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    repo.write("pkg/core.py", "def helper():\n    return 2\n\n\ndef brand_new():\n    return 3\n")
    env = repo.ask("changed", "")
    _assert_stale(env, _STALE_KINDS)


# Spec: An untracked source file with 0 indexed symbols counts as stale.
def test_uncommitted_changed_with_an_untracked_file_the_index_never_saw_is_stale(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    written = os.path.getmtime(repo.db_path)
    # Older than the index on purpose: it is the 0 indexed symbols, not the mtime, that makes it
    # stale.
    repo.write("pkg/brandnew.py", "def untracked_fn():\n    return 1\n", mtime=written - 30)
    env = repo.ask("changed", "")
    _assert_stale(env, _STALE_KINDS)


# Spec: `changed` with no target in a non-git directory returns `outcome: unavailable` with
# `reason: not-a-git-repo`, never "working tree clean".
# Edge case: Uncommitted `changed` in a non-git dir -> `not-a-git-repo`, unavailable.
def test_uncommitted_changed_outside_a_git_repo_is_unavailable_not_a_git_repo(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch, git=False)
    env = repo.ask("changed", "")
    assert env.get("reason") == "not-a-git-repo", env
    assert env.get("outcome") == "unavailable", env
    assert "working tree clean" not in str(env.get("result") or "")


# --------------------------------------------------------------------------- changed <ref>

# Spec: `changed <ref>` is stale when HEAD's commit time (`git log -1 --format=%ct`) is newer than
# the graph db, even when no file in the diff is.
# Edge case: Commit made after the index, outside the diff -> `stale-index`.
def test_changed_ref_is_stale_when_head_was_committed_after_the_index(tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    now = time.time()
    # The diff since HEAD~1 is pkg/core.py; every file on disk predates the index...
    repo.write("pkg/core.py", "def helper():\n    return 5\n\n\ndef compute():\n    return helper()\n")
    repo.write("pkg/app3.py", "from pkg.core import helper\n\n\ndef new_caller():\n    return helper()\n")
    repo.commit("after the index", when=now - 10)
    repo.age_all(now - 120)
    repo.stamp_index(now - 60)
    # ...but HEAD was committed after it.
    assert int(repo.run_git("log", "-1", "--format=%ct").strip()) > os.path.getmtime(repo.db_path)
    env = repo.ask("changed", "HEAD~1")
    _assert_stale(env)


# Spec: An edit 0.1 s after the index is stale, because the 2 s slack is gone.
# Edge case: Edit 0.1 s after the index -> `stale-index` (no slack).
def test_changed_ref_is_stale_for_an_edit_a_tenth_of_a_second_after_the_index(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    written = os.path.getmtime(repo.db_path)
    repo.write("pkg/core.py", "def helper():\n    return 9\n\n\ndef compute():\n    return helper()\n",
               mtime=written + 0.1)
    env = repo.ask("changed", "HEAD")
    _assert_stale(env)


# --------------------------------------------------------------------------- callers / callees / impact

# Spec: `callers`, `callees` and `impact` have a `stale-index` gap when the target's file ... is
# newer than the index. ... the answer is never `safe_for_destructive`.
def test_callers_has_a_stale_index_gap_when_the_targets_file_is_newer_than_the_index(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    repo.write("pkg/core.py", "def helper():\n    return 2\n\n\ndef compute():\n    return helper()\n")
    env = repo.ask("callers", "helper")
    _assert_stale(env)
    assert safe_for_destructive(env) is False


# Spec: ... when ... any row's file is ... newer than the index. Rows in those files are
# `unstated`, and the answer is never `safe_for_destructive`.
# Happy path 1: The agent edits `core.py` and asks `callers helper` without reindexing. The answer
# is partial with a `stale-index` gap, and rows in `core.py` are unstated.
def test_callers_rows_in_a_file_edited_after_the_index_are_unstated(tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    repo.write("pkg/app.py", "def main():\n    return 0\n")
    env = repo.ask("callers", "helper")
    _assert_stale(env)
    assert env.get("confidence") == "partial"
    rows = {r.get("qualified_name"): r for r in env.get("rows") or []}
    assert rows["pkg.app.main"].get("evidence") == "unstated", rows["pkg.app.main"]
    assert rows["pkg.core.compute"].get("evidence") == "resolved", rows["pkg.core.compute"]
    assert safe_for_destructive(env) is False


# Spec: `callers` ... have a `stale-index` gap when ... any row's file is missing. Rows in those
# files are `unstated`.
# Edge case: A row's file was deleted or renamed after the index -> row `unstated`, `stale-index`
# gap.
def test_callers_row_in_a_file_deleted_after_the_index_is_unstated(tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    os.remove(repo.path("pkg/app.py"))
    env = repo.ask("callers", "helper")
    _assert_stale(env)
    rows = {r.get("qualified_name"): r for r in env.get("rows") or []}
    assert rows["pkg.app.main"].get("evidence") == "unstated", rows["pkg.app.main"]
    assert safe_for_destructive(env) is False


# Edge case: A row's file was ... renamed after the index -> row `unstated`, `stale-index` gap.
def test_callers_row_in_a_file_renamed_after_the_index_is_unstated(tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    written = os.path.getmtime(repo.db_path)
    repo.run_git("mv", "pkg/app.py", "pkg/application.py")
    os.utime(repo.path("pkg/application.py"), (written - 30, written - 30))
    env = repo.ask("callers", "helper")
    _assert_stale(env)
    rows = {r.get("qualified_name"): r for r in env.get("rows") or []}
    assert rows["pkg.app.main"].get("evidence") == "unstated", rows["pkg.app.main"]
    assert safe_for_destructive(env) is False


# Spec: `callees` ... have a `stale-index` gap when the target's file ... is missing ...
def test_callees_of_a_symbol_whose_file_was_deleted_has_a_stale_index_gap(tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    os.remove(repo.path("pkg/core.py"))
    env = repo.ask("callees", "compute")
    _assert_stale(env)
    assert safe_for_destructive(env) is False


# Spec: `impact` ha[s] a `stale-index` gap when the target's file or any row's file is ... newer
# than the index.
def test_impact_has_a_stale_index_gap_when_a_rows_file_is_newer_than_the_index(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    repo.write("pkg/app.py", "def main():\n    return 0\n")
    env = repo.ask("impact", "helper")
    _assert_stale(env)
    assert safe_for_destructive(env) is False


# --------------------------------------------------------------------------- the MCP cache

def _mcp_gateway(repo: Repo, monkeypatch) -> Gateway:
    monkeypatch.setenv("CODEINTEL_REINDEX", "off")
    gw = Gateway(graph=repo.provider(), reindexer=Reindexer())
    monkeypatch.setattr(server, "_GATEWAY", gw)
    monkeypatch.setattr(server, "_refresh_missing_engines", lambda gw: None)
    return gw


def _mcp_callers(repo: Repo, target: str = "helper") -> dict:
    return server.code_query_handler(
        {"op": "callers", "target": target, "project_root": repo.root, "engine": "graph"})


def _external_index(repo: Repo) -> None:
    """What `codeintel index` in another process does: the graph learns about b.py's c2, and the
    db file is rewritten."""
    repo.write("pkg/b.py", "from pkg.core import helper\n\n\ndef c2():\n    return helper()\n")
    g = repo.graph
    g.edge(g.node("pkg.b.c2", "pkg/b.py"), g.node("pkg.core.helper", "pkg/core.py"),
           strategy="import_map", confidence="0.95")
    now = time.time()
    repo.age_all(now - 5)
    repo.stamp_index(now - 1)


# Spec: Over MCP with `CODEINTEL_REINDEX=off`, after `codeintel index` runs in another process, the
# next `callers` call is not served from cache (`cached` is false) and includes the new caller.
# Edge case: External `codeintel index` while `serve` runs with REINDEX=off -> next answer is a
# cache miss.
# Happy path 2: The agent runs `codeintel index .` and asks again. The cache key has moved, so the
# answer is fresh, `cached: false` and complete if nothing else is missing.
def test_mcp_callers_after_an_external_index_is_a_cache_miss_with_the_new_caller(
        tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    _mcp_gateway(repo, monkeypatch)
    _mcp_callers(repo)
    assert _mcp_callers(repo).get("cached") is True      # the cache is in play before the index
    _external_index(repo)
    after = _mcp_callers(repo)
    assert after.get("cached") is False, after
    assert "pkg.b.c2" in str(after.get("result") or ""), after.get("result")
    assert after.get("confidence") == "complete", after.get("gaps")


# Edge case: Index deleted with `reset` while the server runs -> missing db changes the key: cache
# miss.
def test_mcp_callers_after_the_index_is_deleted_is_a_cache_miss(tmp_path, monkeypatch):
    repo = _helper_repo(tmp_path, monkeypatch)
    _mcp_gateway(repo, monkeypatch)
    _mcp_callers(repo)
    assert _mcp_callers(repo).get("cached") is True
    os.remove(repo.db_path)
    after = _mcp_callers(repo)
    assert after.get("cached") is False, after


# --------------------------------------------------------------------------- the reindexing flag

# Spec: `reindexing: true` appears only when a pass was already running before the query, or this
# query found a changed tree. An unchanged tree gives no flag.
# Edge case: Query on an unchanged tree (MCP) -> no `reindexing` flag.
def test_reindexing_flag_only_for_a_pass_already_running_or_a_changed_tree(
        tmp_path, monkeypatch, background_reindex):
    repo = _helper_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEINTEL_REINDEX", "on")
    release = threading.Event()
    passes: list[str] = []

    def _slow_pass(self, root):
        passes.append(root)
        release.wait(10)

    monkeypatch.setattr(Reindexer, "_semantic_reindex", _slow_pass)
    monkeypatch.setattr(Reindexer, "_graph_reindex", lambda self, root: None)
    reindexer = Reindexer(debounce_seconds=0)
    gw = Gateway(graph=repo.provider(), reindexer=reindexer)
    monkeypatch.setattr(server, "_GATEWAY", gw)
    monkeypatch.setattr(server, "_refresh_missing_engines", lambda gw: None)

    def ask(target: str = "helper") -> dict:
        return server.code_query_handler(
            {"op": "callers", "target": target, "project_root": repo.root, "engine": "graph"})

    def drain() -> None:
        release.set()
        deadline = time.time() + 10
        while reindexer.reindex_pending(repo.root) and time.time() < deadline:
            time.sleep(0.02)
        release.clear()

    try:
        ask()                                  # whatever the first query does, let it settle
        drain()

        unchanged = ask("main")             # nothing changed on disk since
        assert unchanged.get("result") is not None, unchanged
        assert not unchanged.get("reindexing"), unchanged
        drain()

        # A pass that was already running before the query (another process holds the
        # repository's index lock) is reported.
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import os, sys\n"
             "from codeintel.core.filelock import exclusive, lock_path\n"
             "with exclusive(lock_path(os.path.realpath(sys.argv[1]))) as got:\n"
             "    print('held' if got else 'busy', flush=True)\n"
             "    sys.stdin.read()\n",
             repo.root],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            assert holder.stdout is not None and holder.stdout.readline().strip() == b"held"
            running = ask("pkg.app.main")
            assert running.get("reindexing") is True, running
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(10)
            holder.stdout.close()
        drain()

        # This query found a changed tree.
        repo.write("pkg/core.py", "def helper():\n    return 42\n")
        changed = ask("pkg.core.helper")
        assert changed.get("reindexing") is True, changed
    finally:
        release.set()
