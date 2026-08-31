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
