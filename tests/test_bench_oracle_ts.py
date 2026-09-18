"""The TypeScript oracle, pinned against a corpus whose answer is known by construction.

This arm exists because the worst failure ever observed in this project is not in Python. Thirty-two
fabricated callers for `describe` — a framework global matched across files that never imported it —
and every number in `bench/README.md` was silent about it, because every number was Python.

It also could not have been built a commit earlier. Under positives-only truth the `describe` sites
were all unjudged, so a TypeScript arm pointed straight at them would have reported `n/a` or 100% and
measured nothing. `not-target` is what makes the arm able to say anything.

The interesting result is that TypeScript ends up MORE decidable than Python on exactly that case.
An ES module's bindings are exhaustively stated, so a bare name a module never imports provably is
not some other file's export. `test_the_case_python_must_abstain_on_is_decidable_here` asserts both
halves of that against the two oracles at once.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

pytest.importorskip("tree_sitter_language_pack")

BENCH = pathlib.Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(BENCH))

import oracle_ts
from oracle_py import CALL, IMPORT, NOT_TARGET, REFERENCE, UNDECIDABLE, Truth

CORPUS = str(BENCH / "fixtures" / "corpus_ts")
SRC = "src"


@pytest.fixture(scope="module")
def repo():
    """One parse of the tree, shared — the same reuse the scorer gets for a real repository."""
    return oracle_ts.index_repo(CORPUS)


def _truth(repo, symbol: str, def_file: str = f"{SRC}/proxy.ts") -> Truth:
    return oracle_ts.truth_for(CORPUS, oracle_ts.target_from_definition(CORPUS, def_file, symbol),
                               repo)


def _labels(t: Truth) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for kind, keys in ((CALL, t.calls), (REFERENCE, t.references), (IMPORT, t.imports),
                       (NOT_TARGET, t.negatives), (UNDECIDABLE, t.undecidable)):
        for k in keys:
            out[k] = kind
    return out


# --- the headline -----------------------------------------------------------------------------

def test_a_framework_global_is_a_proven_non_caller(repo) -> None:
    """THE case. `jestGlobals.ts` calls a bare `describe` it never imports.

    This is the shape that produced 32 fabricated callers. It is a proven negative here because the
    file is a module and imports no `describe`: an ES module reaches another file's module-scope
    symbol only through an import.
    """
    t = _truth(repo, "describe")
    assert t.negatives == {(f"{SRC}/jestGlobals.ts", "<module>")}
    assert not t.calls
    assert not t.undecidable
    assert t.coverage == 1.0


def test_the_case_python_must_abstain_on_is_decidable_here() -> None:
    """The two oracles, on the same shape, reaching different and individually correct answers.

    Python abstains because another module can install a global and the syntax cannot rule it out.
    TypeScript decides, because module reachability does rule it out. Neither is being generous;
    they are reporting what their own language actually settles.
    """
    import oracle_py

    py_corpus = str(BENCH / "fixtures" / "corpus")
    py = oracle_py.truth_for(
        py_corpus, oracle_py.target_from_definition(py_corpus, "src/corpuspkg/sse.py", "describe"))
    assert py.undecidable == {("src/corpuspkg/injected.py", "suite")}
    assert not py.negatives

    ts = oracle_ts.truth_for(
        CORPUS, oracle_ts.target_from_definition(CORPUS, f"{SRC}/proxy.ts", "describe"))
    assert ts.negatives == {(f"{SRC}/jestGlobals.ts", "<module>")}
    assert not ts.undecidable


# --- the whole labelled corpus ------------------------------------------------------------------

EXPECTED = {
    # Direct, aliased, transitively re-exported, and through a namespace import. All calls.
    (f"{SRC}/callerDirect.ts", "send"): CALL,
    (f"{SRC}/callerAliased.ts", "dispatch"): CALL,
    (f"{SRC}/callerFacade.ts", "relay"): CALL,
    (f"{SRC}/namespaceCall.ts", "viaNamespace"): CALL,
    (f"{SRC}/jestGlobals.ts", "<module>"): CALL,
    # Passed, never invoked — the `forward_released_item` shape that started all of this.
    (f"{SRC}/passesValue.ts", "install"): REFERENCE,
    # A type-space mention: not a caller, but a real dependency, so it belongs in change impact.
    (f"{SRC}/typeOnly.ts", "<module>"): REFERENCE,
    # Binding sites. An import is not a caller.
    (f"{SRC}/callerDirect.ts", "<module>"): IMPORT,
    (f"{SRC}/callerAliased.ts", "<module>"): IMPORT,
    (f"{SRC}/callerFacade.ts", "<module>"): IMPORT,
    (f"{SRC}/namespaceCall.ts", "<module>"): IMPORT,
    (f"{SRC}/passesValue.ts", "<module>"): IMPORT,
    (f"{SRC}/importsOnly.ts", "<module>"): IMPORT,
    (f"{SRC}/reexport.ts", "<module>"): IMPORT,
    (f"{SRC}/facade.ts", "<module>"): IMPORT,
    # Proven negatives: a local, a parameter, and an import of a different symbol.
    (f"{SRC}/shadowLocal.ts", "handler"): NOT_TARGET,
    (f"{SRC}/shadowParam.ts", "dispatch"): NOT_TARGET,
    (f"{SRC}/shadowImport.ts", "emit"): NOT_TARGET,
    # The three guards that keep the module argument honest.
    (f"{SRC}/memberCall.ts", "Relay.go"): UNDECIDABLE,
    (f"{SRC}/aliasImport.ts", "viaAlias"): UNDECIDABLE,
    (f"{SRC}/script.ts", "runAll"): UNDECIDABLE,
}


def test_every_corpus_site_gets_the_label_it_was_written_for(repo) -> None:
    assert _labels(_truth(repo, "forwardReleasedItem")) == EXPECTED


def test_the_defining_file_is_not_a_caller_of_itself(repo) -> None:
    """`export function forwardReleasedItem` binds a name; it does not mention one."""
    assert f"{SRC}/proxy.ts" not in {f for f, _ in _labels(_truth(repo, "forwardReleasedItem"))}


# --- resolution ---------------------------------------------------------------------------------

def test_a_transitive_re_export_is_followed(repo) -> None:
    """`proxy` -> `reexport` -> `facade`, each a stated `export { x } from`. Following a stated
    import is what a correct resolver does; matching a bare name is the opposite of it."""
    aliases = oracle_ts.alias_set(f"{CORPUS}/{SRC}/proxy.ts", "forwardReleasedItem", repo)
    assert {pathlib.Path(f).name for f, _ in aliases} == {"proxy.ts", "reexport.ts", "facade.ts"}
    assert (f"{SRC}/callerFacade.ts", "relay") in _truth(repo, "forwardReleasedItem").calls


def test_an_aliased_import_is_found_under_its_new_name(repo) -> None:
    """`import { forwardReleasedItem as fwd }` means every call site reads `fwd(...)`. Scanning
    only for the target's own spelling finds the import and none of its callers."""
    assert (f"{SRC}/callerAliased.ts", "dispatch") in _truth(repo, "forwardReleasedItem").calls


def test_a_namespace_import_is_decidable_but_a_value_is_not(repo) -> None:
    """`ns.forwardReleasedItem()` and `obj.forwardReleasedItem()` look identical until you ask what
    the receiver is. One is arithmetic on a stated import; the other is the abstention class."""
    t = _truth(repo, "forwardReleasedItem")
    assert (f"{SRC}/namespaceCall.ts", "viaNamespace") in t.calls
    assert (f"{SRC}/memberCall.ts", "Relay.go") in t.undecidable


# --- the three guards on the module-reachability argument -----------------------------------------

def test_a_script_is_not_a_module_so_its_bare_names_are_undecidable(repo) -> None:
    """`script.ts` has no import and no export. Its top-level names share the global scope, so the
    reachability argument the negative rests on simply does not apply to it."""
    assert (f"{SRC}/script.ts", "runAll") in _truth(repo, "forwardReleasedItem").undecidable


def test_a_self_installed_global_forces_abstention_on_that_name(repo) -> None:
    """`globalSetup.ts` assigns `globalThis.legacyHelper`, which manufactures exactly the escape
    hatch module reachability denies. So `legacyHelper` abstains where `describe` decides — the
    guard is per NAME, and both outcomes are visible in one corpus.
    """
    assert "legacyHelper" in repo.injected_globals
    t = _truth(repo, "legacyHelper")
    assert t.undecidable == {(f"{SRC}/usesLegacy.ts", "total")}
    assert not t.negatives
    # …and the name without the global assignment still decides.
    assert _truth(repo, "describe").negatives


def test_an_unresolvable_specifier_is_undecidable_not_guessed(repo) -> None:
    """`import { forwardReleasedItem } from "@app/proxy"` with no tsconfig and no node_modules.

    It could be a path alias for the target's own file or a package exporting the same name. Both
    readings are consistent with the file, so neither is asserted.
    """
    assert (f"{SRC}/aliasImport.ts", "viaAlias") in _truth(repo, "forwardReleasedItem").undecidable


# --- what it costs an engine to invent a caller ---------------------------------------------------

def test_fabricated_typescript_callers_are_charged(repo) -> None:
    """A name-matcher on `describe` claims the jest file. That is the entire 32-caller failure in
    miniature, and under positives-only truth it cost nothing at all."""
    from score import Scores

    t = _truth(repo, "describe")
    claim = {(f"{SRC}/jestGlobals.ts", "<module>")}

    before = Scores()
    before.add(claim, t.calls, t.calls | t.references | t.imports)
    assert before.fp == 0
    assert before.precision is None

    after = Scores()
    after.add(claim, t.calls, t.calls | t.references | t.imports | t.negatives)
    assert after.fp == 1
    assert after.precision == 0.0


def test_the_scorer_reads_both_oracles_through_one_seam() -> None:
    """`score.LANGUAGES` is what lets one scorer, one truth type and one set of arms serve both."""
    from score import LANGUAGES

    lang = LANGUAGES["typescript"]()
    lang.prepare(CORPUS)
    qn = lang.target(CORPUS, f"{SRC}/proxy.ts", "describe")
    assert lang.truth(CORPUS, qn).negatives == {(f"{SRC}/jestGlobals.ts", "<module>")}
    assert lang.enclosing(CORPUS, f"{SRC}/callerDirect.ts", 4) == "send"
    assert lang.kinds_at(CORPUS, f"{SRC}/jestGlobals.ts", 9, qn) == {NOT_TARGET}


def test_the_readme_states_this_corpus_size_correctly():
    """`bench/README.md` counts these files in prose, four times, and the count had drifted.

    The corpus gained a file and the sentences describing it did not, so the document explaining
    what the arm covers was describing a tree one file smaller than the one on disk. That is the
    same defect `tests/test_docs_ci_claims.py` guards for the CI claims — a hand-written number
    about a machine-readable fact, with nothing checking the two still agree — and it is worth one
    assertion here because the corpus is the thing those sentences exist to describe.

    The count is DERIVED, never typed: a hand-written expectation is what is being guarded against.
    """
    import re

    readme = (BENCH / "README.md").read_text(encoding="utf-8")
    on_disk = len(list((BENCH / "fixtures" / "corpus_ts" / "src").glob("*.ts")))
    claimed = {int(n) for n in re.findall(r"(\d+) (?:known )?files", readme)}
    # Only the corpus-sized claims are ours to police; the `pathly-adapters` row counts thousands.
    corpus_claims = {n for n in claimed if n < 100}
    assert corpus_claims == {on_disk}, (
        f"bench/README.md claims {sorted(corpus_claims)} file(s) for corpus_ts; "
        f"there are {on_disk} on disk"
    )


# --- class-qualified targets ------------------------------------------------------------------
#
# `bench/README.md` carried the qualified case as an observation for weeks, on the stated grounds
# that scoring it needed "a repository whose truth is establishable" — meaning something smaller
# than the 1,483-file monorepo it was hand-checked on. That diagnosis was wrong, and these tests
# pin what it was actually blocked on: the oracle abstains on every `obj.method(...)` site, and
# every real call site of a method is one of those. Twenty-four files were never the problem.

def test_a_qualified_target_decides_the_receiver_its_class_body_declares(repo) -> None:
    """`this.chain.resolve(...)` where the class states `private readonly chain: StrategyChain`.

    The declaration is a fact in the text, not an inference, which is the whole basis for deciding
    it. `StrategyAgent` reaches the target through a barrel re-export, so the class's alias chain
    has to be followed before the annotation can be matched — the qualifier's equivalent of the
    indirection `settleFacade.ts` puts in front of `settleQueue`.
    """
    t = _truth(repo, "StrategyChain.resolve", f"{SRC}/strategyChain.ts")

    assert (f"{SRC}/chainAgents.ts", "StrategyAgent.route") in t.calls, _labels(t)
    assert t.coverage == 1.0, f"the qualified case must be fully decidable: {_labels(t)}"
    assert not t.undecidable, _labels(t)


def test_a_field_holding_a_different_class_is_a_proven_non_caller(repo) -> None:
    """`FallbackAgent.route` spells its call `this.chain.resolve(q)` — identical to the true caller,
    character for character. What separates them is the declared class, and nothing else.

    Without this half the arm could only reward finding callers, and an engine answering
    "everything" would score as well as one answering correctly.
    """
    t = _truth(repo, "StrategyChain.resolve", f"{SRC}/strategyChain.ts")

    assert (f"{SRC}/chainAgents.ts", "FallbackAgent.route") in t.negatives, _labels(t)


def test_a_promise_executor_is_a_proven_non_caller_of_a_method(repo) -> None:
    """The population that supplied 43 of 48 reported callers on the repository this is modelled
    from. `new Promise((resolve, reject) => ... resolve(x))` binds the name right there, and a
    method is never reached as a bare name — so these are decidable negatives, not abstentions.
    """
    t = _truth(repo, "StrategyChain.resolve", f"{SRC}/strategyChain.ts")

    for fn in ("fetchLater", "settleSoon", "firstOf"):
        assert (f"{SRC}/promiseExecutors.ts", fn) in t.negatives, (fn, _labels(t))


def test_a_structural_receiver_type_is_still_an_abstention(repo) -> None:
    """The limit, and it is the point of the design rather than a gap in it.

    `memberCall.ts` annotates its field with an object type, not a class. That states the shape of
    the receiver and not its identity, so the site stays undecidable — the same answer the oracle
    gave before qualified targets existed. A rule that guessed here would manufacture exactly the
    kind of unfounded truth this harness is built to measure other tools against.
    """
    t = _truth(repo, "forwardReleasedItem")

    assert (f"{SRC}/memberCall.ts", "Relay.go") in t.undecidable, _labels(t)


def test_an_unqualified_target_still_abstains_on_every_property_access(repo) -> None:
    """The backward-compatibility pin. Asking for the bare leaf name must behave exactly as it did
    before qualified targets existed: a property access is undecidable without a qualifier to
    decide it against, because there is then nothing to compare the declared class to.
    """
    t = _truth(repo, "resolve", f"{SRC}/strategyChain.ts")

    for scope in ("StrategyAgent.route", "FallbackAgent.route"):
        assert (f"{SRC}/chainAgents.ts", scope) in t.undecidable, (scope, _labels(t))
    assert not t.calls, _labels(t)


def test_a_receiver_typed_by_an_unresolvable_import_abstains(tmp_path) -> None:
    """The boundary of the negative branch, and the reason it is drawn conservatively.

    A field annotated with a class imported from a specifier that resolves to nothing is NOT a
    proven different class — the specifier could be a path alias for the target's own file, which is
    exactly what `aliasImport.ts` pins for an unqualified target. Claiming a negative here would
    manufacture a wrong truth, and a wrong truth in this file silently rescores an engine.

    Built as its own tree rather than added to the corpus: the corpus is pinned file-by-file by the
    tests above and by a derived count in `bench/README.md`, and this boundary needs a `tsconfig`-
    less path alias that would change what several of those files mean.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "chain.ts").write_text(
        "export class StrategyChain {\n  resolve(x: string): string { return x; }\n}\n")
    (src / "agent.ts").write_text(
        "import { StrategyChain } from '@aliased/chain';\n"
        "export class Agent {\n"
        "  private readonly chain: StrategyChain;\n"
        "  constructor() { this.chain = null as never; }\n"
        "  go(q: string) { return this.chain.resolve(q); }\n"
        "}\n")

    t = oracle_ts.truth_for(str(tmp_path), "src/chain.ts::StrategyChain.resolve")

    assert ("src/agent.ts", "Agent.go") in t.undecidable, _labels(t)
    assert ("src/agent.ts", "Agent.go") not in t.negatives, _labels(t)
