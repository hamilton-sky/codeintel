"""The benchmark's oracle, pinned against a corpus whose answer is known by construction.

`bench/` had no tests and ran in no CI job, which is an odd place for the component that decides
whether every accuracy claim in this project is true. Worse, `bench/run.py` points at two private
repositories by absolute path, so nobody else could re-run the table at all — including the next time
the graph backend moves and someone needs to know whether a number regressed.

These tests do not replace that run. Real repositories supply the mess (`tests/test_corpus.py` says
why that matters and it applies here). They pin the floor: the labels the oracle must produce, and in
particular the three defect classes that each silently zeroed a result before they were found.

    source root      `src/pkg/mod.py` is imported as `pkg.mod`; calling it `src.pkg.mod` means no
                     import statement ever matches and every site becomes undecidable.
    re-export chain  following a stated `from .mod import name` took one symbol from 17% coverage
                     to 100%.
    proven negative  a bare name the file binds to something ELSE. Filed under `undecidable`, a
                     claim on it cost an engine nothing, and 32 fabricated callers scored 100%.
"""
from __future__ import annotations

import pathlib
import sys

BENCH = pathlib.Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(BENCH))

from oracle_py import (
    CALL,
    IMPORT,
    NOT_TARGET,
    REFERENCE,
    UNDECIDABLE,
    Truth,
    target_from_definition,
    truth_for,
)

CORPUS = str(BENCH / "fixtures" / "corpus")
PKG = "src/corpuspkg"


def _truth(def_file: str, symbol: str) -> tuple[str, Truth]:
    qn = target_from_definition(CORPUS, def_file, symbol)
    return qn, truth_for(CORPUS, qn)


def _labels(t: Truth) -> dict[tuple[str, str], str]:
    """Every judged key, flattened to its final label after the precedence rules have run."""
    out: dict[tuple[str, str], str] = {}
    for kind, keys in ((CALL, t.calls), (REFERENCE, t.references), (IMPORT, t.imports),
                       (NOT_TARGET, t.negatives), (UNDECIDABLE, t.undecidable)):
        for k in keys:
            out[k] = kind
    return out


# --- the module name a file is imported AS -------------------------------------------------------

def test_module_name_comes_from_the_source_root_not_the_repo_root() -> None:
    """`src/` is not a package, so it is not part of the name. Getting this wrong matches nothing."""
    qn = target_from_definition(CORPUS, f"{PKG}/sse.py", "_broadcast")
    assert qn == "corpuspkg.sse._broadcast"
    assert not qn.startswith("src.")


# --- the whole labelled corpus, one assertion per case -------------------------------------------

EXPECTED = {
    # A direct import and a direct call: the control group.
    (f"{PKG}/caller_direct.py", "send"): CALL,
    (f"{PKG}/caller_direct.py", "<module>"): IMPORT,
    # Two stated re-exports deep. Ordinary Python, and the case that defeated the first oracle.
    (f"{PKG}/caller_facade.py", "relay"): CALL,
    (f"{PKG}/caller_facade.py", "<module>"): IMPORT,
    # Passed, never invoked — the `forward_released_item` shape. Not a call; still an impact edge.
    (f"{PKG}/passes_value.py", "install"): REFERENCE,
    (f"{PKG}/passes_value.py", "<module>"): IMPORT,
    # An import is a binding site, not a caller. Counting it as one is `lsp_raw`'s 74%.
    (f"{PKG}/imports_only.py", "<module>"): IMPORT,
    (f"{PKG}/reexport.py", "<module>"): IMPORT,
    (f"{PKG}/facade.py", "<module>"): IMPORT,
    # Proven negatives: the file's own syntax says what the name is, and it is not the target.
    (f"{PKG}/shadow_local.py", "handler"): NOT_TARGET,
    (f"{PKG}/shadow_param.py", "dispatch"): NOT_TARGET,
    (f"{PKG}/shadow_import.py", "emit"): NOT_TARGET,
    # Abstentions: the syntax genuinely does not settle it.
    (f"{PKG}/attr_call.py", "Relay.go"): UNDECIDABLE,
    (f"{PKG}/star.py", "fan_out"): UNDECIDABLE,
    (f"{PKG}/mixed.py", "handle"): UNDECIDABLE,
}


def test_every_corpus_site_gets_the_label_it_was_written_for() -> None:
    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    assert _labels(t) == EXPECTED


def test_the_definition_site_is_not_itself_a_reference() -> None:
    """`def _broadcast` binds a name; it does not mention one. Neither does the rival definition."""
    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    files = {f for f, _ in _labels(t)}
    assert f"{PKG}/sse.py" not in files
    assert f"{PKG}/other.py" not in files


# --- the negative, which is the point of this change ---------------------------------------------

def test_a_shadowed_name_is_proven_not_the_target_rather_than_abstained_on() -> None:
    """Three shadowing forms — a local, a parameter, an import of something else — all decidable."""
    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    assert t.negatives == {
        (f"{PKG}/shadow_local.py", "handler"),
        (f"{PKG}/shadow_param.py", "dispatch"),
        (f"{PKG}/shadow_import.py", "emit"),
    }


def test_a_builtin_accounts_for_the_name() -> None:
    """Nothing imports `corpuspkg.sse.filter`, so a bare `filter(...)` is provably the builtin."""
    _, t = _truth(f"{PKG}/sse.py", "filter")
    assert t.negatives == {(f"{PKG}/shadow_builtin.py", "evens")}
    assert not t.calls


def test_an_unbound_global_stays_undecidable_in_python() -> None:
    """The `describe` shape. Python permits another module to install a global, so the syntax
    cannot rule the target out — the abstention here is deliberate, not an oversight. The same
    shape IS decidable in TypeScript, where a module-scope symbol is reachable only by import."""
    _, t = _truth(f"{PKG}/sse.py", "describe")
    assert t.undecidable == {(f"{PKG}/injected.py", "suite")}
    assert not t.negatives


def test_doubt_anywhere_in_a_key_outranks_a_proven_negative() -> None:
    """`mixed.py` binds the name locally AND reaches an unreadable attribute, in one function.

    Scoring it as a proven non-caller would charge an engine a false positive for a claim that
    might be correct — the mirror of the defect this change fixes, and just as wrong."""
    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    key = (f"{PKG}/mixed.py", "handle")
    assert key in t.undecidable
    assert key not in t.negatives


# --- what it costs an engine to invent a caller --------------------------------------------------

def test_fabricated_callers_are_now_charged_instead_of_dropped() -> None:
    """The regression this whole change exists for.

    There are two ways to invent a caller, and the benchmark used to catch only one of them.

    Claiming an IMPORT or a REFERENCE as a call was always charged, because the oracle had judged
    those sites — that is `lsp_raw`'s measured 74% precision, import lines counted as callers.
    Claiming a bare name the file binds to something else was free: the site was `undecidable`, so
    `claimed & decidable` dropped it. That is the shape of the worst failure seen in this project,
    32 invented callers for `describe`, and it scored 100%.

    So this asserts the blind half specifically: the three shadowed files, claimed on their own.
    """
    from score import Scores

    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    fabrications = {
        (f"{PKG}/shadow_local.py", "handler"),
        (f"{PKG}/shadow_param.py", "dispatch"),
        (f"{PKG}/shadow_import.py", "emit"),
    }

    before = Scores()
    before.add(fabrications, t.calls, t.calls | t.references | t.imports)
    assert before.fp == 0                     # every claim dropped as unjudged
    assert before.precision is None           # the symbol left the population entirely

    after = Scores()
    after.add(fabrications, t.calls, t.calls | t.references | t.imports | t.negatives)
    assert after.fp == 3
    assert after.precision == 0.0


def test_the_fabrication_class_the_benchmark_already_caught_still_costs() -> None:
    """A name-matcher claiming every mention: two real callers, and four wrong ones.

    Three are the newly-decidable shadows; the fourth is `passes_value.py`, where the symbol is
    passed rather than called. That one was always charged, and must stay charged — the two
    failure modes are independent and this pins both at once.
    """
    from score import Scores

    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    every_mention = {k for k in _labels(t) if k[1] != "<module>"}
    scored = Scores()
    scored.add(every_mention, t.calls, t.calls | t.references | t.imports | t.negatives)

    assert (scored.tp, scored.fp, scored.fn) == (2, 4, 0)
    assert scored.precision == 1 / 3
    assert scored.recall == 1.0               # a name-matcher misses nothing; it over-claims


def test_coverage_reflects_the_sites_the_oracle_can_now_decide() -> None:
    """Coverage is reported on every run, so it has to count negatives as decided — they are."""
    _, t = _truth(f"{PKG}/sse.py", "_broadcast")
    assert t.decided == len(EXPECTED) - len(t.undecidable)
    assert 0.0 < t.coverage < 1.0                                    # three abstentions remain
