"""The verified-caller precision gate: the threshold, and the three ways it can answer.

`docs/eval-2026-09-10-readiness.md` Phase 6 asks for "measured verified-caller precision above an
agreed threshold, preferably 95% or higher". The threshold was agreed at **95% on 2026-09-18**.
Until then the gate could not be failed, and a gate that cannot be failed cannot be passed either —
it just prints numbers.

The case this file exists for is the third one. A gate that reports PASS when nothing was measured
converts "we could not ask" into "we asked and it was fine", which is the substitution the whole
benchmark exists to catch in the tools it measures. Committing it here — in the thing doing the
catching — would be the funniest possible version of this repository's recurring defect, so the
unmeasurable case fails loudly and this is the test that says so.
"""
from __future__ import annotations

import pathlib
import sys

BENCH = pathlib.Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(BENCH))

from score import VERIFIED_PRECISION_FLOOR, Scores, _gate


def _scores(tp: int, fp: int) -> Scores:
    s = Scores()
    s.tp, s.fp, s.symbols = tp, fp, tp + fp
    return s


def test_the_agreed_threshold_is_ninety_five_percent():
    """Pinned, because it is a DECISION rather than a measurement.

    Nothing else in the tree records that someone chose this number, so a silent edit would leave
    the readiness doc claiming a floor the benchmark no longer enforces — the two-fields-one-fact
    drift this project keeps finding. Changing it should require changing this line too."""
    assert VERIFIED_PRECISION_FLOOR == 0.95


def test_exactly_at_the_floor_passes():
    """`>= 95%`, not `> 95%`. "Above an agreed threshold, preferably 95% or higher" reads the
    boundary as acceptable, and a gate that failed at exactly its own stated floor would be
    enforcing 95.000…1% while documenting 95%."""
    line, code = _gate(_scores(tp=19, fp=1), gated=True, repo="x")   # 95.0%
    assert code == 0
    assert "PASS" in line and "95%" in line


def test_just_below_the_floor_fails():
    line, code = _gate(_scores(tp=94, fp=6), gated=True, repo="x")   # 94.0%
    assert code == 1
    assert "FAIL" in line


def test_an_unmeasured_arm_fails_rather_than_passing_on_an_empty_population():
    """THE test in this file. No claims at all means no precision — and "no precision" must never
    render as a pass. `Scores.precision` returns None here, which is falsy, so the obvious
    `if precision >= floor` spelling would have raised, and the obvious defensive `precision or 0`
    would have failed for the right answer with the wrong reason. It is named explicitly."""
    line, code = _gate(_scores(tp=0, fp=0), gated=True, repo="x")

    assert code == 1
    assert "NOT MEASURED" in line
    assert "Refusing to call that a pass" in line


def test_an_ungated_arm_says_it_was_not_applied_rather_than_staying_silent():
    """`corpus-ts` is excluded from the floor because it is a deliberately adversarial fixture, and
    excluding the arm that fails is a suspicious move in general. So the verdict line still prints
    on every run and names the exclusion — silence would be indistinguishable from a pass."""
    line, code = _gate(_scores(tp=3, fp=9), gated=False, repo="corpus-ts")   # 25%, well under

    assert code == 0
    assert "not applied" in line and "corpus-ts" in line
    assert "PASS" not in line, "an ungated arm must not read as a pass"


def test_every_gated_repo_in_the_bench_is_a_real_tree_not_the_fixture():
    """The exclusion list is one entry long and should stay that way by accident of nobody adding
    to it, not by accident of nobody checking. A second ungated arm is how a gate quietly stops
    covering the thing it was written for."""
    import run as bench_run

    ungated = [k for k, (_root, _t, _lang, gated) in bench_run.REPOS.items() if not gated]
    assert ungated == ["corpus-ts"], (
        f"{ungated} are excluded from the precision floor. Only the checked-in smoke fixture "
        f"should be — anything else is an engine measurement being exempted from its own gate.")
