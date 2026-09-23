"""`corpus_ts_typed` must differ from `corpus_ts` by a `tsconfig.json` and by nothing else.

The arm was built to answer one question — can a language server resolve a class-qualified method's
receiver when the tree has a project file? Its first run said no, for a reason that was not the
project file: `lsp._split_target_file_hint` returned early on a target with no `@file`, so the
dotted name reached Serena verbatim while Serena name paths are slash-separated
(`find_symbol("StrategyChain/resolve")` resolves it exactly). With that fixed, the answer is yes:
both LSP arms score the pair at 100% here, while `corpus_ts`, lacking only the tsconfig, still
reports them unanswered.

That makes these tests load-bearing. This is the tree the fix is measured on, and the measurement
only means anything if the tsconfig is still the only difference when someone comes back to it.

The answer is only worth having if the two corpora differ by exactly one thing. If someone edits a
source in one tree and not the other — fixing a typo, tightening a type, adding a caller — the arm
keeps producing numbers and silently stops being a controlled comparison, which is worse than not
running it: a difference would then be attributable to the tsconfig or to the edit, and nothing in
the output would say which. Neither corpus is large enough for that drift to be visible by reading.

So the invariant is asserted rather than intended. These tests are cheap, they run in CI with no
backend, and they fail the moment the comparison stops being one.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

_BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
# `bench/` is a flat script directory, not a package: `oracle_ts` imports `oracle_py` as a sibling,
# which resolves only with `bench/` itself on the path. Same two lines as `test_bench_oracle_ts.py`
# and `test_bench_gate.py` — the alternative is importing these as `bench.*`, which breaks that
# sibling import at its first line.
sys.path.insert(0, _BENCH)
_UNTYPED = os.path.join(_BENCH, "fixtures", "corpus_ts")
_TYPED = os.path.join(_BENCH, "fixtures", "corpus_ts_typed")

def _sources(root: str) -> dict[str, bytes]:
    src = os.path.join(root, "src")
    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(src):
        for f in files:
            full = os.path.join(dirpath, f)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, src)] = fh.read()
    return out


def test_both_corpora_hold_the_same_source_files():
    """The whole tree, globbed on both sides — not a named subset.

    This arm first held only the five files that carry the class-qualified case, and this test
    compared exactly those five. That made the comparison two variables, not one: a tsconfig AND
    24 fewer files, so fewer names for `tsserver` to see and collide on. A review caught it. Globbing
    both trees is the only form of this check that a file added on either side cannot slip past."""
    typed, untyped = _sources(_TYPED), _sources(_UNTYPED)
    assert untyped, "corpus_ts/src is empty — the comparison has nothing to compare"
    assert sorted(typed) == sorted(untyped), (
        f"only in corpus_ts: {sorted(set(untyped) - set(typed))}; "
        f"only in corpus_ts_typed: {sorted(set(typed) - set(untyped))}. "
        "The typed arm is corpus_ts plus a tsconfig; copy the file across, or remove it from both.")


def test_every_source_is_byte_identical():
    """Byte-for-byte, not "equivalent". A whitespace or comment difference is still a difference in
    what `tsserver` parses, and this comparison has no budget for judgement calls about which
    differences are harmless."""
    typed, untyped = _sources(_TYPED), _sources(_UNTYPED)
    drifted = sorted(n for n in untyped if n in typed and typed[n] != untyped[n])
    assert not drifted, (
        f"corpus_ts_typed/src has drifted from corpus_ts in {drifted}. The typed arm is a "
        "controlled comparison and the control is that these files are copies. Re-copy them, or "
        "if the change is wanted, make it in BOTH.")


def test_the_tsconfig_is_the_variable_and_is_present_on_exactly_one_side():
    """The experiment in one assertion. `corpus_ts`'s missing tsconfig is deliberate and
    load-bearing for the oracle's unresolvable-specifier guard (see bench/README.md), so this also
    guards against someone 'fixing' the untyped corpus by adding one and collapsing both arms into
    the same measurement."""
    for name in ("tsconfig.json", "jsconfig.json"):
        assert not os.path.exists(os.path.join(_UNTYPED, name)), (
            f"corpus_ts must NOT have a {name} — its absence is what the oracle's "
            "unresolvable-specifier guard bites on, and it is what makes the typed arm a "
            "comparison rather than a duplicate."
        )
    assert os.path.exists(os.path.join(_TYPED, "tsconfig.json"))


def test_the_tsconfig_declares_no_path_aliases():
    """A `paths` map would be a second variable: it changes how the ORACLE resolves specifiers
    (`oracle_ts._tsconfig_paths` reads exactly this file), not just how tsserver does. Every import
    in the corpus is relative, so there is nothing for an alias map to do here except widen
    what a difference between the arms could be attributed to."""
    pytest.importorskip("tree_sitter_language_pack")   # oracle_ts parses on import
    from oracle_ts import _tsconfig_paths

    base_url, paths = _tsconfig_paths(_TYPED)
    assert not paths, f"corpus_ts_typed/tsconfig.json declares path aliases: {paths}"
    assert not base_url or base_url == _TYPED, base_url


def test_the_tsconfig_survives_the_oracles_own_jsonc_reader():
    """It carries a `"//"` comment block explaining why it exists, which is legal JSON but unusual.
    `_tsconfig_paths` strips JSONC comments and trailing commas before parsing; a file it cannot
    read yields no aliases *silently*, which would make the previous test pass for the wrong
    reason. Parse it independently and require that it really is a tsconfig."""
    with open(os.path.join(_TYPED, "tsconfig.json"), encoding="utf-8") as fh:
        raw = fh.read()
    parsed = json.loads(raw)

    assert "compilerOptions" in parsed
    assert parsed.get("include"), "an empty `include` would give tsserver no project to build"


def test_both_corpora_are_registered_as_bench_arms():
    """The fixture is inert until `run.py` knows about it, and a fixture nobody runs is a directory
    of dead files that still has to be kept in sync by the tests above."""
    with open(os.path.join(_BENCH, "run.py"), encoding="utf-8") as fh:
        src = fh.read()

    for key in ('"corpus-ts"', '"corpus-ts-typed"'):
        assert re.search(rf"^\s*{re.escape(key)}:\s*\(", src, re.M), f"{key} is not a REPOS key"


def test_the_typed_arm_scores_only_the_class_qualified_targets():
    """Its whole reason to exist. The other four `corpus-ts` symbols are scored one arm up; copying
    them here would buy a second number for an answered question and four more hand-maintained
    truths. Both targets carry a class qualifier — a dot whose left side is a class, which is the
    shape `run.py`'s own header admits and the shape the ledger item is about."""
    from run import REPOS

    root, targets, language, gated = REPOS["corpus-ts-typed"]

    assert root == _TYPED
    assert language == "typescript"
    assert gated is False, "a maximally adversarial hand-written fixture is not a production gate"
    assert [t for _, t in targets] == ["StrategyChain.resolve", "FallbackChain.resolve"]
    for path, target in targets:
        assert "." in target and target.split(".")[0][:1].isupper(), (
            f"{target} does not look class-qualified; this arm scores only that shape"
        )
        assert os.path.exists(os.path.join(_TYPED, path)), path
