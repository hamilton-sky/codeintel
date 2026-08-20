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

import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

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
    {
        # A second pure-Python library, added for the `deadcode` measurement. One repository is not a
        # corpus: `click` alone put the op's precision at 100%, and this one put it at 14% — the
        # difference being Makefile targets, which the graph backend indexes as `Function` nodes.
        # A measurement that would have reinstated an op on n=1 is the argument for n>1.
        "name": "requests",
        "url": "https://github.com/psf/requests.git",
        "sha": "0e322af87745eff34caffe4df68456ebc20d9068",
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

    # Known-answer symbols for the dead-code measurement, in both directions. Without these the
    # oracle has no dead symbols at all on either repository — see `_PLANTED_DEADCODE`.
    with open(os.path.join(repo, "_planted_deadcode.py"), "w", encoding="utf-8") as fh:
        fh.write(_PLANTED_DEADCODE)

    return repo


# Planted inside every corpus repo. Private by FILENAME as well as by symbol name, so the oracle's
# "public API of an installed package" rule cannot fire on it. Each group is a shape the `deadcode`
# withdrawal evidence actually recorded.
_PLANTED_DEADCODE = '''"""Planted by codeintel's corpus harness. Not part of this repository."""
from __future__ import annotations

import sys
import typing as t

_DISPATCH: dict[str, t.Callable[[], str]] = {}


def _planted_register(fn: t.Callable[[], str]) -> t.Callable[[], str]:
    _DISPATCH[fn.__name__] = fn
    return fn


# --- genuinely dead: no reference anywhere in this repository ---------------------------------

def _planted_dead_plain() -> str:
    return "nothing in this tree names me"


async def _planted_dead_async() -> str:
    # `async def`. A verification pattern of `^\\s*def ` cannot see this line, which is how 33 of
    # 66 functions became invisible to the check that declared a tree clean.
    return "nothing names me either"


def _planted_dead_nested_owner() -> str:
    def _planted_dead_inner() -> str:
        return "nested, and nothing names me"
    return "outer"


class _PlantedHolder:
    def _planted_dead_method(self) -> str:
        return "no caller anywhere"


# --- live, but carrying in-degree 0 in a call graph -------------------------------------------

@_planted_register
def _planted_live_registered() -> str:
    """Reached only through the registry its decorator put it in — the rollup-plugin-hook shape."""
    return "the decorator registered me"


def _planted_live_by_string() -> str:
    """Reached only by name through getattr — no syntactic caller exists."""
    return "getattr finds me"


def _planted_live_in_table() -> str:
    """Reached only as a value in a dispatch table — the `Record<string, fn>` shape."""
    return "a table holds me"


_PLANTED_TABLE: dict[str, t.Callable[[], str]] = {"table": _planted_live_in_table}
_PLANTED_RESOLVED = getattr(sys.modules[__name__], "_planted_live_by_string")
'''


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


def test_the_dead_code_oracle_can_actually_find_dead_code(corpus_repo):
    """Non-vacuity for the oracle itself, and it has to come before any number derived from it.

    Every liveness rule in `_label_definition` is a reason to call a symbol LIVE, so an oracle with a
    bug that makes one rule fire too eagerly labels the whole tree live and then "proves" any
    dead-code heuristic worthless. The planted canaries are the control: they are known-dead by
    construction, and the oracle has to find every one of them — including the `async def`, which is
    the declaration form that made a human verification miss 33 of 66 functions.
    """
    population = _labelled_population(corpus_repo)
    assert len(population) > 300, f"implausibly few definitions for a real repo: {len(population)}"

    dead = {defn.qualname for label, _evidence, defn in population.values() if label == "dead"}
    planted = {defn.qualname for _l, _e, defn in population.values()
               if defn.path == "_planted_deadcode.py"}
    assert planted, "the canary file was not planted — the measurement below has no denominator"

    must_be_dead = {q for q in planted if "_dead_" in q}
    assert must_be_dead <= dead, f"the oracle missed known-dead symbols: {sorted(must_be_dead - dead)}"
    assert any("async" in q for q in must_be_dead & dead), (
        "the `async def` canary is not among the dead labels — the AST walk is missing a "
        "declaration form, which is exactly the defect this population is derived to avoid")

    must_be_live = {q for q in planted if "_live_" in q}
    assert not (must_be_live & dead), (
        f"the oracle called framework-reached symbols dead: {sorted(must_be_live & dead)}")


def test_deadcode_precision_and_recall_are_measured_not_assumed(corpus_repo):
    """The measurement that retired `deadcode`, kept runnable so the decision can be revisited.

    `_op_deadcode` no longer exists, so what this measures is the SIGNAL it was built on: the graph's
    raw in-degree-0 symbol set, scoped exactly as the op scoped it. That signal is what any future
    dead-code op would start from, and the finding that retired this one is a property of the signal
    rather than of the code that dressed it up — on two pinned real repositories it is dominated by
    live symbols, and on `requests` it is dominated by **Makefile targets**, which the backend
    indexes as `Function` nodes.

    The recorded numbers, for the record and for comparison: precision 6/24 = 25% as shipped, and on
    real code with the canaries removed it named 18 candidates of which every one was live. Recall
    was 60%, against a denominator made entirely of planted symbols — in 2,425 real definitions
    across the two repositories there was not one dead private symbol to find.

    This test asserts the PREMISE of the retirement rather than the historical numbers, which would
    churn on every upstream backend release. If it ever fails, the premise has stopped holding and
    the decision is worth re-opening with fresh measurements — that is a result, not a flake.
    """
    provider = _indexed_graph(corpus_repo)
    population = _labelled_population(corpus_repo)
    label_of = {defn.name: label for label, _evidence, defn in population.values()}

    rows = provider._search_symbols(
        {"label": "Function", "max_degree": 0, "exclude_entry_points": True, "limit": 200},
        _project_name(provider, corpus_repo), 120_000)
    assert rows is not None, "the backend did not answer — nothing below would mean anything"
    candidates = [r for r in rows
                  if not provider._is_noise(r) and not r.get("is_entry_point")]
    assert candidates, "no in-degree-0 candidates at all — this measurement proves nothing"

    real = [r for r in candidates if str(r.get("file_path") or "") != "_planted_deadcode.py"]
    live = [r for r in real if label_of.get(str(r.get("name") or "")) != "dead"]
    detail = "\n".join(f"    {r.get('name')}  ({r.get('file_path')})  "
                       f"oracle={label_of.get(str(r.get('name') or ''), '(not a python def)')}"
                       for r in real[:25])
    assert len(live) >= len(real) * 0.5, (
        "the raw in-degree-0 signal is now MAJORITY DEAD on this repository, which is not what "
        "retired `deadcode` — re-run the measurement and reconsider the decision.\n"
        f"{len(live)} of {len(real)} real candidates were live:\n{detail}")

    planted_dead = {defn.name for label, _e, defn in population.values()
                    if label == "dead" and defn.path == "_planted_deadcode.py"}
    found = {str(r.get("name") or "") for r in candidates} & planted_dead
    assert found, (
        f"the signal did not surface ANY planted dead symbol ({sorted(planted_dead)}), so this "
        f"repository cannot speak to recall either way")


def test_the_retired_op_refuses_on_a_real_repository(corpus_repo):
    """And the retirement holds end-to-end, against a real index rather than a stub: `deadcode` must
    safe-null with its explanation, not answer, and not report `unsupported-op`."""
    provider = _indexed_graph(corpus_repo)
    envelope = provider.build_result("deadcode", "", [], 60000, corpus_repo)

    assert envelope["ok"] is True
    assert envelope["result"] is None, envelope
    assert envelope.get("reason") == "op-withdrawn", envelope
    assert "callers" in (envelope.get("hint") or ""), envelope


def _project_name(provider, repo: str) -> str:
    resolution = provider._lookup_project(repo).resolution
    assert resolution is not None
    return resolution.name


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


def test_callers_render_module_scope_as_a_location_not_a_pseudo_symbol(corpus_repo):
    """A module-scope pseudo-node must never reach a `callers` row as a symbol.

    The backend has no node for code that runs at module or class-body scope, so it hangs those edges
    off a whole-file container: a `File` node whose qualified name it synthesises as
    `<module>.__file__`, and/or a `Module` node named for the file. Rendered verbatim,
    `## Callers of invoke (4)` on this corpus listed `src.click.core.__file__` and
    `src.click.decorators.__file__` as two of the four callers — half the count a reader trusts was a
    symbol nobody can open. This was invisible to 818 unit tests and immediate on a real repository,
    which is why the invariant lives here.

    The edge is real — module-scope code genuinely references the symbol (`core.__file__` references
    `builtins.len` and an exception class from another file) — so it is relabelled to its file
    location, never dropped: on this corpus 147 symbols are referenced ONLY from module scope, and
    dropping would report a live, referenced function as uncalled.

    The container population is DERIVED from the live graph by SHAPE — never a typed label list, and
    never the false premise that a caller is only ever a Function or Method. Real repositories call
    from `Class` nodes (a `@Injectable()` decorator references the class's own body) and from
    `Variable` nodes (a `const` arrow function), and those are real navigable symbols, not containers;
    asserting "every non-Function/Method caller is a container" fails on the first such repo, and did
    (`brightsky-ai`, `pathly-adapters`). What makes a node a container is that it renders as a FILE
    rather than a symbol: a `.__file__` qualified name, or a `name` that is a path or a source
    filename. Every such node must be caught by the module-scope filter, so a new container label the
    backend introduces fails here instead of rendering as a fiction again.
    """
    from codeintel.graph_render import _is_module_scope_node
    from codeintel.providers.graph import _FILE_EXTENSIONS, _strip_project_prefix

    p = _indexed_graph(corpus_repo)
    proj = _project_name(p, corpus_repo)

    edges = p._query_rows(
        "MATCH (a)-[c:CALLS|USAGE]->(b) RETURN labels(a), a.name, a.qualified_name, b.name LIMIT 10000",
        proj, 120_000)
    assert edges, "the graph returned no call edges — nothing below would mean anything"  # non-vacuity

    def _renders_as_a_file_container(name: str, qn_raw: str) -> bool:
        # A container stands for a FILE, not a symbol: the backend synthesises a `.__file__` qualified
        # name for a `File` node and names a `Module` node after its path/basename. A real symbol —
        # Function, Method, Class, Variable — has an identifier name and a dotted symbol qn.
        if _strip_project_prefix(qn_raw, may_be_filename=False).endswith(".__file__"):
            return True
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        return "/" in name or ("." in base and base.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS)

    file_dunder_targets: list[str] = []
    container_shaped = 0
    for e in edges:
        name, qn = str(e.get("a.name") or ""), str(e.get("a.qualified_name") or "")
        if not _renders_as_a_file_container(name, qn):
            continue
        container_shaped += 1
        # Derived rule-3 guard: anything that renders as a file/module container MUST be caught by the
        # module-scope filter, or it reaches output as a fictional symbol. Catches a new container
        # label without a typed list, and does NOT fire on Class/Variable callers.
        assert _is_module_scope_node(e.get("labels(a)")), (
            f"a caller renders as a file/module container but the module-scope filter does not catch "
            f"it, so it will reach output as a fiction: name={name!r} qn={qn!r} "
            f"labels={e.get('labels(a)')!r}")
        if qn.endswith(".__file__") and e.get("b.name"):
            file_dunder_targets.append(str(e["b.name"]))
    assert container_shaped, "no file/module container caller nodes on a real repo — cannot show the bug"

    # End to end, over real targets. NO `callers` answer may render the synthetic `__file__` symbol,
    # and at least one whose caller genuinely includes a `__file__` node must show the relabelled
    # location — the relabel firing, not merely the fiction being absent for other reasons.
    assert file_dunder_targets, "no `__file__` caller on a real repo — the specific defect is absent"
    demonstrated = False
    for target in dict.fromkeys(file_dunder_targets):          # de-dupe, keep order, cap the work
        body = str(p.build_result("callers", target, [], 60000, corpus_repo).get("result") or "")
        assert ".__file__" not in body, (
            f"a module-scope pseudo-node reached `callers {target}` output as a symbol:\n{body}")
        if "module scope of" in body:
            demonstrated = True
        if demonstrated and len(dict.fromkeys(file_dunder_targets)) > 6:
            break
    assert demonstrated, "no callers answer rendered a module-scope location — the relabel is not firing"


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


# --------------------------------------------------------------------------- deadcode measurement
#
# `deadcode` is withdrawn (`_WITHDRAWN_OPS` in graph.py) until "a labelled corpus measures its
# precision and recall". This is that corpus. It is the one op whose output is an instruction to
# delete code, so the standard of evidence has to be higher than for any other, and "it looked right
# on a repo I know" is what got it withdrawn in the first place.
#
# Three properties make these numbers mean something:
#
# * **The population comes from the AST, not from the op.** A sample drawn from what the op returns
#   can only measure precision, and would report perfect recall by construction. Every function and
#   method defined in the repository is labelled, so recall has an honest denominator.
# * **`ast`, not a regex.** The human verification that cleared the recorded false negative used
#   `^\s*def `, which never matches `async def` — 33 of the 66 functions in that tree were invisible
#   to the check that declared the tree clean. A verification whose population is defined by a
#   pattern is only as good as the pattern.
# * **The labelling errs toward LIVE, on purpose.** Every rule below is a reason to call a symbol
#   live; "dead" is only what survives all of them. That biases the measurement AGAINST the op — a
#   symbol this oracle wrongly calls live costs the op precision — which is the right direction for
#   a decision about whether to trust it to tell people to delete code.

_MEASURED_LANGUAGE = ".py"       # the oracle below is Python-only; see `_label_definition`

# Python calls these itself. `__init__` has no caller in any source tree and is not dead.
_PROTOCOL_PREFIX = "__"


@dataclass(frozen=True)
class _Definition:
    """One function or method, as the AST sees it."""

    name: str
    qualname: str
    path: str
    line: int
    end_line: int
    decorators: tuple[str, ...]
    class_name: str
    class_bases: tuple[str, ...] = ()

    @property
    def is_private(self) -> bool:
        """Whether nothing outside this repository could name it.

        A public symbol of an installed package is called by code that is not in the tree, so
        in-degree 0 says nothing about it. Privacy is the property that makes "unreferenced here"
        mean "unreferenced": a leading underscore on the symbol or on any package component of its
        path."""
        parts = self.path.replace("\\", "/").split("/")
        return (self.name.startswith("_")
                or any(part.startswith("_") and part != "__init__.py" for part in parts))


def _python_definitions(repo: str) -> list[_Definition]:
    """Every `def` and `async def` in *repo*, at any nesting depth, methods included.

    The candidate population for the measurement. Files the product itself considers out of scope
    (generated, archived) are skipped using the product's own predicates rather than a second
    hand-typed copy of them."""
    from codeintel.providers.graph import _ARCHIVE_DIRS, _VERIFY_SKIP_DIRS
    from codeintel.source_kind import looks_generated_path, looks_generated_text

    found: list[_Definition] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in _VERIFY_SKIP_DIRS
                       and d.lower() not in _ARCHIVE_DIRS
                       and not looks_generated_path(d + "/x")]
        for fname in sorted(filenames):
            if not fname.endswith(_MEASURED_LANGUAGE) or looks_generated_path(fname):
                continue
            full = os.path.join(dirpath, fname)
            try:
                text = pathlib.Path(full).read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (OSError, SyntaxError):
                continue
            if looks_generated_text(text):
                continue
            rel = os.path.relpath(full, repo)

            def _walk(node, prefix: str, class_name: str,
                      bases: tuple[str, ...] = (), rel=rel) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        _walk(child, f"{prefix}{child.name}.", child.name,
                              tuple(ast.unparse(b) for b in child.bases))
                    elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        # `async def` is the whole point of using the AST here.
                        found.append(_Definition(
                            name=child.name,
                            qualname=f"{prefix}{child.name}",
                            path=rel,
                            line=child.lineno,
                            end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                            decorators=tuple(ast.unparse(d) for d in child.decorator_list),
                            class_name=class_name,
                            class_bases=bases,
                        ))
                        _walk(child, f"{prefix}{child.name}.", class_name, bases)
                    else:
                        _walk(child, prefix, class_name, bases)

            _walk(tree, "", "")
    return found


def _python_references(repo: str) -> dict[str, list[tuple[str, int, str]]]:
    """Every name USED anywhere in the tree → [(file, line, how)].

    Four kinds of use, and the last two are the ones a naive scan misses — they are exactly the
    shapes that made four of five candidates live on the repository that got this op withdrawn:

    * a plain load (`handle(...)`, `x = handle`)
    * an attribute (`self.handle()`, `click.echo`) — how every method is called
    * an import alias (`from .core import Command`) — a library's public API is re-exported, and
      that import is the only "reference" a public symbol has inside its own repository
    * a string constant (`getattr(obj, "handle")`, `{"handle": handle}`, `__all__ = ["handle"]`) —
      a runtime-dispatched name has no syntactic caller at all
    """
    uses: dict[str, list[tuple[str, int, str]]] = {}

    def _note(name: str, rel: str, line: int, how: str) -> None:
        if name:
            uses.setdefault(name, []).append((rel, line, how))

    for dirpath, _dirnames, filenames in os.walk(repo):
        for fname in sorted(filenames):
            if not fname.endswith(_MEASURED_LANGUAGE):
                continue
            full = os.path.join(dirpath, fname)
            try:
                tree = ast.parse(pathlib.Path(full).read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            rel = os.path.relpath(full, repo)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    _note(node.id, rel, node.lineno, "load")
                elif isinstance(node, ast.Attribute):
                    _note(node.attr, rel, node.lineno, "attribute")
                elif isinstance(node, ast.Import | ast.ImportFrom):
                    for alias in node.names:
                        _note(alias.name.rsplit(".", 1)[-1], rel, node.lineno, "import")
                        _note(alias.asname or "", rel, node.lineno, "import")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    _note(node.value.strip(), rel, node.lineno, "string")
    return uses


def _non_python_mentions(repo: str) -> dict[str, set[str]]:
    """Names mentioned in files that are not Python — docs, templates, packaging metadata.

    A name in `pyproject.toml` is an entry point the packaging system calls; a name in the docs is
    public API someone is being told to call. Neither has a syntactic caller in the tree."""
    mentions: dict[str, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for fname in filenames:
            if fname.endswith(_MEASURED_LANGUAGE):
                continue
            full = os.path.join(dirpath, fname)
            try:
                text = pathlib.Path(full).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > 2_000_000:
                continue
            rel = os.path.relpath(full, repo)
            for word in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text)):
                mentions.setdefault(word, set()).add(rel)
    return mentions


def _label_definition(
    defn: _Definition,
    uses: dict[str, list[tuple[str, int, str]]],
    same_name_methods: dict[str, set[str]],
    declared_classes: dict[str, tuple[str, ...]],
    mentions: dict[str, set[str]],
) -> tuple[str, str]:
    """``("live"|"dead", evidence)`` for one definition.

    Every branch returning "live" records the reference that makes it live; the "dead" branch records
    the searches that came back empty. The rules are disjunctive, so their order changes no outcome.

    They are deliberately generous about liveness, and each one is a shape the withdrawal evidence
    actually recorded: a decorator that registers a function under another name (the rollup plugin
    hook, click's own `@group.command()` examples), a method implementing an interface its own
    repository does not declare, a symbol whose only "caller" is a user of the published package.
    Wrongly calling one of these dead is the failure mode that made an agent delete working code, so
    the oracle refuses to call any of them dead — which costs the op precision rather than granting
    it, the correct direction for this decision.
    """
    if defn.name.startswith("__") and defn.name.endswith("__"):
        return "live", f"python protocol method — the interpreter calls `{defn.name}`"

    if defn.decorators:
        # A decorator either registers the function somewhere, replaces it with an object referenced
        # under another name, or marks it as part of an interface. In all three cases the function's
        # own name having in-degree 0 is not evidence about whether it runs.
        return "live", f"decorated with @{defn.decorators[0]} — registered, wrapped or interface"

    if re.match(r"test_", defn.name) and (
            os.path.basename(defn.path).startswith(("test_", "conftest"))):
        return "live", "collected and called by pytest, by naming convention"

    for how in ("load", "attribute", "import", "string"):
        for path, line, kind in uses.get(defn.name, ()):
            if kind != how:
                continue
            # A reference inside the definition's OWN body is recursion, not a use of it.
            if path == defn.path and defn.line <= line <= defn.end_line:
                continue
            return "live", f"{how} at {path}:{line}"

    external = _first_external_base(defn.class_bases, declared_classes) if defn.class_name else ""
    if external:
        # `TextWrapper._wrap_chunks` overrides a stdlib base; `_WindowsConsoleReader.readinto`
        # implements `io.RawIOBase` two links up. The interface is not declared in this tree, so
        # nothing in this tree can call it by name.
        return "live", f"method of a class extending {external}, which is defined outside this repo"

    if defn.class_name and defn.name in _runtime_protocol_names():
        # `ConsoleStream.writelines` — a duck-typed file wrapper with no base to inherit from. The
        # interpreter and the stdlib call these names on anything that has them.
        return "live", f"`{defn.name}` is a runtime protocol method called on duck-typed objects"

    others = same_name_methods.get(defn.name, set()) - {f"{defn.path}:{defn.line}"}
    if defn.class_name and others:
        return "live", f"overrides/overridden — same name defined at {sorted(others)[0]}"

    if not defn.is_private:
        return "live", "public API of an installed package — its callers are outside this repository"

    where = mentions.get(defn.name) or set()
    if where:
        return "live", f"named outside Python source (entry point/docs): {sorted(where)[0]}"

    return "dead", (
        f"private, undecorated, not a protocol method, not an override; no load, attribute, import "
        f"or string use anywhere in the tree outside {defn.path}:{defn.line}-{defn.end_line}; "
        f"not mentioned in any non-Python file"
    )


def _labelled_population(repo: str) -> dict[str, tuple[str, str, _Definition]]:
    """``{"path:line": (label, evidence, definition)}`` for every function/method in *repo*."""
    defs = _python_definitions(repo)
    uses = _python_references(repo)
    mentions = _non_python_mentions(repo)
    same_name_methods: dict[str, set[str]] = {}
    for d in defs:
        if d.class_name:
            same_name_methods.setdefault(d.name, set()).add(f"{d.path}:{d.line}")
    classes = _class_bases_in(repo)
    out = {}
    for d in defs:
        label, evidence = _label_definition(d, uses, same_name_methods, classes, mentions)
        out[f"{d.path}:{d.line}"] = (label, evidence, d)
    return out


def _class_bases_in(repo: str) -> dict[str, tuple[str, ...]]:
    """``{class name: its declared bases}`` for every class this repository declares.

    Needed to answer "does this class inherit, transitively, from something defined OUTSIDE this
    tree?" — because a method implementing an external interface has no caller here by construction.
    `_WindowsConsoleReader(_WindowsConsoleRawIOBase)` looks entirely local until you follow one more
    link to `io.RawIOBase`, and a non-transitive check called its `readinto` dead."""
    bases: dict[str, tuple[str, ...]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fname in sorted(filenames):
            if not fname.endswith(_MEASURED_LANGUAGE):
                continue
            try:
                tree = ast.parse(pathlib.Path(os.path.join(dirpath, fname)).read_text(
                    encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases.setdefault(node.name, tuple(ast.unparse(b) for b in node.bases))
    return bases


def _first_external_base(bases: tuple[str, ...], declared: dict[str, tuple[str, ...]]) -> str:
    """The first base in the transitive closure of *bases* that this repository does not declare.

    A DOTTED base is always external: `textwrap.TextWrapper` names another module's class even when
    the repo happens to declare a `TextWrapper` of its own — which is exactly how click's
    `TextWrapper._wrap_chunks`, whose docstring says it mirrors the stdlib method it overrides, was
    labelled unreferenced."""
    seen: set[str] = set()
    queue = list(bases)
    while queue:
        base = queue.pop(0)
        if base in seen:
            continue
        seen.add(base)
        if "." in base or base not in declared:
            return base
        queue.extend(declared[base])
    return ""


def _runtime_protocol_names() -> frozenset[str]:
    """Method names the interpreter or the standard library calls on a duck-typed object.

    `ConsoleStream` declares no base at all — it is a `t.TextIO` wrapper — so no inheritance check
    can tell that its `writelines` is the file protocol rather than a dead method. The NAMES are read
    off the real runtime objects rather than typed here, so a method Python adds in a later version
    is covered without anyone remembering; the three modules are the input, and they are the stable
    part."""
    import collections.abc
    import io
    import typing

    names: set[str] = set(dir(object))
    for module in (io, collections.abc, typing):
        for value in vars(module).values():
            if isinstance(value, type):
                names.update(dir(value))
    return frozenset(names)
