"""`code.query op=changed target=<ref>`: who uses the functions this branch removes or rewrites.

The question was "who uses the functions this branch removes or rewrites?", and `changed` could not
answer it three ways: it saw UNCOMMITTED edits only (a committed branch came back "working tree
clean"), it was FILE-granular (every symbol in a touched file was "impacted"), and a DELETED function
could not be asked about at all, because the graph indexed at HEAD has no node for it.

These tests run against a REAL throwaway git repository — the git half is not something a mock can
vouch for — and a fake backend that answers `callers` from a canned graph. What they pin is the
envelope: that every way the answer can be short of the truth is a named gap rather than a quiet
omission, and that a text mention is never allowed to read as a resolved call.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from codeintel.gateway import Gateway
from codeintel.outcome import Missing
from codeintel.providers.graph import GraphProvider

PROJECT = "tmp-proj"          # hyphenated, like a path-slug registration, so the prefix is stripped

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], capture_output=True,
        text=True, check=True, env={**os.environ, **_GIT_ENV})
    return done.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------------------------- the repository

_CORE_BASE = '''\
def keep(a):
    return a


def gone(a):
    return a


def resig(a, b=1):
    return a


def rewrite(a):
    return a + 1


class Svc:
    def run(self, x):
        return x
'''

_CORE_FEATURE = '''\
def keep(a):
    return a


def resig(a):
    return a


def rewrite(a):
    return a + 2


def fresh():
    return 0


class Svc:
    def run(self, x):
        return x
'''


@pytest.fixture(scope="module")
def _template(tmp_path_factory) -> Path:
    """`main` with a small package, then `feature` — one commit that removes `gone`, drops a
    parameter of `resig`, rewrites `rewrite`, adds `fresh`, and updates ONE of resig's two callers.

    Built once and copied per test: forty `git` invocations per test would be most of the suite's
    runtime for nothing."""
    repo = tmp_path_factory.mktemp("template")
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/core.py", _CORE_BASE)
    _write(repo, "pkg/also.py", "from pkg.core import resig\n\n\ndef use_resig():\n    return resig(1)\n")
    _write(repo, "pkg/legacy.py",
           "from pkg.core import resig, gone\n\n\ndef legacy():\n    return resig(2) + gone(3)\n\n\n"
           "def note():\n    # gone is documented elsewhere\n    return 0\n")
    _write(repo, "native/engine.cpp", "int f() { return 1; }\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "pkg/core.py", _CORE_FEATURE)
    _write(repo, "pkg/also.py",
           "from pkg.core import resig\n\n\ndef use_resig():\n    return resig(1) + 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature work")
    return repo


@pytest.fixture
def repo(_template, tmp_path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(_template, dest, symlinks=True)
    return dest


# ----------------------------------------------------------------------------------- the backend

def _edge(callee: str, callee_file: str, caller_qn: str, caller_file: str, *,
          conf: str = "0.95", strategy: str = "lsp_direct", kind: str = "CALLS") -> dict:
    callee_qn = f"{PROJECT}.{callee_file[:-3].replace('/', '.')}.{callee}"
    return {
        "a.name": caller_qn.rsplit(".", 1)[-1], "a.qualified_name": f"{PROJECT}.{caller_qn}",
        "a.file_path": caller_file, "labels(a)": "Function", "type(c)": kind,
        "c.confidence": conf, "strategy": strategy,
        "b.name": callee, "b.qualified_name": callee_qn, "b.file_path": callee_file,
    }


class _Graph:
    """The canned graph a fake backend answers from, and the record of what it was asked."""

    def __init__(self) -> None:
        self.edges: dict[str, list[dict]] = {}     # callee bare name -> edge rows
        self.nodes: dict[str, list[dict]] = {}     # bare name -> node rows (for the no-edges probe)
        self.fail: set[str] = set()                # names whose lookup times out
        self.methods: list[str] = []
        self.cyphers: list[str] = []


def _standard_graph() -> _Graph:
    g = _Graph()
    g.edges["resig"] = [
        _edge("resig", "pkg/core.py", "pkg.also.use_resig", "pkg/also.py"),
        _edge("resig", "pkg/core.py", "pkg.legacy.legacy", "pkg/legacy.py"),
    ]
    # `rewrite` is indexed and nothing calls it — the "no edges" case. `use_resig` is NOT in the
    # graph at all — the "never indexed" case. They must be told apart.
    g.nodes["rewrite"] = [{"n.qualified_name": f"{PROJECT}.pkg.core.rewrite",
                           "n.file_path": "pkg/core.py"}]
    return g


def _index(cache: Path, *, age: str = "fresh") -> None:
    """The graph backend's own file for this project, dated so the index looks fresh or stale."""
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / f"{PROJECT}.db"
    db.write_bytes(b"")
    now = time.time()
    when = now + 3600 if age == "fresh" else now - 7200
    os.utime(db, (when, when))


def _provider(monkeypatch, tmp_path, repo: Path, graph: _Graph, *,
              index: str | None = "fresh") -> GraphProvider:
    monkeypatch.setattr(
        "codeintel.providers.graph.shutil.which", lambda x: "/fake/codebase-memory-mcp")
    cache = tmp_path / "graph-cache"
    monkeypatch.setenv("CODEBASE_MEMORY_CACHE_DIR", str(cache))
    if index is not None:
        _index(cache, age=index)
    p = GraphProvider()
    listing = {"projects": [{"name": PROJECT, "root_path": str(repo)}]}

    def run(method, payload, timeout_ms):
        graph.methods.append(method)
        return listing if method == "list_projects" else None

    def rows(cypher, project, timeout_ms):
        graph.cyphers.append(cypher)
        if "-[" in cypher:
            name = re.search(r'b\.name="([^"]+)"', cypher).group(1)
            if name in graph.fail:
                p._backend._last_failure = Missing("timeout", "the graph backend timed out")
                return []
            return [dict(r) for r in graph.edges.get(name, [])]
        name = re.search(r'n\.name="([^"]+)"', cypher).group(1)
        return [dict(r) for r in graph.nodes.get(name, [])]

    monkeypatch.setattr(p, "_run", run)
    monkeypatch.setattr(p, "_query_rows", rows)
    return p


def _ask(p: GraphProvider, repo: Path, target: str = "main") -> dict[str, Any]:
    return p.build_result("changed", target, [], 30000, str(repo))


def _gap_kinds(env: dict) -> list[str]:
    return [g["kind"] for g in env.get("gaps", [])]


# ================================================================================ the base ref

def test_a_committed_branch_is_reported_where_a_clean_working_tree_used_to_hide_it(
        monkeypatch, tmp_path, repo):
    """The original defect. Every change here is COMMITTED, so the working tree is clean and the
    old op's answer was "(working tree clean — no uncommitted changes)"."""
    graph = _standard_graph()
    p = _provider(monkeypatch, tmp_path, repo, graph)
    env = _ask(p, repo)

    assert env["result"] is not None, env
    body = env["result"]
    assert "working tree clean" not in body
    assert "(1 removed · 1 signature · 2 body · 1 added, across 2 source file(s))" in body, body
    assert "detect_changes" not in graph.methods, "the range answer comes from git, not the backend"


def test_an_empty_target_is_still_the_original_uncommitted_op(monkeypatch, tmp_path, repo):
    """No target: byte-for-byte the op that existed — it asks the backend, and nothing else runs."""
    graph = _standard_graph()
    p = _provider(monkeypatch, tmp_path, repo, graph)
    calls = []

    def run(method, payload, timeout_ms):
        calls.append((method, payload))
        if method == "list_projects":
            return {"projects": [{"name": PROJECT, "root_path": str(repo)}]}
        return {"changed_files": [], "changed_count": 0, "impacted_symbols": [], "depth": 2}

    monkeypatch.setattr(p, "_run", run)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a subprocess was run"))
    env = _ask(p, repo, target="")
    assert env["result"] == "## Changes impact\n(working tree clean — no uncommitted changes)"
    assert ("detect_changes", {"project": PROJECT}) in calls


def test_a_blank_target_of_only_whitespace_is_still_the_original_op(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo, _Graph())
    monkeypatch.setattr(p, "_run", lambda m, pl, t: (
        {"projects": [{"name": PROJECT, "root_path": str(repo)}]} if m == "list_projects" else
        {"changed_files": [], "impacted_symbols": []}))
    assert "working tree clean" in _ask(p, repo, target="   ")["result"]


def test_the_range_compares_the_merge_base_against_the_working_tree_not_just_the_commits(
        monkeypatch, tmp_path, repo):
    """Committed AND uncommitted: an edit to `keep` that was never committed, and a brand-new file
    that git has never seen, are both part of "what this branch would change if committed now"."""
    _write(repo, "pkg/core.py", (repo / "pkg/core.py").read_text().replace(
        "def keep(a):\n    return a\n", "def keep(a):\n    return a * 3\n"))
    _write(repo, "pkg/brand_new.py", "def brand_new():\n    return 1\n")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    assert "`keep`" in body, "an uncommitted edit must be in the range"
    assert "`brand_new`" in body, "an untracked file is new work and must be in the range"


@pytest.mark.parametrize("target", ["main", "main...HEAD", "HEAD~1"])
def test_every_documented_spelling_of_a_base_resolves(monkeypatch, tmp_path, repo, target):
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo, target)
    assert env["result"] is not None and "## Changes since" in env["result"], env


def test_a_sha_and_a_tag_are_accepted_as_a_base(monkeypatch, tmp_path, repo):
    sha = _git(repo, "rev-parse", "main").strip()
    _git(repo, "tag", "v0", "main")
    for target in (sha, sha[:10], "v0"):
        env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo, target)
        assert env["result"] is not None, (target, env)


def test_the_answer_is_never_cached_by_the_gateway(monkeypatch, tmp_path, repo):
    """`changed` reads live state the content-hash cache cannot see, and a ref makes that worse:
    the same string means a different diff after every commit."""
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())
    gw = Gateway(graph=p)
    first = gw.query(op="changed", target="main", engine="graph", project_root=str(repo))
    second = gw.query(op="changed", target="main", engine="graph", project_root=str(repo))
    assert first["result"] is not None and "## Changes since" in first["result"]
    assert first["cached"] is False and second["cached"] is False


# ================================================================ refusals: a precise reason

def test_an_unknown_ref_is_a_safe_null_that_says_so(monkeypatch, tmp_path, repo):
    graph = _standard_graph()
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo, "no-such-branch")
    assert env["ok"] is True and env["result"] is None
    assert env["reason"] == "unknown-ref" and env["outcome"] == "not_found"
    assert "no-such-branch" in env["hint"]
    assert "not-in-graph" not in str(env), "a bad ref must not be reported as a claim about the index"
    assert "detect_changes" not in graph.methods


def test_a_root_that_is_not_a_git_repository_is_a_safe_null(monkeypatch, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    env = _ask(_provider(monkeypatch, tmp_path, plain, _Graph()), plain, "main")
    assert env["result"] is None and env["reason"] == "not-a-git-repo"
    assert env["outcome"] == "unavailable"


def test_a_root_that_does_not_exist_is_a_safe_null_and_not_an_exception(monkeypatch, tmp_path):
    ghost = tmp_path / "ghost"
    env = _ask(_provider(monkeypatch, tmp_path, ghost, _Graph()), ghost, "main")
    assert env["ok"] is True and env["result"] is None and env["reason"] == "not-a-git-repo"


def test_unrelated_histories_have_no_merge_base_and_say_so(monkeypatch, tmp_path, repo):
    """No common ancestor means no point to compare FROM. That is not "nothing changed"."""
    _git(repo, "checkout", "-q", "--orphan", "island")
    _git(repo, "rm", "-rfq", ".")
    _write(repo, "x.py", "def x(): pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "island")
    _git(repo, "checkout", "-q", "feature")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo, "island")
    assert env["result"] is None and env["reason"] == "no-merge-base"
    assert "unrelated histories" in env["hint"] and "not a statement that nothing changed" in env["hint"]


def test_a_detached_head_is_not_a_refusal_because_it_still_has_a_merge_base(
        monkeypatch, tmp_path, repo):
    """A checked-out SHA or tag is the normal state in CI and during a bisect. The comparison is
    against the merge-base, which a detached HEAD has as much as a branch does."""
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo, "main")
    assert env["result"] is not None and "(1 removed · 1 signature · 2 body · 1 added" in env["result"]


def test_a_repository_with_no_commits_is_a_safe_null(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    env = _ask(_provider(monkeypatch, tmp_path, empty, _Graph()), empty, "main")
    assert env["result"] is None and env["reason"] == "no-merge-base"


@pytest.mark.parametrize("target,why", [
    ("main..feature", "two-dot"),
    ("main...feature", "right-hand side can only be HEAD"),
])
def test_a_range_that_means_something_else_is_refused_not_reinterpreted(
        monkeypatch, tmp_path, repo, target, why):
    """`a..b` is a different diff in git (no merge-base). Answering it as `a...b` would be a
    semantic nobody asked for, presented as theirs."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo, target)
    assert env["result"] is None and env["reason"] == "unsupported-range"
    assert why in env["hint"]


def test_a_ref_that_looks_like_an_option_never_reaches_git(monkeypatch, tmp_path, repo):
    """`--output=<file>` as a ref would otherwise be a way to make `git` write a file."""
    victim = tmp_path / "victim"
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo, f"--output={victim}")
    assert env["result"] is None and env["reason"] == "unknown-ref"
    assert not victim.exists()


def test_a_refusal_does_not_leak_into_the_next_answer_on_the_same_provider(
        monkeypatch, tmp_path, repo):
    """The reason travels on per-request state. A stale one would turn a genuine `not-in-graph`
    miss on the NEXT call into a git complaint about a ref nobody asked about."""
    graph = _Graph()
    p = _provider(monkeypatch, tmp_path, repo, graph)
    assert _ask(p, repo, "no-such-branch")["reason"] == "unknown-ref"
    miss = p.build_result("callers", "nothing_here", [], 30000, str(repo))
    assert miss["result"] is None and miss["reason"] == "not-in-graph", miss


def test_an_unexpected_failure_inside_the_range_is_a_safe_null_never_a_raise(
        monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())

    def boom(*a, **k):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr("codeintel.graph_changed.diff_file", boom)
    env = _ask(p, repo)
    assert env["ok"] is True and env["result"] is None and env["reason"] == "error"
    assert "not a statement about your code" in env["hint"]


# ===================================================== a removed function: text, never evidence

def test_a_removed_function_that_is_still_referenced_is_listed_as_a_text_mention(
        monkeypatch, tmp_path, repo):
    """`gone` was deleted on the branch and `pkg/legacy.py` still imports and calls it. The graph
    cannot say so — it has no node for it — so the answer is `git grep`, and it must read as what
    it is: name mentions, never resolved calls."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]

    assert "### Removed — and still mentioned (1)" in body, body
    assert "`gone` — removed" in body and "[discovery]" in body.split("`gone` — removed")[1].split("\n")[0]
    assert "NOT resolved calls" in body

    mentions = [r for r in env["rows"] if r["changed_symbol"] == "gone"]
    assert {r["file"] for r in mentions} == {"pkg/legacy.py"}
    assert len(mentions) == 2                          # the import and the call; not the comment
    for r in mentions:
        assert r["relation"] == "mention"
        assert r["verified"] is False and r["evidence"] == "name-matched"
        assert r["strategy"] == "git-grep" and r["group_class"] == "discovery"
        assert "not a resolved call" in r["why"]


def test_a_text_mention_can_never_make_an_answer_safe_to_act_on(monkeypatch, tmp_path, repo):
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    assert env["evidence_class"] == "discovery"
    assert env["evidence"]["safe_for_destructive"] is False
    assert env["confidence"] == "partial" and "text-mention-only" in _gap_kinds(env)
    assert "Safe for destructive decisions: **no**" in env["result"]


def test_a_comment_that_names_a_removed_function_is_not_listed_as_a_surviving_use(
        monkeypatch, tmp_path, repo):
    """`# gone is documented elsewhere` is not a caller. The parser can tell for Python, so it is
    set aside — and counted, so the discarding is itself visible."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    listed = [r for r in env["rows"] if r["changed_symbol"] == "gone"]
    assert all("documented elsewhere" not in r["text"] for r in listed)


def test_a_mention_that_is_a_string_is_kept_and_labelled_rather_than_discarded(
        monkeypatch, tmp_path, repo):
    """`__all__ = ["gone"]` and `patch("pkg.core.gone")` break when `gone` goes, so a string
    mention is real. Dropping it would be the over-eager filter this tool keeps retiring."""
    _write(repo, "pkg/exports.py", '__all__ = ["gone"]\n')
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    labelled = {r["file"]: r["label"] for r in env["rows"] if r["changed_symbol"] == "gone"}
    assert labelled["pkg/exports.py"] == "string"
    assert "text mention, string" in env["result"]


def test_a_removed_function_nothing_mentions_is_listed_apart_and_says_so(
        monkeypatch, tmp_path, repo):
    _write(repo, "pkg/legacy.py", "def legacy():\n    return 0\n\n\ndef note():\n    return 0\n")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    assert "### Removed — nothing else mentions the name (1)" in body, body
    assert "### Removed — and still mentioned" not in body
    assert not [r for r in env["rows"] if r["changed_symbol"] == "gone"]


def test_finding_no_mention_of_a_removed_function_is_still_not_proof(monkeypatch, tmp_path):
    """Nothing in the working tree names `g` — and that is still a TEXT result. A name built at
    runtime, a `getattr`, a caller in another repository all leave no mention, so an answer whose
    only finding is "no mention" is `partial` and cannot be called safe."""
    repo = tmp_path / "solo-removed"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "m/core.py", "def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "m/core.py", "def f():\n    return 1\n")
    _git(repo, "commit", "-qam", "drop g")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)

    assert "### Removed — nothing else mentions the name (1)" in env["result"]
    assert env["confidence"] == "partial" and "text-mention-only" in _gap_kinds(env)
    assert "finding none is not proof" in str(env["gaps"])
    assert env["evidence_class"] == "discovery"
    assert (env.get("evidence") or {}).get("safe_for_destructive") is not True


def test_a_function_moved_to_another_file_is_flagged_as_possibly_moved(monkeypatch, tmp_path, repo):
    """`gone` left core.py and a `gone` appeared in moved.py. Whether those are the same function
    is a guess this op does not make — but the reader is told the new location exists."""
    _write(repo, "pkg/moved.py", "def gone(a):\n    return a\n")
    body = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)["result"]
    assert "was ADDED in `pkg/moved.py`" in body and "importing the old location" in body


def test_a_failed_text_search_is_unknown_and_never_an_empty_list(monkeypatch, tmp_path, repo):
    monkeypatch.setattr("codeintel.graph_changed.grep_mentions", lambda *a, **k: __import__(
        "codeintel.changed_range", fromlist=["MentionSearch"]).MentionSearch((), error="git grep died"))
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    assert "did not complete (git grep died)" in body and "UNKNOWN, not no" in body
    assert "callers-unavailable" in _gap_kinds(env)
    assert "nothing else mentions" not in body


# =============================================== a signature change: the also-changed split

def test_a_signature_change_splits_callers_into_those_the_diff_touched_and_those_it_did_not(
        monkeypatch, tmp_path, repo):
    """The core value. `resig` lost a parameter; `use_resig` was edited in the same diff and
    `legacy` was not — and `legacy` is the one that may break."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    group = body.split("#### `resig`")[1].split("####")[0]

    assert "signature: parameters (-b)" in group
    untouched, _, also = group.partition("also changed in this diff")
    assert "did NOT touch" in untouched and "pkg.legacy.legacy" in untouched
    assert "pkg.also.use_resig" in also and "pkg.legacy.legacy" not in also

    by_status = {r["name"]: r["caller_status"] for r in env["rows"]
                 if r["changed_symbol"] == "resig"}
    assert by_status == {"legacy": "untouched", "use_resig": "also-changed"}


def test_callers_keep_their_resolved_versus_name_matched_evidence(monkeypatch, tmp_path, repo):
    """Reused, not re-derived: the rows are the ones `callers` produced, so a name-matched caller
    is badged exactly as it would be there."""
    graph = _standard_graph()
    graph.edges["resig"][1] = _edge("resig", "pkg/core.py", "pkg.legacy.legacy", "pkg/legacy.py",
                                    conf="0.38", strategy="suffix_match")
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    rows = {r["name"]: r for r in env["rows"] if r["changed_symbol"] == "resig"}
    assert rows["use_resig"]["evidence"] == "resolved" and rows["use_resig"]["verified"] is True
    assert rows["legacy"]["evidence"] == "name-matched" and rows["legacy"]["verified"] is False
    assert "[?0.38]" in env["result"]
    assert "low-confidence-edges" in _gap_kinds(env)


def _solo_repo(tmp_path: Path, name: str, *, edit_caller: bool) -> Path:
    """A repository whose one change is `f` gaining a parameter, so nothing else in the diff can
    disturb what is being asserted about it. `go` calls `f`; it is edited too when asked."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "m/core.py", "def f(a):\n    return a\n")
    _write(repo, "m/use.py", "from m.core import f\n\n\ndef go():\n    return f(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "m/core.py", "def f(a, b=1):\n    return a\n")
    if edit_caller:
        _write(repo, "m/use.py", "from m.core import f\n\n\ndef go():\n    return f(1, 2)\n")
    _git(repo, "commit", "-qam", "work")
    return repo


def test_a_group_of_resolved_complete_callers_is_labelled_evidence(monkeypatch, tmp_path):
    repo = _solo_repo(tmp_path, "solo", edit_caller=False)
    graph = _Graph()
    graph.edges["f"] = [_edge("f", "m/core.py", "m.use.go", "m/use.py")]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)

    assert "#### `f` — signature: parameters (+b) · m/core.py:1 · [evidence]" in env["result"]
    assert [r["caller_status"] for r in env["rows"]] == ["untouched"]


def test_the_envelope_is_safe_only_when_every_group_is_evidence_and_complete(
        monkeypatch, tmp_path):
    repo = _solo_repo(tmp_path, "solo2", edit_caller=False)
    graph = _Graph()
    graph.edges["f"] = [_edge("f", "m/core.py", "m.use.go", "m/use.py")]
    clean = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert clean["confidence"] == "complete", clean.get("gaps")
    assert clean["evidence"]["safe_for_destructive"] is True
    assert "Safe for destructive" not in clean["result"]            # a clean answer prints no banner

    graph.edges["f"] = [_edge("f", "m/core.py", "m.use.go", "m/use.py", conf="0.38",
                              strategy="suffix_match")]
    guessed = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert guessed["evidence"]["safe_for_destructive"] is False
    assert guessed["confidence"] == "partial"


def test_a_stale_index_alone_is_enough_to_withdraw_safe_for_destructive(monkeypatch, tmp_path):
    """Everything else about this answer is clean — one resolved caller, a whole list — so the
    only thing standing between it and `safe_for_destructive: true` is the index's age."""
    repo = _solo_repo(tmp_path, "solo3", edit_caller=False)
    graph = _Graph()
    graph.edges["f"] = [_edge("f", "m/core.py", "m.use.go", "m/use.py")]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph, index="stale"), repo)
    assert _gap_kinds(env) == ["stale-index"]
    assert env["evidence"]["safe_for_destructive"] is False


# ======================================================================== honest about lookups

def test_a_failed_caller_lookup_is_unknown_never_none(monkeypatch, tmp_path, repo):
    """A backend failure must not read as "no callers". Deleting on that reading is the exact
    mistake the envelope exists to prevent."""
    graph = _standard_graph()
    graph.fail.add("resig")
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    group = env["result"].split("#### `resig`")[1].split("####")[0]
    assert "did not complete" in group and "UNKNOWN, not none" in group
    assert "records no caller" not in group
    assert "callers-unavailable" in _gap_kinds(env) and env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_symbol_the_index_never_saw_is_not_reported_as_having_no_callers(
        monkeypatch, tmp_path, repo):
    """`use_resig` has no node in the canned graph — the index predates it. "Not indexed" and
    "indexed with no callers" license opposite conclusions, so they are told apart."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    use_resig = body.split("#### `use_resig`")[1].split("####")[0].split("###")[0]
    assert "not in the graph index" in use_resig and "UNKNOWN, not none" in use_resig
    rewrite = body.split("#### `rewrite`")[1].split("####")[0].split("###")[0]
    assert "records no caller" in rewrite and "not proof there is none" in rewrite
    kinds = _gap_kinds(env)
    assert "symbol-not-indexed" in kinds and "no-graph-callers" in kinds


def test_a_group_with_no_callers_keeps_the_answer_partial(monkeypatch, tmp_path, repo):
    """Zero recorded callers is not proof of none (framework dispatch, a call through a value), so
    it cannot be the evidence that makes an answer safe."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    assert env["evidence"]["safe_for_destructive"] is False


def test_the_symbol_cap_is_disclosed_and_keeps_the_most_severe_first(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())
    p._RANGE_SYMBOL_CAP = 2
    env = _ask(p, repo)
    body = env["result"]

    assert "symbols-truncated" in _gap_kinds(env) and env["confidence"] == "partial"
    assert "2 of 4 changed symbols were not looked up" in str(env["gaps"])
    assert "### Not looked up (2)" in body
    not_looked = body.split("### Not looked up (2)")[1].split("###")[0]
    assert "`rewrite` (body)" in not_looked and "`use_resig` (body)" in not_looked
    assert "`gone` — removed" in body and "`resig` — signature" in body, \
        "the cap keeps removed, then signature, ahead of body"


def test_the_file_cap_gap_counts_the_files_it_cut(monkeypatch, tmp_path, repo):
    """Two source files changed; a cap of one compares one and names the other as cut — and the
    headline's own file count is the number compared, not the number that changed."""
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())
    p._RANGE_FILE_CAP = 1
    env = _ask(p, repo)
    gap = next(g for g in env["gaps"] if g["kind"] == "files-truncated")
    assert gap["detail"].startswith("1 further source file(s) changed and were not compared")
    assert "across 1 source file(s))" in env["result"]
    assert env["confidence"] == "partial"


def test_a_text_mention_row_never_claims_to_be_verified(monkeypatch, tmp_path, repo):
    """Whatever the file and whatever the label, a `git grep` hit is a name match: it is never
    `verified`, it is never in the `resolved` bucket, and it says which tool found it. Pinned over
    every mention row of a richer answer than the one the first test reads."""
    _write(repo, "pkg/exports.py", '__all__ = ["gone"]\n')
    _write(repo, "web/app.ts", "export const x = gone(1)\n")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    mentions = [r for r in env["rows"] if r["relation"] == "mention"]
    assert {r["file"] for r in mentions} >= {"pkg/legacy.py", "pkg/exports.py", "web/app.ts"}
    for r in mentions:
        assert r["verified"] is False, r
        assert r["evidence"] == "name-matched" and r["strategy"] == "git-grep", r
        assert r["group_class"] == "discovery", r
    assert env["evidence"]["verified"] == sum(1 for r in env["rows"] if r["verified"])
    assert env["evidence"]["possible"] >= len(mentions)


def test_a_symbol_cap_that_is_not_reached_raises_no_gap(monkeypatch, tmp_path, repo):
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    assert "symbols-truncated" not in _gap_kinds(env)
    assert "Not looked up" not in env["result"]


def test_the_row_cap_of_a_lookup_is_forwarded_as_a_gap_and_total_becomes_unknown(
        monkeypatch, tmp_path, repo):
    graph = _standard_graph()
    graph.edges["resig"] = [
        _edge("resig", "pkg/core.py", f"pkg.legacy.caller{i}", "pkg/legacy.py") for i in range(50)]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert "row-cap-reached" in _gap_kinds(env)
    assert env["evidence"]["total"] is None and env["evidence"]["truncated"] is True


def test_rows_past_the_print_limit_are_counted_and_withheld_not_forgotten(
        monkeypatch, tmp_path, repo):
    graph = _standard_graph()
    graph.edges["resig"] = [
        _edge("resig", "pkg/core.py", f"pkg.legacy.caller{i}", "pkg/legacy.py") for i in range(20)]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert "… (+8 more, not shown)" in env["result"]
    assert env["evidence"]["truncated"] is True
    assert env["evidence"]["total"] == env["evidence"]["returned"] + 8


# ====================================================================== language degradation

def test_a_changed_file_in_an_unsupported_language_degrades_to_file_granular(
        monkeypatch, tmp_path, repo):
    """C++ is chunked but its definitions are not named by the indexer, so symbols cannot be
    classified. The file is still reported — and no symbol is invented for it."""
    _write(repo, "native/engine.cpp", "int f() { return 2; }\nint added() { return 3; }\n")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    assert "symbol-diff-unsupported-language" in _gap_kinds(env) and env["confidence"] == "partial"
    assert "### Compared at file granularity only (1)" in body
    assert "`native/engine.cpp` — unsupported-language" in body
    assert "`added`" not in body and "`f`" not in body.split("### Compared")[0]


def test_a_changed_file_that_no_longer_parses_is_reported_and_not_silently_skipped(
        monkeypatch, tmp_path, repo):
    _write(repo, "pkg/also.py", "def use_resig(:\n    broken\n")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    assert "symbol-diff-unparsable" in _gap_kinds(env)
    assert "pkg/also.py" in env["result"] and "does not parse as Python" in env["result"]
    assert "unknown, not none" in str(env["gaps"])


def test_a_file_changed_only_outside_any_definition_is_named_not_swallowed(
        monkeypatch, tmp_path, repo):
    _write(repo, "pkg/legacy.py", "from pkg.core import resig, gone\nLIMIT = 5\n\n\ndef legacy():\n"
                                  "    return resig(2) + gone(3)\n\n\ndef note():\n"
                                  "    # gone is documented elsewhere\n    return 0\n")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    assert "changed outside any definition" in body and "`pkg/legacy.py`" in body
    # …and said as a LIMIT, not only as a note: it is what keeps `safe_for_destructive` false.
    gap = next(g for g in env["gaps"] if g["kind"] == "module-level-not-compared")
    assert "`pkg/legacy.py`" in gap["detail"] and "outside any definition" in gap["detail"], gap
    assert env["evidence"]["safe_for_destructive"] is False


def test_a_symlink_pointing_outside_the_repository_is_never_read_into_an_answer(
        monkeypatch, tmp_path, repo):
    """A committed or untracked symlink can name any file the process can read. The tree is read
    through the same containment every other reader uses, so the target's bytes — and the symbol it
    would have contributed — never reach the answer."""
    outside = tmp_path / "outside.py"
    outside.write_text("def stolen():\n    return 1\n")
    (repo / "pkg" / "escape.py").symlink_to(outside)
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    assert "stolen" not in env["result"]
    assert "symbol-diff-unparsable" in _gap_kinds(env)
    assert "`pkg/escape.py`" in env["result"] and "could not be read" in env["result"]


def test_concurrent_lookups_keep_each_symbols_rows_to_themselves(monkeypatch, tmp_path):
    """The graph lookups run several at a time. Their per-request state is per-thread, which is the
    property that lets them: a row that landed in another symbol's group would put a caller under a
    function it never calls."""
    repo = tmp_path / "many"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    count = 10
    _write(repo, "pkg/core.py", "".join(f"def f{n}(a):\n    return a\n\n\n" for n in range(count)))
    _write(repo, "pkg/use.py", "".join(f"def call{n}():\n    return {n}\n\n\n" for n in range(count)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "pkg/core.py",
           "".join(f"def f{n}(a):\n    return a + {n}\n\n\n" for n in range(count)))
    _git(repo, "commit", "-qam", "rewrite every body")

    graph = _Graph()
    for n in range(count):
        graph.edges[f"f{n}"] = [_edge(f"f{n}", "pkg/core.py", f"pkg.use.call{n}", "pkg/use.py")]
    p = _provider(monkeypatch, tmp_path, repo, graph)
    assert p._RANGE_LOOKUP_WORKERS > 1
    env = _ask(p, repo)

    assert len([r for r in env["rows"] if r["relation"] == "caller"]) == count
    for row in env["rows"]:
        assert row["name"] == row["changed_symbol"].replace("f", "call", 1), row
    for n in range(count):
        group = env["result"].split(f"#### `f{n}`")[1].split("####")[0]
        assert f"call{n} " in group and sum(f"call{m} " in group for m in range(count)) == 1


def test_a_renamed_file_is_not_reported_as_everything_removed_and_added(monkeypatch, tmp_path, repo):
    _git(repo, "mv", "pkg/legacy.py", "pkg/legacy_renamed.py")
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    body = env["result"]
    assert "(1 removed · 1 signature · 2 body · 1 added" in body, "a pure rename adds no symbols"
    assert "`pkg/legacy.py` → `pkg/legacy_renamed.py`" in body
    assert "importing the old module path may break" in body
    assert "`legacy`" not in body.split("### Added")[-1].split("###")[0]


# ============================================================================== index freshness

def test_an_index_older_than_the_changed_files_raises_a_stale_index_gap(
        monkeypatch, tmp_path, repo):
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph(), index="stale"), repo)
    assert "stale-index" in _gap_kinds(env) and env["confidence"] == "partial"
    assert "older tree than the one the symbols were diffed in" in env["result"]
    assert env["evidence"]["safe_for_destructive"] is False


def test_an_index_whose_age_cannot_be_read_says_so_and_does_not_imply_freshness(
        monkeypatch, tmp_path, repo):
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph(), index=None), repo)
    assert "index-age-unknown" in _gap_kinds(env) and "stale-index" not in _gap_kinds(env)
    assert "cannot be called current" in env["result"]


def test_a_fresh_index_raises_neither_staleness_gap(monkeypatch, tmp_path, repo):
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph(), index="fresh"), repo)
    assert "## Changes since" in env["result"], "the negative control must be a real range answer"
    kinds = _gap_kinds(env)
    assert "stale-index" not in kinds and "index-age-unknown" not in kinds


def test_a_plain_commit_after_the_index_does_not_make_it_stale(monkeypatch, tmp_path, repo):
    """A commit records content that was already on disk, so it says nothing about whether the
    index saw it. Counting it flagged every query after every commit."""
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph(), index="fresh")
    # An index written before the commit, but after every file was last touched:
    db = tmp_path / "graph-cache" / f"{PROJECT}.db"
    base = time.time() + 600
    os.utime(db, (base, base))
    for path in repo.rglob("*.py"):
        os.utime(path, (base - 300, base - 300))
    _write(repo, "pkg/extra.py", "")
    os.utime(repo / "pkg/extra.py", (base - 300, base - 300))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a later commit")
    env = _ask(p, repo)
    assert "## Changes since" in env["result"], "the negative control must be a real range answer"
    kinds = _gap_kinds(env)
    assert "stale-index" not in kinds, kinds


# ========================================================================= the answer's shape

@pytest.mark.parametrize("cap", [40, 2])
def test_the_headline_counts_exactly_the_symbols_listed_beneath_it(
        monkeypatch, tmp_path, repo, cap):
    """The headline is a sum and the sections partition the symbols — each is in exactly one — so
    the numbers must agree with what is printed, with the symbol cap reached or not."""
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())
    p._RANGE_SYMBOL_CAP = cap
    body = _ask(p, repo)["result"]
    head = re.search(r"\((\d+) removed · (\d+) signature · (\d+) body · (\d+) added", body)
    total = sum(int(n) for n in head.groups())

    listed = 0
    for heading, n in re.findall(r"(?m)^### (.+?) \((\d+)\)$", body):
        section = body.split(f"### {heading} ({n})", 1)[1].split("\n### ", 1)[0]
        if heading.startswith(("Removed — and", "Signature", "Body")):
            members = len(re.findall(r"(?m)^#### `", section))
        elif heading.startswith("Removed — nothing"):
            members = len(re.findall(r"(?m)^\* `", section))
        elif heading.startswith(("Added", "Not looked up")):
            members = len(re.findall(r"`[^`]+`", section.split("\n", 2)[1]))
        else:
            continue                          # file-granular files are not symbols
        assert members == int(n), (heading, section)
        listed += members
    assert listed == total, body


def test_every_dash_line_in_the_body_is_a_published_row_in_the_order_printed(
        monkeypatch, tmp_path, repo):
    """`rows[]` is the body's `- ` lines, line for line — which is what lets an integration filter
    on the fields instead of parsing the prose."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    lines = [ln for ln in env["result"].splitlines() if ln.startswith("- ")]
    rows = env["rows"]
    assert len(lines) == len(rows) == env["evidence"]["returned"]
    for line, row in zip(lines, rows, strict=True):
        assert row["file"] in line, (line, row)


def test_the_first_screen_states_the_verdict_above_the_heading_when_the_answer_is_partial(
        monkeypatch, tmp_path, repo):
    body = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)["result"]
    assert body.startswith("> **Confidence: partial**"), body[:200]
    assert body.index("Safe for destructive decisions") < body.index("## Changes since")


def test_a_limit_is_stated_in_the_body_as_well_as_in_the_gaps_field(monkeypatch, tmp_path, repo):
    """`gaps` is what an integration branches on; the body is what an agent reads. Neither may be
    the only place a limit appears."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    for kind in _gap_kinds(env):
        assert f"**{kind}**" in env["result"], kind
    assert "### Limits of this answer" in env["result"]


def test_no_non_row_line_in_the_body_starts_with_a_dash(monkeypatch, tmp_path, repo):
    """A bench scorer reads every `- ` line as a result row. Prose in that shape would be scored
    as a fabricated caller."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    for line in env["result"].splitlines():
        if line.startswith("- "):
            assert re.match(r"- (\S+ \[|\S+:\d+  `|module scope)", line), line


# ============================================================== what was NOT compared, said as gaps
#
# `safe_for_destructive` is "no gap, no unverified row, nothing withheld". A category the op simply
# does not compare raised no gap, so an answer that never looked at it could still be `complete`.

def _branch(tmp_path: Path, name: str, base: dict[str, str], work: dict[str, str | None]) -> Path:
    """A repository on `main` holding `base`, then a `work` branch with one commit applying `work`
    (a value of None deletes the file)."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in base.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "work")
    for rel, text in work.items():
        if text is None:
            (repo / rel).unlink()
        else:
            _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "work")
    return repo


def test_a_branch_that_only_changes_a_file_this_op_cannot_read_is_not_reported_as_inert(
        monkeypatch, tmp_path):
    """Scenario A of the review. `app/user.rb` is a language with no definition-level reading and no
    place in the source list, so the answer used to read "no source file differs" and `confidence:
    complete` — which a reader takes for "this branch is inert"."""
    repo = _branch(tmp_path, "ruby", {"app/user.rb": "def greet\n  1\nend\n"},
                   {"app/user.rb": "def greet\n  2\nend\n", "docs/notes.md": "# notes\n"})
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)
    body = env["result"]

    assert "no source file differs" not in body, body
    assert "NOT compared" in body and "`app/user.rb`" in body and "`docs/notes.md`" in body, body
    assert "not a statement that nothing changed" in body, body
    gap = next(g for g in env["gaps"] if g["kind"] == "non-source-changes-not-compared")
    assert "2 changed file(s)" in gap["detail"] and "`app/user.rb`" in gap["detail"], gap
    assert env["confidence"] == "partial"
    assert "**non-source-changes-not-compared**" in body, "a limit is stated in the body as well"


def test_a_tree_with_no_change_at_all_still_says_nothing_differs_and_raises_no_gap(
        monkeypatch, tmp_path):
    repo = _branch(tmp_path, "inert", {"m/core.py": "def f():\n    return 1\n"}, {})
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)
    assert "no file differs between the merge-base" in env["result"], env["result"]
    assert "NOT compared" not in env["result"]
    assert env.get("gaps", []) == [] and env["confidence"] == "complete"


def test_non_source_files_are_named_up_to_five_and_the_rest_are_counted(monkeypatch, tmp_path):
    work = {f"conf/c{i}.yaml": f"k: {i}\n" for i in range(7)}
    repo = _branch(tmp_path, "many-config", {"m/core.py": "def f():\n    return 1\n"}, work)
    gap = next(g for g in _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)["gaps"]
               if g["kind"] == "non-source-changes-not-compared")
    assert gap["detail"].startswith("7 changed file(s)"), gap
    assert gap["detail"].count("`conf/c") == 5 and "+2 more" in gap["detail"], gap


def test_a_rename_with_a_changed_symbol_does_not_leave_the_answer_safe(monkeypatch, tmp_path):
    """Scenario B. `a.py` becomes `b.py` and `f` is rewritten, with one verified caller. Importers of
    the OLD module path were never asked about, so a clean caller list cannot make this safe."""
    core = "".join(f"def g{i}(a):\n    return a + {i}\n\n\n" for i in range(12))
    repo = _branch(
        tmp_path, "renamed",
        {"m/a.py": core + "def f(a):\n    return a\n",
         "m/use.py": "from m.a import f\n\n\ndef go():\n    return f(1)\n"},
        {"m/a.py": None, "m/b.py": core + "def f(a):\n    return a + 1\n"})
    graph = _Graph()
    graph.edges["f"] = [_edge("f", "m/b.py", "m.use.go", "m/use.py")]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)

    assert "#### `f` — body" in env["result"] and "[evidence]" in env["result"], env["result"]
    gap = next(g for g in env["gaps"] if g["kind"] == "renamed-module-importers-unchecked")
    assert "`m/a.py` → `m/b.py`" in gap["detail"] and "Importers of the old module path" in gap["detail"]
    assert env["confidence"] == "partial"
    assert env["evidence"]["safe_for_destructive"] is False, env["evidence"]


def test_a_branch_with_a_changed_symbol_and_nothing_unchecked_is_still_safe(monkeypatch, tmp_path):
    """The negative control for the three gaps above: they are raised when the count is above zero
    and not otherwise, so a plain in-place edit with a verified caller keeps its clean answer."""
    repo = _solo_repo(tmp_path, "control", edit_caller=False)
    graph = _Graph()
    graph.edges["f"] = [_edge("f", "m/core.py", "m.use.go", "m/use.py")]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)
    assert env.get("gaps", []) == [] and env["evidence"]["safe_for_destructive"] is True


_MANIFEST = '[project.scripts]\nrun = "pkg.core:gone"\n'


def test_a_removed_function_still_named_in_a_manifest_is_reported_not_dropped(
        monkeypatch, tmp_path):
    """`git grep` found `gone` in `pyproject.toml` and the hit was thrown away because the file is not
    source — uncounted. A removed function named in an entry-point table fails at runtime."""
    repo = _branch(
        tmp_path, "manifest",
        {"pkg/core.py": "def f():\n    return 1\n\n\ndef gone():\n    return 2\n",
         "pyproject.toml": _MANIFEST, "conf/app.yaml": "handler: pkg.core.gone\n"},
        {"pkg/core.py": "def f():\n    return 1\n"})
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)
    body = env["result"]

    gap = next(g for g in env["gaps"] if g["kind"] == "mentions-outside-source")
    assert "`conf/app.yaml`" in gap["detail"] and "`pyproject.toml`" in gap["detail"], gap
    assert "`gone`" in gap["detail"]
    assert "### Removed — and still mentioned (1)" in body, body
    assert "nothing else mentions the name" not in body, body
    assert "`pyproject.toml`" in body and "fails at runtime" in body
    assert env["evidence"]["safe_for_destructive"] is False if env.get("evidence") else True
    assert env["confidence"] == "partial"


def test_the_count_of_files_a_removed_name_survives_in_equals_the_files_it_names(
        monkeypatch, tmp_path):
    """The line says "N file(s)" and then lists some. N is the files that were found, the list is cut
    at five and says how many it left out, so the two must add up — and the gap, which counts the
    same files for the envelope, must give the same five."""
    config = {f"conf/c{i}.yaml": "handler: gone\n" for i in range(7)}
    repo = _branch(
        tmp_path, "seven-configs",
        {"pkg/core.py": "def f():\n    return 1\n\n\ndef gone():\n    return 2\n", **config},
        {"pkg/core.py": "def f():\n    return 1\n"})
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)

    line = re.search(r"written in (\d+) file\(s\) this op does not read as code: (.+?) — an entry",
                     env["result"])
    assert line, env["result"]
    listed = re.findall(r"`([^`]+)`", line.group(2))
    more = re.search(r"\(\+(\d+) more\)", line.group(2))
    assert int(line.group(1)) == 7 == len(listed) + (int(more.group(1)) if more else 0), line.group(0)
    assert len(listed) == 5
    gap = next(g for g in env["gaps"] if g["kind"] == "mentions-outside-source")
    assert all(f"`{p}`" in gap["detail"] for p in listed) and "(+2 more)" in gap["detail"], gap


def test_a_removed_function_named_nowhere_outside_source_raises_no_such_gap(
        monkeypatch, tmp_path):
    repo = _branch(
        tmp_path, "no-manifest",
        {"pkg/core.py": "def f():\n    return 1\n\n\ndef gone():\n    return 2\n"},
        {"pkg/core.py": "def f():\n    return 1\n"})
    env = _ask(_provider(monkeypatch, tmp_path, repo, _Graph()), repo)
    assert "mentions-outside-source" not in _gap_kinds(env)
    assert "### Removed — nothing else mentions the name (1)" in env["result"]


def test_the_files_a_name_survives_in_are_listed_configuration_first(tmp_path):
    from codeintel.changed_range import grep_mentions

    repo = _branch(
        tmp_path, "order",
        {"pkg/core.py": "def f():\n    return 1\n", "README.md": "call gone\n",
         "pyproject.toml": _MANIFEST, "conf/app.yaml": "handler: gone\n", "x.py": "gone()\n"},
        {})
    found = grep_mentions(str(repo), "gone")
    assert found.outside == ("conf/app.yaml", "pyproject.toml", "README.md"), found.outside
    assert [m.path for m in found.hits] == ["x.py"], "a source hit is still a hit and not an outside file"


# ===================================================== a lower bound is not stated as a total

def test_a_caller_list_cut_by_the_distinct_cap_does_not_state_the_kept_rows_as_its_total(
        monkeypatch, tmp_path):
    """`f` has 120 distinct callers and `callers` keeps 50. Its own gap says "120 distinct callers
    exist and 50 are shown" — and the envelope said `evidence.total: 50`, because the 70 it had
    counted and set aside never reached this op's total. 120 is known, so 120 is what is said; the
    one figure it may not state is 50."""
    from tests.test_edge_endpoint_cap import _EdgeBackend

    repo = _solo_repo(tmp_path, "solo-many", edit_caller=False)
    p = _provider(monkeypatch, tmp_path, repo, _Graph())
    backend = _EdgeBackend(
        [_edge("f", "m/core.py", f"m.use.go{i}", f"m/use{i}.py") for i in range(120)])
    monkeypatch.setattr(p, "_query_rows", lambda c, proj, t: backend(c, proj, t) if "-[" in c else [])
    env = _ask(p, repo)

    assert "120 distinct callers exist and 50 are shown" in str(env["gaps"]), env["gaps"]
    ev = env["evidence"]
    assert ev["total"] in (None, 120), ev
    assert ev["total"] != 50 and ev["truncated"] is True and ev["safe_for_destructive"] is False, ev
    assert "50 in total" not in env["result"], env["result"][:300]
    group = env["result"].split("#### `f`")[1]
    assert "[advisory]" in group.split("\n")[0], "a group with rows withheld is not `evidence`"


def test_a_truncated_text_search_makes_the_total_unknown_and_not_the_rows_that_fit(
        monkeypatch, tmp_path, repo):
    """The text half of the same defect: a `git grep` cut at its cap knows a lower bound, and the
    envelope stated the rows that fit as the total."""
    from codeintel.changed_range import grep_mentions

    monkeypatch.setattr("codeintel.graph_changed.grep_mentions",
                        lambda root, name: grep_mentions(root, name, cap=1))
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)

    assert "mentions-truncated" in _gap_kinds(env)
    ev = env["evidence"]
    assert ev["total"] is None and ev["truncated"] is True, ev
    assert "total unknown" in env["result"].split("## Changes since")[0]


def test_git_grep_output_is_read_up_to_a_bound_and_no_further(monkeypatch, tmp_path):
    """A name that is also a word in a vendored file matches without limit. `subprocess.run` buffers
    all of stdout and waits for git to finish; this reads a bounded amount and stops git. The stand-in
    git writes more than the bound and then never exits, so an implementation that waits for it is
    stopped by the timeout instead — and reports an error where this one reports a cut list."""
    import sys

    from codeintel import changed_range

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "line = b'pkg/a.py\\x001\\x00gone(1)\\n'\n"
        "sys.stdout.buffer.write(line * 100_000)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(60)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(changed_range, "_GREP_OUTPUT_CAP_BYTES", 200_000, raising=False)
    monkeypatch.setattr(changed_range, "_GIT_TIMEOUT_S", 5)

    started = time.monotonic()
    found = changed_range.grep_mentions(str(tmp_path), "gone")

    assert found.error == "", found.error
    assert found.truncated is True and len(found.hits) == 200, (found.truncated, len(found.hits))
    assert time.monotonic() - started < 4, "it read a bounded amount; it did not wait for git to end"


# ================================================ git is a bystander: nothing the repository names runs

def test_a_repository_configured_fsmonitor_command_is_never_run(monkeypatch, tmp_path, repo):
    """`core.fsmonitor` names a command git runs on `diff` and `ls-files`. A branch under review is
    input, and its `.git/config` is part of it — so every git call here overrides the setting."""
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '\\0'\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _write(repo, "pkg/untracked.py", "def u():\n    return 1\n")          # makes `ls-files` do work
    marker.unlink(missing_ok=True)

    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)

    assert env["result"] is not None and "## Changes since" in env["result"], env
    assert not marker.exists(), "git ran the repository's fsmonitor command"


def test_a_failed_listing_of_untracked_files_is_a_gap_and_not_a_silent_omission(
        monkeypatch, tmp_path, repo):
    from codeintel import changed_range

    real = changed_range._git

    def flaky(root, *args):
        if args[0] == "ls-files":
            return 128, b"", "fatal: unable to read the index"
        return real(root, *args)

    monkeypatch.setattr(changed_range, "_git", flaky)
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)

    gap = next(g for g in env["gaps"] if g["kind"] == "untracked-files-unknown")
    assert "unable to read the index" in gap["detail"] and "not yet added" in gap["detail"], gap
    assert env["confidence"] == "partial" and env["evidence"]["safe_for_destructive"] is False
    assert "untracked-files-unknown" in env["result"]


def test_resolving_a_base_does_not_ask_git_about_shallowness_when_it_succeeds(monkeypatch, repo):
    """The answer was stored and never read, at the price of one more `git` process per call."""
    from codeintel import changed_range

    seen: list[tuple[str, ...]] = []
    real = changed_range._git
    monkeypatch.setattr(changed_range, "_git", lambda root, *a: (seen.append(a), real(root, *a))[1])

    base = changed_range.resolve_base(str(repo), "main")

    assert isinstance(base, changed_range.RangeBase), base
    assert not any("--is-shallow-repository" in a for a in seen), seen


def test_a_root_outside_any_work_tree_is_not_told_to_run_an_op_that_cannot_work_there(
        monkeypatch, tmp_path):
    """No-target `changed` asks the backend's `detect_changes`, which asks git. Against an indexed
    directory with no `.git` it reports ZERO changed files after an edit, so the hint that offered
    it as the alternative sent the reader to an op that answers wrongly."""
    plain = tmp_path / "plain"
    plain.mkdir()
    env = _ask(_provider(monkeypatch, tmp_path, plain, _Graph()), plain, "main")
    assert env["reason"] == "not-a-git-repo"
    assert "still reports uncommitted edits" not in env["hint"], env["hint"]
    assert "git checkout" in env["hint"], env["hint"]


# ======================================================================= what each symbol's gap says

def test_a_merged_gap_gives_each_symbol_its_own_detail_and_not_the_first_ones(
        monkeypatch, tmp_path, repo):
    """One gap per kind, but "1 of 2 rows were resolved by suffix match" under a heading that names
    `resig` AND `rewrite` states a figure about `rewrite` that was only measured for `resig`."""
    graph = _standard_graph()
    graph.edges["resig"][1] = _edge("resig", "pkg/core.py", "pkg.legacy.legacy", "pkg/legacy.py",
                                    conf="0.38", strategy="suffix_match")
    graph.edges["rewrite"] = [
        _edge("rewrite", "pkg/core.py", f"pkg.legacy.caller{i}", "pkg/legacy.py",
              conf="0.38", strategy="suffix_match") for i in range(3)]
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)

    merged = [g for g in env["gaps"] if g["kind"] == "low-confidence-edges"]
    assert len(merged) == 1, merged
    detail = merged[0]["detail"]
    assert "`resig`: 1 of 2 row(s)" in detail, detail
    assert "`rewrite`: 3 of 3 row(s)" in detail, detail
    assert "for `resig`, `rewrite`" not in detail, "the first symbol's figures under every name"


def test_unavailable_lookups_each_keep_their_own_reason(monkeypatch, tmp_path, repo):
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())

    def rows(cypher, project, timeout_ms):
        if "-[" in cypher:
            name = re.search(r'b\.name="([^"]+)"', cypher).group(1)
            p._backend._last_failure = Missing("timeout", f"timed out asking for {name}")
        return []

    monkeypatch.setattr(p, "_query_rows", rows)
    gap = next(g for g in _ask(p, repo)["gaps"] if g["kind"] == "callers-unavailable")
    assert "`resig`: timed out asking for resig" in gap["detail"], gap
    assert "`rewrite`: timed out asking for rewrite" in gap["detail"], gap


def test_a_refused_caller_lookup_carries_the_fix_not_only_the_message(monkeypatch, tmp_path, repo):
    """`callers` puts the refusal AND the way past it in its hint. The gap on `changed <ref>` quoted
    the message and stopped, so the one op an agent runs on a branch was the one that could not say
    what to do about a coordination lock."""
    from codeintel.graph_backend import _refused

    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())
    refusal = _refused(1, "CBM CLI could not start because a pre-coordination or unverified CBM "
                          "generation is active; close all CBM sessions and commands, then retry")

    def rows(cypher, project, timeout_ms):
        if "-[" in cypher:
            p._backend._last_failure = refusal
        return []

    monkeypatch.setattr(p, "_query_rows", rows)
    env = _ask(p, repo)

    gap = next(g for g in env["gaps"] if g["kind"] == "callers-unavailable")
    assert "pre-coordination" in gap["detail"] and "to fix: close every codebase-memory-mcp" in gap["detail"]
    assert "cbm-daemon" in gap["detail"], gap
    group = env["result"].split("#### `resig`")[1].split("####")[0]
    assert "cbm-daemon" in group, "the group says it too, not only the gaps field"


def test_partial_rows_with_a_failed_call_still_state_the_failure(monkeypatch, tmp_path, repo):
    """The probe came back full (so there ARE rows) and the follow-up that would have counted them
    failed. The group carried `row-cap-reached` — a symptom — and nothing about the call that failed."""
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph())
    edges = [_edge("resig", "pkg/core.py", f"pkg.legacy.c{i}", "pkg/legacy.py") for i in range(50)]

    def rows(cypher, project, timeout_ms):
        if "-[" not in cypher:
            return []
        if "count(*)" in cypher:
            p._backend._last_failure = Missing("timeout", "the count query timed out")
            return []
        return [dict(e) for e in edges] if 'b.name="resig"' in cypher else []

    monkeypatch.setattr(p, "_query_rows", rows)
    env = _ask(p, repo)

    kinds = _gap_kinds(env)
    assert "row-cap-reached" in kinds and "callers-incomplete" in kinds, kinds
    gap = next(g for g in env["gaps"] if g["kind"] == "callers-incomplete")
    assert "the count query timed out" in gap["detail"] and "`resig`" in gap["detail"], gap
    assert env["evidence"]["safe_for_destructive"] is False


# ================================================================== the badge `callers` prints

def test_a_downgraded_same_module_caller_keeps_its_qualified_call_badge(monkeypatch, tmp_path, repo):
    """`callers` prints `[?0.90 qualified call]` for a `same_module` edge whose call is written
    through a receiver. `changed` re-rendered the row from its fields without that verdict and
    printed `[?0.90]` — the backend's high score behind a bare question mark."""
    graph = _standard_graph()
    edge = _edge("resig", "pkg/core.py", "pkg.legacy.legacy", "pkg/legacy.py",
                 conf="0.90", strategy="same_module")
    edge["callee"] = "subprocess.resig"
    graph.edges["resig"][1] = edge
    env = _ask(_provider(monkeypatch, tmp_path, repo, graph), repo)

    group = env["result"].split("#### `resig`")[1].split("####")[0]
    legacy = next(ln for ln in group.splitlines() if "legacy" in ln)
    assert "[?0.90 qualified call]" in legacy, legacy
    row = next(r for r in env["rows"] if r["name"] == "legacy" and r["changed_symbol"] == "resig")
    assert row["verified"] is False and row["evidence"] == "name-matched", row


# ================================================================== what the index looked like

def test_the_index_age_is_read_before_the_lookups_can_touch_the_index(monkeypatch, tmp_path, repo):
    """The age is the db file's mtime, and the lookups are backend processes that open that file. If
    opening it moves the mtime, a stamp taken afterwards says "just now" for every index and
    `stale-index` can never fire. The fake's lookups touch the file, as such a backend would."""
    p = _provider(monkeypatch, tmp_path, repo, _standard_graph(), index="stale")
    db = tmp_path / "graph-cache" / f"{PROJECT}.db"
    inner = p._query_rows

    def touching(cypher, project, timeout_ms):
        os.utime(db, None)
        return inner(cypher, project, timeout_ms)

    monkeypatch.setattr(p, "_query_rows", touching)
    env = _ask(p, repo)

    assert "stale-index" in _gap_kinds(env), env.get("gaps")
    assert env["evidence"]["safe_for_destructive"] is False


# ================================================================== the rest of what was promised

def test_a_removed_group_counts_the_comments_it_set_aside(monkeypatch, tmp_path, repo):
    """docs/graph.md promises comments and definitions are "set aside and counted". The count was
    printed only for symbols with no live mention at all."""
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    group = env["result"].split("#### `gone`")[1].split("####")[0].split("\n### ")[0]
    assert "1 comment(s) or definition(s) that match the name were set aside" in group, group


def test_an_unreadable_file_message_quotes_the_size_limit_the_reader_actually_enforces(
        monkeypatch, tmp_path, repo):
    monkeypatch.setattr("codeintel.graph_changed.MAX_SOURCE_BYTES", 3_000_000)
    outside = tmp_path / "outside.py"
    outside.write_text("def stolen():\n    return 1\n")
    (repo / "pkg" / "escape.py").symlink_to(outside)
    env = _ask(_provider(monkeypatch, tmp_path, repo, _standard_graph()), repo)
    assert "larger than 3 MB" in env["result"], env["result"]


# ================================================================ the qualifier scan, carried through

def _svc_provider(monkeypatch, tmp_path, repo: Path, callers: list[dict]) -> GraphProvider:
    """`Svc.run` rewritten in the working tree, with *callers* recorded against it.

    The fake backend answers the one class-lookup the method target triggers (so the answer is not
    qualified by a lookup that fell over), and nothing else about the hierarchy."""
    (repo / "pkg" / "core.py").write_text(
        (repo / "pkg" / "core.py").read_text().replace("    def run(self, x):\n        return x\n",
                                                       "    def run(self, x):\n        return x + 1\n"))
    graph = _Graph()
    graph.edges["run"] = callers
    p = _provider(monkeypatch, tmp_path, repo, graph)
    served = p._query_rows

    def rows(cypher, project, timeout_ms):
        if "[:DEFINES_METHOD]" in cypher and 'm.name="run"' in cypher:
            return [{"c.qualified_name": f"{PROJECT}.pkg.core.Svc", "c.name": "Svc",
                     "c.file_path": "pkg/core.py", "c.base_classes": None,
                     "m.qualified_name": f"{PROJECT}.pkg.core.Svc.run", "m.file_path": "pkg/core.py"}]
        if "[:DEFINES_METHOD]" in cypher and "m.qualified_name IN" in cypher:
            # The qualifier scan asks which class defines the method it is about to scan on: a node
            # labelled `Method` is not by itself a class's member.
            return [{"c.qualified_name": f"{PROJECT}.pkg.core.Svc", "c.name": "Svc",
                     "c.file_path": "pkg/core.py", "c.base_classes": None,
                     "m.qualified_name": f"{PROJECT}.pkg.core.Svc.run"}]
        if "[:DEFINES_METHOD]" in cypher or "INHERITS" in cypher or "(p:Class)" in cypher:
            return []
        return served(cypher, project, timeout_ms)

    monkeypatch.setattr(p, "_query_rows", rows)
    return p


def _run_edge(caller_qn: str, caller_file: str, **kw) -> dict:
    """An edge into `Svc.run`, name-matched by default, in the shape the real backend returns."""
    row = _edge("run", "pkg/core.py", caller_qn, caller_file, conf="0.55", strategy="suffix_match", **kw)
    row["b.qualified_name"] = f"{PROJECT}.pkg.core.Svc.run"
    row["labels(b)"] = '["Method"]'
    row["callee"] = "svc.run"
    return row


def test_changed_carries_the_qualifier_scan_through_and_shows_the_label_callers_shows(
        monkeypatch, tmp_path, repo):
    """`changed <ref>` reuses `callers`' rows by the fields `rows[]` publishes, so the scan has to
    travel in them — and `changed` has no note to say it in, so the label on the line is the only
    place its reader meets the verdict. It is the same label, from the same function, as `callers`.

    The lookups run on a thread pool, and the root they read is per-thread state a worker does not
    inherit: without it every worker scans nothing and `changed` says less than the identical
    `callers` answer. The split into callers the diff touched and those it did not is untouched by
    any of this."""
    _write(repo, "pkg/svc_user.py",
           "from pkg.core import Svc\n\n\ndef fresh_user():\n    return Svc().run(1)\n")
    callers = [
        _run_edge("pkg.legacy.legacy", "pkg/legacy.py"),            # untouched; never writes `Svc`
        _run_edge("pkg.also.use_resig", "pkg/also.py"),             # edited in this diff; never writes it
        _run_edge("pkg.svc_user.fresh_user", "pkg/svc_user.py"),    # added in this diff; writes it
    ]
    env = _ask(_svc_provider(monkeypatch, tmp_path, repo, callers), repo)
    group = env["result"].split("#### `Svc.run`")[1].split("\n####")[0]
    rows = {r["name"]: r for r in env["rows"] if r["changed_symbol"] == "Svc.run"}

    assert rows["legacy"]["caller_status"] == "untouched", rows
    assert rows["use_resig"]["caller_status"] == "also-changed", rows
    assert rows["fresh_user"]["caller_status"] == "also-changed", rows
    assert (rows["legacy"]["qualifier_seen"], rows["use_resig"]["qualifier_seen"],
            rows["fresh_user"]["qualifier_seen"]) == (False, False, True), rows
    assert rows["legacy"]["qualifier"] == "Svc", rows["legacy"]
    legacy_line = next(ln for ln in group.splitlines() if "pkg.legacy.legacy" in ln)
    fresh_line = next(ln for ln in group.splitlines() if "fresh_user" in ln)
    assert "[?0.55] [never writes `Svc`]" in legacy_line, legacy_line
    assert "never writes" not in fresh_line, fresh_line
    assert env["evidence"]["qualifier_absent"] == 2 and env["evidence"]["qualifier_present"] == 1
    assert len([ln for ln in env["result"].splitlines() if ln.startswith("- ")]) == len(env["rows"])


def test_a_group_that_prints_a_never_writes_line_prints_the_caveat_and_the_command_once(
        monkeypatch, tmp_path, repo):
    """`changed` has no `Checked:` note, so a `[never writes …]` mark on one of its lines would be a
    mark with its qualification left off. The group that prints one carries the caveat beneath its
    rows — once per group, however many rows are marked — with the command that reproduces it. A group
    with no marked row carries nothing."""
    _write(repo, "pkg/other.py", "def third():\n    return 1\n")
    callers = [
        _run_edge("pkg.legacy.legacy", "pkg/legacy.py"),
        _run_edge("pkg.also.use_resig", "pkg/also.py"),
        _run_edge("pkg.other.third", "pkg/other.py"),
    ]
    env = _ask(_svc_provider(monkeypatch, tmp_path, repo, callers), repo)
    group = env["result"].split("#### `Svc.run`")[1].split("\n####")[0]

    assert [r["qualifier_seen"] for r in env["rows"] if r["changed_symbol"] == "Svc.run"] == [False] * 3
    assert group.count("[never writes `Svc`]") == 3, group
    assert group.count("_Rows marked `[never writes …]`") == 1, group
    assert "a text search of the files as they are on disk" in group, group
    assert "an instance the file gets from elsewhere" in group, group
    assert "rg -n --fixed-strings -- 'Svc' " in group, group
    assert not [ln for ln in group.splitlines() if ln.startswith("- ") and "_Rows marked" in ln]
    assert len([ln for ln in env["result"].splitlines() if ln.startswith("- ")]) == len(env["rows"])


def test_changed_scans_nothing_below_the_floor_and_so_prints_no_mark_and_no_caveat(
        monkeypatch, tmp_path, repo):
    """`callers` scans only when its note will be printed (three name-matched rows, at least half the
    answer), and `changed` reuses those rows, so a group with two callers carries neither a mark nor
    the caveat for one. CONTROL: the same symbol with three is marked and carries the caveat."""
    two = [_run_edge("pkg.legacy.legacy", "pkg/legacy.py"),
           _run_edge("pkg.also.use_resig", "pkg/also.py")]
    env = _ask(_svc_provider(monkeypatch, tmp_path, repo, two), repo)
    group = env["result"].split("#### `Svc.run`")[1].split("\n####")[0]

    assert [r["qualifier_seen"] for r in env["rows"] if r["changed_symbol"] == "Svc.run"] == [None] * 2
    assert "never writes" not in group and "_Rows marked" not in group, group
    assert env["evidence"]["qualifier_absent"] == 0, env["evidence"]


def test_changed_shares_one_scan_budget_across_its_workers(monkeypatch, tmp_path, repo):
    """The scan's reading time is one allowance per ANSWER. `changed` runs its lookups on a pool, each
    calling `callers`, which scans — so the allowance is created once, on the op, and handed to every
    worker, or forty symbols would each get the whole of it. The worker is another thread, so what it
    passes the scan is the proof it received the op's budget and not a fresh one."""
    from codeintel import qualifier_scan

    given: list[Any] = []
    real = qualifier_scan.files_naming

    def spy(root, token, files, *, budget=None):
        given.append(budget)
        return real(root, token, files, budget=budget)

    monkeypatch.setattr(qualifier_scan, "files_naming", spy)
    for i in range(3):
        _write(repo, f"pkg/m{i}.py", f"def c{i}():\n    return {i}\n")
    callers = [_run_edge(f"pkg.m{i}.c{i}", f"pkg/m{i}.py") for i in range(3)]
    provider = _svc_provider(monkeypatch, tmp_path, repo, callers)
    env = _ask(provider, repo)

    assert [r["qualifier_seen"] for r in env["rows"] if r["changed_symbol"] == "Svc.run"] == [False] * 3
    assert given and all(b is not None for b in given), given
    assert all(b is provider._scan_budget for b in given), (given, provider._scan_budget)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_at_a_tracked_path_is_not_read_and_cannot_hang_the_reader(tmp_path):
    """`read_new` goes through `contained_path`, which checks where a file is and how many links it
    has — not what it is. A FIFO planted at a tracked path has one link and a size of zero, so it
    passed both, and `open` on it blocks until a writer appears. Only a regular file is opened.
    CONTROL: a regular file in the same directory is read."""
    import threading

    from codeintel.changed_range import read_new

    (tmp_path / "plain.py").write_text("x = 1\n")
    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    out: dict[str, Any] = {}

    def run() -> None:
        out["pipe"] = read_new(str(tmp_path), "pipe.py")
        out["plain"] = read_new(str(tmp_path), "plain.py")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(3.0)
    if thread.is_alive():
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))   # let the stuck reader go
        except OSError:
            pass
    assert not thread.is_alive(), "read_new blocked on a FIFO"
    assert out == {"pipe": None, "plain": "x = 1\n"}, out
