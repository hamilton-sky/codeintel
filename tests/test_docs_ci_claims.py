"""The README's claims about CI must match the workflow that actually runs.

This guards a drift that has now happened in BOTH directions. The `deadcode` case was the
optimistic one: the docs advertised a capability the product had withdrawn. This is the pessimistic
one — the README told readers the graph and LSP backends were "not installed in CI, so those two
engines are exercised against hand-authored mocks", long after dedicated contract jobs had been
added that install the real backends and run against them. Understating assurance is a smaller sin
than overstating it, but it is the same defect: a hand-written claim about a machine-readable fact,
with nothing checking the two still agree.

Facts are derived from `.github/workflows/ci.yml`, never typed here — a hand-typed expectation is
the thing being guarded against. Parsed as text rather than with PyYAML, which is present in the
dev environment but is not a declared dependency, so importing it would make this test the reason a
clean install fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"


def _job_blocks(text: str) -> dict[str, str]:
    """``{job_name: body}`` for each top-level job — a 2-space key under `jobs:`."""
    lines = text.splitlines()
    starts: list[tuple[int, str]] = [
        (i, m.group(1))
        for i, line in enumerate(lines)
        if (m := re.match(r"^  ([a-z0-9][a-z0-9_-]*):\s*$", line))
    ]
    blocks: dict[str, str] = {}
    for idx, (line_no, name) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        blocks[name] = "\n".join(lines[line_no:end])
    return blocks


@pytest.fixture(scope="module")
def ci_text() -> str:
    if not CI.exists():
        pytest.skip("ci.yml not present")
    return CI.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_the_workflow_actually_has_contract_jobs(ci_text):
    # Non-vacuity. Every assertion below is of the form "for each contract job ..." and would pass
    # trivially against a workflow that has none — which is exactly the state this file exists to
    # notice, so it is asserted rather than assumed.
    contract = [n for n in _job_blocks(ci_text) if "contract" in n]
    assert contract, "no *-contract jobs found in ci.yml — the parser or the workflow changed"


def test_readme_names_every_contract_job(ci_text, readme_text):
    missing = [
        name for name in _job_blocks(ci_text)
        if "contract" in name and name not in readme_text
    ]
    assert not missing, (
        f"ci.yml runs contract job(s) {missing} that the README never mentions — the docs "
        "understate what is actually verified"
    )


def test_readme_does_not_claim_backends_are_absent_from_ci(ci_text, readme_text):
    # The precise stale sentence, tied to the fact that falsifies it.
    installs_graph_backend = "codebase-memory-mcp==" in ci_text
    if installs_graph_backend:
        assert "not installed in CI" not in readme_text, (
            "ci.yml installs the pinned graph backend, but the README still says the backends are "
            "'not installed in CI' — the claim was true before the contract jobs existed"
        )


def test_readme_reflects_which_contract_jobs_actually_gate(ci_text, readme_text):
    """A non-gating job must not read as a gating one.

    `continue-on-error` is the difference between "a serena regression turns CI red" and "a serena
    regression is a note someone may notice". A reader deciding how much to trust the LSP path
    needs that distinction, so if any contract job carries the flag, the README has to say so.
    """
    soft = [
        name for name, body in _job_blocks(ci_text).items()
        if "contract" in name and re.search(r"^\s*continue-on-error:\s*true\s*$", body, re.M)
    ]
    if soft:
        assert "continue-on-error" in readme_text, (
            f"contract job(s) {soft} are continue-on-error, so they do not gate a release — the "
            "README describes the contract coverage without saying which parts are non-gating"
        )
