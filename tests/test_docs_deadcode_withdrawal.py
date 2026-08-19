"""The docs and `_WITHDRAWN_OPS` must not drift apart.

`_WITHDRAWN_OPS` (graph.py) is the single source of truth for which ops are withdrawn from the
product. The README and the live reference docs under `docs/` are meant to say so wherever they
mention a withdrawn op — a capability table that silently loses a row, or keeps advertising an op
that actually safe-nulls, tells a returning user nothing true. This test does not hand-type the
withdrawn op population (that is exactly the recurring defect class this project keeps re-learning
not to reproduce, per CHANGELOG `0.15.4`): it imports `_WITHDRAWN_OPS` and checks every live
markdown doc against it.

Historical documents are exempt on purpose: `CHANGELOG.md` narrates the state AT THE TIME of each
dated entry (rewriting old entries to match today would falsify the history), and
`docs/eval-*.md` / `docs/adr/**` are dated evaluation/decision records, not living reference docs a
user reaches for to learn what an op does today.
"""
from __future__ import annotations

from pathlib import Path

from codeintel.providers.graph import _WITHDRAWN_OPS

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories/files that document HISTORY rather than current behaviour. A dated eval report or an
# ADR is allowed to say "at the time, X worked like this" without also carrying today's withdrawal
# note — that is not drift, that is an accurate record of the past.
_HISTORICAL_PATHS = (
    _REPO_ROOT / "CHANGELOG.md",
    _REPO_ROOT / "docs" / "adr",
    _REPO_ROOT / "reports",
)


def _is_historical(path: Path) -> bool:
    if any(path == p or p in path.parents for p in _HISTORICAL_PATHS):
        return True
    # `docs/eval-*.md` — dated point-in-time evaluation reports.
    return path.parent == _REPO_ROOT / "docs" and path.name.startswith("eval-")


def _live_markdown_files() -> list[Path]:
    candidates = [_REPO_ROOT / "README.md", *(_REPO_ROOT / "docs").rglob("*.md")]
    return sorted(p for p in candidates if p.is_file() and not _is_historical(p))


def test_withdrawn_ops_actually_exist_and_are_non_empty():
    # A population check on the fixture itself: if `_WITHDRAWN_OPS` were ever emptied silently,
    # every check below would pass vacuously and certify nothing.
    assert _WITHDRAWN_OPS, "_WITHDRAWN_OPS is empty — the checks below would pass vacuously"
    for op, hint in _WITHDRAWN_OPS.items():
        assert isinstance(op, str) and op
        assert op in hint, (op, "the hint must name the op it is about")
        assert len(hint) > 80, (op, "a withdrawal with no explanation is barely better than none")


def test_a_withdrawal_hint_never_offers_a_way_to_run_an_op_that_cannot_run():
    """A hint may only advertise the `CODEINTEL_ENABLE_UNVERIFIED_OPS` opt-in if the opt-in exists
    AND the op has something to opt in to.

    `deadcode` was withdrawn-but-present for a while, reachable behind that flag; it is now retired
    and the flag is gone. A hint still naming it would send a reader to an environment variable that
    changes nothing, which costs more trust than saying nothing would. Derived from the class and
    the gate's own source, so it cannot be satisfied by a flag that is documented but unread."""
    import inspect

    from codeintel.providers.graph import GraphProvider

    gate = inspect.getsource(GraphProvider.build_result)
    flag_is_consulted = "_unverified_ops_enabled" in gate
    for op, hint in _WITHDRAWN_OPS.items():
        if "ENABLE_UNVERIFIED" not in hint.upper():
            continue
        assert flag_is_consulted, (
            f"{op}'s hint advertises CODEINTEL_ENABLE_UNVERIFIED_OPS, but build_result no longer "
            f"consults it — the flag enables nothing")
        assert hasattr(GraphProvider, f"_op_{op}"), (
            f"{op}'s hint advertises an opt-in, but there is no `_op_{op}` left to run")


def test_live_docs_do_not_describe_a_RUNNING_op_as_withdrawn():
    """The same drift pointing the other way, which is the half no test covered.

    Every check here reads `_WITHDRAWN_OPS` and looks for the word "withdrawn"; on the day an op is
    reinstated they all keep passing while the docs still tell a reader it refuses to run — the
    exact shape of the README's CI claim that `0.15.5` had to correct. So: a table row naming an op
    that DOES run must not call it withdrawn. Rows that also name a withdrawn op are skipped, since
    those rows are about the withdrawal."""
    from codeintel.providers.graph import _GRAPH_OPS, _WITHDRAWN_OPS

    running = sorted(set(_GRAPH_OPS) - set(_WITHDRAWN_OPS))
    assert running, "no running graph ops — this check would pass vacuously"
    for path in _live_markdown_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue
            if "withdrawn" not in line.lower():
                continue
            if any(f"`{w}`" in line for w in _WITHDRAWN_OPS):
                continue
            named = [op for op in running if f"`{op}`" in line]
            assert not named, (
                f"{path}:{lineno} calls {named} withdrawn, but {named} runs: {line!r}")


def test_live_docs_mark_every_withdrawn_op_as_withdrawn():
    """Any live doc mentioning a withdrawn op's name must also say it is withdrawn.

    File-level, not line-level: this catches the two drift shapes that actually matter — a new doc
    that lists the op as available, and an existing doc that has the mention but has lost the
    withdrawal note entirely (e.g. the whole caveat section was deleted). It will not catch a
    withdrawal note surviving elsewhere in a file while one specific mention (say, a capability
    table row) regresses to advertising the op as available — see the row-level test below for that.
    """
    files = _live_markdown_files()
    assert len(files) >= 5, f"suspiciously few live docs found: {files}"

    for op in _WITHDRAWN_OPS:
        offenders = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            if op not in text:
                continue
            if "withdrawn" not in text.lower():
                offenders.append(path)
        assert not offenders, (
            f"{op!r} is mentioned in {offenders} with no 'withdrawn' anywhere in the file — "
            f"a reader would not learn it was pulled"
        )


def test_live_docs_do_not_present_a_withdrawn_op_as_an_available_table_row():
    """A markdown table row naming a withdrawn op must itself say "withdrawn" — the row is the
    part of a doc most likely to be read in isolation (a skim of the capability table), so the
    note has to be ON the row, not merely somewhere else in the file."""
    files = _live_markdown_files()
    for op in _WITHDRAWN_OPS:
        backtick = f"`{op}`"
        for path in files:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if not (stripped.startswith("|") and stripped.endswith("|")):
                    continue
                if backtick not in line:
                    continue
                assert "withdrawn" in line.lower(), (
                    f"{path}:{lineno} presents {op!r} in a table row with no withdrawal note: "
                    f"{line!r}"
                )
