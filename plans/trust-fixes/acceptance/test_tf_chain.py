"""Story: an agent tracing a path. `chain "A -> B"` honours B.

Driven through `GraphProvider.build_result("chain", ...)`; the fake backend answers `trace_path`,
`search_graph` and `query_graph` over the same in-memory graph, so whichever the engine walks, the
call graph is the one below:

    entry -> load -> Cache.save          (pkg/b.py)
    entry -> log
    other -> helper2
    Store.save                           (pkg/a.py, reached by nothing)
"""
from __future__ import annotations

import re

from _fakegraph import Repo, gap_kinds


def _repo(tmp_path, monkeypatch, *, strategy: str | None = "import_map",
          trace_strategy: bool = False) -> Repo:
    repo = Repo(tmp_path, monkeypatch)
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/a.py",
               "class Store:\n    def save(self, v):\n        return v\n\n\n"
               "def helper2():\n    return 2\n\n\n"
               "def other():\n    return helper2()\n")
    repo.write("pkg/b.py",
               "class Cache:\n    def save(self, v):\n        return v\n\n\n"
               "def log(m):\n    return m\n\n\n"
               "def load():\n    return Cache().save(1)\n\n\n"
               "def entry():\n    log('x')\n    return load()\n")
    repo.commit()
    g = repo.graph
    g.trace_strategy = trace_strategy
    entry = g.node("pkg.b.entry", "pkg/b.py")
    load = g.node("pkg.b.load", "pkg/b.py")
    log = g.node("pkg.b.log", "pkg/b.py")
    cache_save = g.node("pkg.b.Cache.save", "pkg/b.py", "Method")
    g.node("pkg.a.Store.save", "pkg/a.py", "Method")
    other = g.node("pkg.a.other", "pkg/a.py")
    helper2 = g.node("pkg.a.helper2", "pkg/a.py")
    conf = "0.95" if strategy else None
    g.edge(entry, load, strategy=strategy, confidence=conf)
    g.edge(entry, log, strategy=strategy, confidence=conf)
    g.edge(load, cache_save, strategy=strategy, confidence=conf)
    g.edge(other, helper2, strategy=strategy, confidence=conf)
    repo.fresh_index()
    return repo


# Spec: `chain "A -> B"` returns `## Path A → B` with the hops in order when B is reachable within
# 5 hops.
# Happy path 3: The agent asks `chain "entry -> Cache.save"` and gets the ordered path.
def test_chain_to_a_reachable_destination_returns_the_ordered_path(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    env = repo.ask("chain", "entry -> Cache.save")
    text = str(env.get("result") or "")
    assert "## Path entry → Cache.save" in text, text
    body = text.split("## Path entry → Cache.save", 1)[1]
    positions = [body.find(name) for name in ("pkg.b.load", "pkg.b.Cache.save")]
    assert -1 not in positions and positions == sorted(positions), body
    assert "pkg.b.log" not in body, "a hop off the path is listed as part of it"


# Spec: When B is not reachable it says "No call path from A to B within N hops (this is not proof
# none exists)" and has a `no-path-found` gap (partial).
# Happy path 3: ... or "no call path" with a gap.
def test_chain_to_an_unreachable_destination_says_no_call_path_with_a_gap(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    env = repo.ask("chain", "entry -> helper2")
    text = str(env.get("result") or "")
    assert re.search(r"No call path from entry to helper2 within \d+ hops "
                     r"\(this is not proof none exists\)", text), text
    assert "no-path-found" in gap_kinds(env), gap_kinds(env)
    assert env.get("confidence") == "partial"


# Spec: When B does not exist the answer names B as not found (`reason: target-not-found`).
def test_chain_to_a_destination_that_does_not_exist_is_target_not_found(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    env = repo.ask("chain", "entry -> nonexistent_zzz")
    assert env.get("reason") == "target-not-found", env
    said = f"{env.get('hint') or ''} {env.get('result') or ''}"
    assert "nonexistent_zzz" in said, env


# Spec: A hop with no `strategy` prints `[unstated]` and adds a `hop-resolution-unstated` gap
# (partial).
def test_chain_hop_without_a_strategy_is_marked_unstated_with_a_gap(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, strategy=None, trace_strategy=False)
    env = repo.ask("chain", "entry -> Cache.save")
    text = str(env.get("result") or "")
    hops = [ln for ln in text.splitlines() if "pkg.b.load" in ln or "pkg.b.Cache.save" in ln]
    assert hops and all("[unstated]" in ln for ln in hops), text
    assert "hop-resolution-unstated" in gap_kinds(env), gap_kinds(env)
    assert env.get("confidence") == "partial"


# Edge case: `chain A` with no `->` -> unchanged neighbourhood answer, hops labelled or
# `[unstated]`.
def test_chain_without_an_arrow_keeps_the_neighbourhood_answer_with_unstated_hops(
        tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, strategy=None, trace_strategy=False)
    env = repo.ask("chain", "entry")
    text = str(env.get("result") or "")
    assert "## Call chain for entry" in text, text
    hops = [ln for ln in text.splitlines() if ln.startswith("- ") and "[hop" in ln]
    assert hops, text
    assert all("[unstated]" in ln for ln in hops), text


# Edge case: B ambiguous in `chain A->B` -> the backend's candidates are listed, no path claimed.
def test_chain_to_an_ambiguous_destination_lists_the_candidates_and_claims_no_path(
        tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    env = repo.ask("chain", "entry -> save")
    text = f"{env.get('result') or ''} {env.get('hint') or ''}"
    assert "Store.save" in text and "Cache.save" in text, text
    assert "## Path" not in text, text


# Edge case: Backend 0.10.x hops that carry `strategy` -> labelled as today, no
# `hop-resolution-unstated` gap.
def test_chain_hops_that_carry_a_strategy_are_labelled_without_an_unstated_gap(
        tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, strategy="lsp_direct", trace_strategy=True)
    env = repo.ask("chain", "entry -> Cache.save")
    text = str(env.get("result") or "")
    assert "## Path entry → Cache.save" in text, text
    hops = [ln for ln in text.splitlines() if "pkg.b.load" in ln or "pkg.b.Cache.save" in ln]
    assert hops and all("[lsp]" in ln for ln in hops), text
    assert "[unstated]" not in text
    assert "hop-resolution-unstated" not in gap_kinds(env)
