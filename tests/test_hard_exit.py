"""The session-end hard exit must preserve pytest's exit code exactly.

`tests/conftest.py` calls `os._exit(session.exitstatus)` in a `trylast pytest_unconfigure`, so the
process never finalises `onnxruntime` and the native teardown race that aborted four CI runs with
exit 134 cannot reach the exit code. See the block comment there for why that layer was chosen over
chasing the last Python reference to every `InferenceSession`.

**This file is the reason that hook is safe to keep.** If it ever stops propagating the status, the
failure mode is silent and total: every run exits 0, CI goes permanently green, and nothing else in
the suite would notice — a broken gate that looks like a working one, which is the exact shape of
defect the layers check was built to prevent elsewhere in this repo.

Each case runs pytest in a SUBPROCESS, because the hook under test calls `os._exit`: asserting on it
in-process would kill the test runner rather than fail a test. The real `tests/conftest.py` is loaded
into those subprocesses with `-p tests.conftest`, so what is measured is the shipped hook and not a
copy of it.

The `--cov-fail-under` case is not padding. It is the case that caught a wrong first implementation:
reading the status from `pytest_sessionfinish`'s `exitstatus` argument reported **0** for a coverage
failure, because the terminal reporter re-derives the final status after the coverage summary runs.
Only reading `session.exitstatus` in `pytest_unconfigure` sees the real value.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _run_pytest(*args: str) -> subprocess.CompletedProcess:
    """Run a nested pytest with the real conftest loaded as a plugin, from the repo root."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-cov",
         "-p", "no:cacheprovider", "-p", "tests.conftest", *args],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )


@pytest.fixture
def probe(tmp_path: Path):
    """Write a throwaway test file outside `tests/` and return its path."""
    def _write(body: str, name: str = "test_probe.py") -> str:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return str(path)
    return _write


def test_a_passing_run_still_exits_zero(probe):
    result = _run_pytest(probe("def test_ok():\n    assert True\n"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_failing_test_still_exits_one(probe):
    """The whole point of the hook is to stop a green suite reporting failure. It must not also stop
    a red suite reporting failure."""
    result = _run_pytest(probe("def test_bad():\n    assert False\n"))
    assert result.returncode == 1, result.stdout + result.stderr


def test_an_error_during_collection_still_exits_two(probe):
    """A module that cannot even be imported is a usage/collection error, and pytest distinguishes it
    from a test failure. Collapsing the two would hide a broken test file."""
    result = _run_pytest(probe("import a_module_that_does_not_exist\n"))
    assert result.returncode == 2, result.stdout + result.stderr


def test_no_matching_tests_still_exits_five(probe):
    result = _run_pytest(probe("def test_ok():\n    assert True\n"), "-k", "nothing_matches_this")
    assert result.returncode == 5, result.stdout + result.stderr


def test_a_missing_path_still_exits_four():
    """Exit 4 is a usage error, raised before a session exists — so this case goes through the REAL
    conftest auto-discovery rather than `-p tests.conftest`.

    Two reasons. The `-p` harness cannot express it at all: an unresolvable argument makes pytest fall
    back to `testpaths = tests`, which auto-discovers the same conftest `-p` already registered, and
    pluggy rejects the double registration with an error of its own (exit 1) before anything under
    test runs. And it does not need the harness: pytest never calls `pytest_sessionstart` for a usage
    error, so `_final_session` stays empty and the hook returns without touching the status. This
    asserts that, through the path CI actually uses.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
         "tests/definitely_absent_file.py"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 4, result.stdout + result.stderr


def test_a_coverage_failure_still_exits_one(probe):
    """The case that caught a wrong first implementation — see this module's docstring.

    `--no-cov` is dropped here on purpose, and an unreachable floor forces the failure.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "--cov=codeintel", "--cov-fail-under=100",
         probe("def test_ok():\n    assert True\n")],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    # And the report itself must survive the hard exit — `os._exit` skips buffer flushing, so a
    # missing flush would silently swallow the very summary a CI reader needs.
    assert "Required test coverage" in result.stdout


def test_the_summary_survives_the_hard_exit(probe):
    """`os._exit` does not flush stdio. Without the explicit flush the hook would trade a spurious
    failure for a silent one."""
    result = _run_pytest(probe("def test_ok():\n    assert True\n"))
    assert "1 passed" in result.stdout, result.stdout + result.stderr


def test_the_escape_hatch_restores_normal_finalisation(probe):
    """`CODEINTEL_NO_HARD_EXIT=1` exists for anyone debugging finalisation itself, so it must still
    produce a correct exit code by the ordinary path."""
    import os

    env = {**os.environ, "CODEINTEL_NO_HARD_EXIT": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
         "-p", "tests.conftest", probe("def test_ok():\n    assert True\n")],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
