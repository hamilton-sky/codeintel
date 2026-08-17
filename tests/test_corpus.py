"""Corpus harness — run the engines against REAL repositories and assert invariants.

Every other fixture in this suite is a synthetic two-to-five-file micro-repo, including the release
canary's. That is precisely the world in which none of this project's real bugs are visible: they
were all properties of scale and mess — a vendored tree, a minified bundle, a repo nested inside
another, a language server that fails to start — and a hand-built fixture cannot produce those by
construction. Every one of them was found by a person deciding to point the tool at an unfamiliar
codebase, and the recurring lesson of this project is that the technique works and nobody had
automated it.

So this file automates it, with three deliberate design choices:

* **Real repositories, pinned by commit SHA.** Not vendored (too large), not floating on a branch
  (an upstream commit must never turn CI red for reasons unrelated to this project).
* **Invariants, not golden answers.** Asserting exact output over a real repo means the assertions
  churn on every backend update and get rubber-stamped. Each check below states something that must
  hold on ANY repository, so it keeps its meaning when the corpus grows.
* **Planted adversarial artifacts.** Real repos supply structure and scale; a controlled canary
  supplies a known right answer. Both are needed — "no result escaped the root" is only meaningful
  when something worth finding sits outside it.

Opt-in, because it clones over the network and takes minutes:

    CODEINTEL_CORPUS=1 pytest tests/test_corpus.py -v

Individual checks skip when the engine they need is unavailable, so this is useful with any subset
of the backends installed. A skip is reported, never silently passed.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEINTEL_CORPUS", "").strip() not in ("1", "true", "on", "yes"),
    reason="corpus harness is opt-in: set CODEINTEL_CORPUS=1 (clones real repositories)",
)

# Pinned by SHA. Chosen for being small enough to clone quickly while still being real code with
# real structure — packages, tests, docs, examples — which is the property the synthetic fixtures
# lack. Add to this list rather than replacing: breadth is the whole point.
CORPUS = [
    {
        "name": "click",
        "url": "https://github.com/pallets/click.git",
        "sha": "cbd7a4109da16ce58f54c2a618b4c986e3041fcf",
    },
]

_CACHE = os.environ.get("CODEINTEL_CORPUS_CACHE") or "/tmp/codeintel-corpus"

# Content planted OUTSIDE every indexed root. If this string ever appears in a result, something
# read past the boundary — the concrete form of the containment bug that shipped.
CANARY = "sk-live-CORPUS-CANARY-MUST-NEVER-APPEAR"


def _clone(spec: dict) -> str:
    """Clone (or reuse) a pinned corpus repo. Skips the test when the network is unavailable —
    an unreachable GitHub is not a defect in this project."""
    dest = os.path.join(_CACHE, spec["name"])
    if os.path.isdir(os.path.join(dest, ".git")):
        head = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if head == spec["sha"]:
            return dest
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(_CACHE, exist_ok=True)
    try:
        subprocess.run(["git", "clone", "--quiet", spec["url"], dest],
                       check=True, capture_output=True, timeout=600)
        subprocess.run(["git", "-C", dest, "checkout", "--quiet", spec["sha"]],
                       check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"could not fetch corpus repo {spec['name']}: {exc}")
    return dest


@pytest.fixture(scope="session", params=CORPUS, ids=lambda s: s["name"])
def corpus_repo(request):
    """A pinned real repository, with adversarial artifacts planted around and inside it."""
    repo = _clone(request.param)

    # A secret OUTSIDE the repo, and a symlink inside pointing at it. Containment is enforced at
    # index time and at read time; this is the shape that defeated the index-time-only version.
    outside = os.path.join(_CACHE, "_outside")
    os.makedirs(outside, exist_ok=True)
    secret = os.path.join(outside, "secret.py")
    with open(secret, "w", encoding="utf-8") as fh:
        fh.write(f'SECRET_TOKEN = "{CANARY}"\n')
    link = os.path.join(repo, "_planted_link.py")
    if not os.path.islink(link):
        try:
            os.symlink(secret, link)
        except OSError:
            pass

    # A minified bundle in a directory on no skip list anywhere — the case a name-based rule
    # structurally cannot catch, and the one that put a webpack chunk in a repo's top hotspots.
    assets = os.path.join(repo, "assets")
    os.makedirs(assets, exist_ok=True)
    with open(os.path.join(assets, "chunk.js"), "w", encoding="utf-8") as fh:
        fh.write("!function(e,t){" + ("a=1;" * 4000) + "}();")

    return repo


# --------------------------------------------------------------------------- helpers

def _graph():
    from codeintel.providers.graph import GraphProvider

    p = GraphProvider()
    if not p.available:
        pytest.skip("codebase-memory-mcp not installed")
    return p


def _indexed_graph(repo: str):
    """A graph provider with *repo* indexed, or a skip explaining why not."""
    p = _graph()
    p._run("index_repository", {"repo_path": repo}, 300_000)
    lookup = p._lookup_project(repo)
    if lookup.reason == "backend-unreachable":
        pytest.skip("graph backend did not respond")
    if lookup.resolution is None:
        pytest.skip("graph backend did not register the corpus repo")
    return p


def _all_text(result: dict) -> str:
    """Everything in an envelope a caller can read."""
    return " ".join(str(result.get(k) or "") for k in ("result", "reason", "hint"))


def _paths_in(text: str) -> list[str]:
    """Every path-looking token in a rendered result."""
    import re

    return re.findall(r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|c|h|cpp|md)", text or "")


GRAPH_OPS = ["hotspots", "deadcode", "overview", "changed"]
SYMBOL_OPS = ["callers", "callees", "impact", "chain"]


# --------------------------------------------------------------------------- invariants

def test_no_op_ever_raises_on_a_real_repository(corpus_repo):
    """The never-raise contract, against real input rather than a three-file fixture."""
    p = _indexed_graph(corpus_repo)
    for op in GRAPH_OPS + SYMBOL_OPS + ["pattern"]:
        r = p.build_result(op, "Command", [], 30000, corpus_repo)
        assert r["ok"] is True, f"{op} broke the never-raise contract"
        assert set(r).issuperset({"ok", "op", "target", "result", "engine", "cached"})


def test_no_result_ever_carries_content_from_outside_the_root(corpus_repo):
    """The containment invariant, with something real to find: a secret outside the repo, reachable
    through a symlink planted inside it."""
    p = _indexed_graph(corpus_repo)
    for op in GRAPH_OPS + SYMBOL_OPS:
        r = p.build_result(op, "Command", [], 30000, corpus_repo)
        assert CANARY not in _all_text(r), f"{op} returned content from outside the indexed root"


def test_no_result_cites_a_generated_or_ignored_file(corpus_repo):
    """Generated content must not appear in a ranking. Checked against two oracles the project did
    not previously consult — its own shape heuristic, and `git check-ignore`, which is the
    repository's own statement about what is not source."""
    from codeintel.source_kind import looks_generated_path

    p = _indexed_graph(corpus_repo)
    offenders: list[str] = []
    for op in GRAPH_OPS:
        r = p.build_result(op, "", [], 30000, corpus_repo)
        for path in _paths_in(str(r.get("result") or "")):
            if looks_generated_path(path):
                offenders.append(f"{op}: {path} (generated by shape)")
                continue
            full = os.path.join(corpus_repo, path)
            if os.path.exists(full):
                ignored = subprocess.run(
                    ["git", "-C", corpus_repo, "check-ignore", "-q", path],
                    capture_output=True,
                )
                if ignored.returncode == 0:
                    offenders.append(f"{op}: {path} (git-ignored)")
    assert not offenders, "generated/ignored files surfaced as results:\n  " + "\n  ".join(offenders)


def test_the_graph_actually_answered(corpus_repo):
    """Non-vacuity guard, and it must come first.

    Every invariant below is of the form "X never appears in a result". All of them pass trivially
    against an engine that returns nothing — which is exactly the state this project shipped in for
    a whole release. A corpus harness that goes green on a dead backend would be worse than no
    harness, because it would certify the outage. So: assert the engine produced substantial output
    before believing anything it did not produce.
    """
    p = _indexed_graph(corpus_repo)
    hotspots = p.build_result("hotspots", "", [], 60000, corpus_repo)
    overview = p.build_result("overview", "", [], 60000, corpus_repo)
    assert hotspots.get("result"), "hotspots returned nothing — the invariants below prove nothing"
    assert overview.get("result"), "overview returned nothing"
    assert len(str(hotspots["result"]).splitlines()) > 10, "implausibly few hotspots for a real repo"
    assert "nodes" in str(overview["result"])


def test_the_planted_bundle_never_ranks_as_a_hotspot(corpus_repo):
    """A minified bundle is by far the most "complex" function in any tree containing one, and
    `assets/` is on no skip list in this codebase.

    Note this is a REGRESSION guard rather than a live check today: the graph backend does its own
    file selection and never offers the `.js` to codeintel at all, so the assertion is currently
    satisfied upstream. It earns its place by failing if that ever changes — but the corresponding
    live check for codeintel's OWN filtering is `test_generated_content_stays_out_of_the_corpus`
    below, which exercises the indexer directly.
    """
    p = _indexed_graph(corpus_repo)
    r = p.build_result("hotspots", "", [], 60000, corpus_repo)
    assert "chunk.js" not in str(r.get("result") or "")


def test_generated_content_stays_out_of_the_corpus(corpus_repo):
    """codeintel's own file selection, over a real repository.

    This is the check the graph one cannot be: it drives `Indexer._walk_files`, which is where this
    project decides what is hand-written source, and needs neither backend nor embedding model. The
    planted bundle sits in `assets/` — a name on no skip list — so only the content-shape heuristic
    can exclude it, and the symlink resolves outside the root so only containment can.
    """
    from codeintel.indexer import Indexer
    from codeintel.semantic_db import SemanticDb

    db = SemanticDb(os.path.join(_CACHE, "walk.sqlite"))
    db.init()
    try:
        walked = {os.path.relpath(str(f), corpus_repo)
                  for f in Indexer(db)._walk_files(pathlib.Path(corpus_repo))}
    finally:
        db.close()

    # Non-vacuity first, again: an empty walk would satisfy every exclusion below.
    assert len(walked) > 40, f"implausibly few files walked for a real repo: {len(walked)}"
    assert any(p.endswith(".py") and p.startswith("src/") for p in walked), "no real source walked"

    assert "assets/chunk.js" not in walked, "a minified bundle entered the corpus"
    assert "_planted_link.py" not in walked, "a symlink out of the root entered the corpus"

    # And nothing the repository itself declares as non-source.
    ignored = [p for p in walked
               if subprocess.run(["git", "-C", corpus_repo, "check-ignore", "-q", p],
                                 capture_output=True).returncode == 0]
    assert not ignored, f"git-ignored files entered the corpus: {ignored[:10]}"


def test_deadcode_hits_have_no_textual_reference_in_the_tree(corpus_repo):
    """`deadcode` names symbols an agent may delete, so its output gets checked against the source
    rather than trusted. A name that appears anywhere beyond its own definition is not dead."""
    import re

    p = _indexed_graph(corpus_repo)
    r = p.build_result("deadcode", "", [], 60000, corpus_repo)
    text = str(r.get("result") or "")
    if not text or "(0)" in text:
        pytest.skip("no deadcode candidates reported for this repo")

    names = re.findall(r"^- ([\w.]+)", text, re.MULTILINE)[:10]
    live: list[str] = []
    for qualified in names:
        name = qualified.rsplit(".", 1)[-1]
        if len(name) < 4:            # too short to grep meaningfully
            continue
        hits = subprocess.run(
            ["git", "-C", corpus_repo, "grep", "-c", "-w", name],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        total = sum(int(line.rsplit(":", 1)[-1]) for line in hits if ":" in line)
        if total > 1:                # more than its own definition
            live.append(f"{qualified}: {total} textual references")
    assert not live, "deadcode named symbols that are referenced in the tree:\n  " + "\n  ".join(live)


def test_a_nonsense_target_is_reported_as_absent_not_as_a_failure(corpus_repo):
    """Reason fidelity. With the backend present and working, a target that genuinely does not
    exist must land in the asked-and-found-nothing family — never in the could-not-ask family,
    which would tell an agent the engine is broken when the answer is simply "no"."""
    p = _indexed_graph(corpus_repo)
    r = p.build_result("callers", "zzz_no_such_symbol_anywhere_zzz", [], 30000, corpus_repo)
    assert r["result"] is None
    assert r.get("reason") in ("not-in-graph", "no-result"), r.get("reason")
    assert r.get("reason") not in (
        "engine-unavailable", "backend-unreachable", "backend-incompatible",
        "project-not-indexed", "project-not-indexed-standalone", "error",
    )


def test_no_result_leaks_an_absolute_host_path(corpus_repo):
    """The home-path disclosure class. Renderers were swept for it once and a third and fourth site
    turned up later; this asserts the property over real output instead of enumerating sites."""
    p = _indexed_graph(corpus_repo)
    home = os.path.expanduser("~")
    for op in GRAPH_OPS + SYMBOL_OPS:
        r = p.build_result(op, "Command", [], 30000, corpus_repo)
        text = _all_text(r)
        assert home not in text, f"{op} leaked the host home directory"
        # The backend's path-slug project id is the flattened absolute path; it must not appear.
        assert corpus_repo.strip("/").replace("/", "-") not in text, f"{op} leaked the project id"


def test_the_same_query_gives_the_same_answer_in_a_fresh_process(corpus_repo):
    """Determinism. A ranking that reorders between runs cannot be reviewed, and a cached answer
    that differs from an uncached one is a silent staleness bug."""
    _indexed_graph(corpus_repo)          # ensure indexed before the subprocesses run
    prog = (
        "import json,sys;"
        "from codeintel.providers.graph import GraphProvider;"
        "p=GraphProvider();"
        f"r=p.build_result('hotspots','',[],60000,{corpus_repo!r});"
        "print(json.dumps(r.get('result')))"
    )
    runs = []
    for _ in range(2):
        out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True,
                             timeout=300)
        if out.returncode != 0:
            pytest.skip(f"subprocess run failed: {out.stderr[-300:]}")
        runs.append(json.loads(out.stdout or "null"))
    assert runs[0] == runs[1], "the same query returned different answers in two fresh processes"
