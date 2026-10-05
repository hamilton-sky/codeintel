"""A graph backend that refuses to run must be quoted, not summarised as a timeout.

The incident this file exists for: `codebase-memory-mcp cli list_projects '{}'` exited 1 in about a
second with

    level=info msg=version_cohort.claimed_unheld build=996bad5f...
    codebase-memory-mcp: CBM CLI could not start because a pre-coordination or unverified CBM
    generation is active; close all CBM sessions and commands, then retry

and `codeintel doctor` said "installed but list_projects failed/timed out", with a remediation that
told the reader to run the very command whose output would have explained it. `_run` had captured
the exit code and the stderr and thrown both away; it then LAUNCHED THE SAME FAILING COMMAND A
SECOND TIME through the deprecated raw-JSON form, and recorded a generic "did not answer" over the
top. Three separate losses, each of which left the next reader with less than the one before.

Every other test of the transport stubs `subprocess.run` or `_run` itself, which is why none of them
could see it: a stub returns whatever the author thought a failing backend looks like. These drive a
REAL executable — a few lines of shell written to `tmp_path` and found through `PATH`, so
`shutil.which`, `subprocess.run`, the exit code, the stderr pipe and the timeout kill all run for
real, and the launch count is read from a file the script itself appends to rather than from a mock's
call list.

POSIX only: the fake is a `#!/bin/sh` script.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from codeintel.outcome import Missing
from codeintel.providers.graph import GraphProvider
from codeintel.redact import contains_home_path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake backend is a POSIX shell script")

# The refusal as the real backend printed it, minus the log line (which the cleaner must drop).
_REFUSAL = ("CBM CLI could not start because a pre-coordination or unverified CBM generation is "
            "active; close all CBM sessions and commands, then retry")
_LOG_LINE = "level=info msg=version_cohort.claimed_unheld build=996bad5f0c1e"


def _fake_backend(tmp_path: Path, monkeypatch, body: str) -> Path:
    """Put a `codebase-memory-mcp` on PATH whose behaviour is `body`, and return its launch log.

    Every launch appends its argument list to the log first, so "launched exactly once" is a count
    of lines in a file the executable wrote — not a number a mock reports about itself. The fake's
    directory goes AHEAD of the real PATH: a developer machine with the genuine backend installed
    must not be able to answer in its place."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "launches.log"
    exe = bindir / "codebase-memory-mcp"
    exe.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\n{body}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return log


def _launches(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _refuses(message: str = _REFUSAL, code: int = 1) -> str:
    return f"echo '{_LOG_LINE}' >&2\necho 'codebase-memory-mcp: {message}' >&2\nexit {code}"


# --------------------------------------------------------------------------- the incident, exactly

def test_a_backend_that_refuses_to_start_is_reported_in_its_own_words(tmp_path, monkeypatch):
    log = _fake_backend(tmp_path, monkeypatch, _refuses())

    started = time.monotonic()
    report = GraphProvider().probe(str(tmp_path))
    elapsed = time.monotonic() - started

    assert report["installed"] is True and report["runnable"] is False, report
    assert _REFUSAL in report["detail"], (
        f"the backend's refusal is the one fact that explains this, and the doctor dropped it: "
        f"{report['detail']!r}")
    assert "timed out" not in report["detail"] and "timed out" not in report["remediation"], (
        "an exit inside a second is not a timeout, and saying so sent the reader to the wrong fix")
    assert "claimed_unheld" not in report["detail"], "the `level=info` log line is noise, not the reason"
    assert elapsed < 10, "the refusal arrives in milliseconds; waiting out a budget means it was misread"
    assert len(_launches(log)) == 1, (
        f"the coordination refusal says the backend will not run AT ALL; a second launch of the "
        f"same command through the raw-JSON form can only get the same answer: {_launches(log)}")


def test_the_coordination_refusal_gets_the_lock_recovery_not_a_command_to_rerun(tmp_path, monkeypatch):
    _fake_backend(tmp_path, monkeypatch, _refuses())
    remediation = GraphProvider().probe(str(tmp_path))["remediation"]

    assert "close every codebase-memory-mcp process" in remediation, remediation
    assert "any agent session that owns one" in remediation, (
        "closing the backend also closes the graph tool in a running agent — the reader is about to "
        "do that, and has to be told it is a cost")
    uid = os.getuid() if hasattr(os, "getuid") else "<uid>"
    assert f"cbm-daemon-{uid}" in remediation, remediation
    assert "temp" in remediation, "the directory is named relative to the platform temp dir"
    assert "lock files outlive" in remediation, remediation
    assert "/private/tmp" not in remediation.replace(os.path.realpath("/tmp"), ""), (
        "macOS's temp location was hardcoded; it is computed, so Linux and Windows read their own")
    assert "list_projects" not in remediation, (
        "the doctor swallowed that command's output; telling the reader to run it is the defect")


def test_the_lock_directory_is_named_where_it_actually_is(tmp_path, monkeypatch):
    """When the daemon directory exists the remediation names its real path rather than a
    description of where it might be — the incident's fix was "move this directory aside", and a
    reader should not have to go looking for it."""
    _fake_backend(tmp_path, monkeypatch, _refuses())
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    uid = os.getuid() if hasattr(os, "getuid") else 0
    daemon = tmp_path / f"cbm-daemon-{uid}"
    daemon.mkdir()

    remediation = GraphProvider().probe(str(tmp_path))["remediation"]
    assert str(daemon) in remediation or str(daemon.resolve()) in remediation, remediation


def test_a_refusal_that_is_not_the_coordination_one_does_not_get_its_remediation(tmp_path, monkeypatch):
    _fake_backend(tmp_path, monkeypatch, _refuses("index database is locked by another process", 3))
    report = GraphProvider().probe(str(tmp_path))

    assert "index database is locked by another process" in report["detail"], report
    assert "cbm-daemon" not in report["remediation"], (
        "the daemon-directory procedure is for ONE refusal; offering it for every failure would "
        "send people to move a directory that is not the problem")
    assert "list_projects" not in report["remediation"], report


def test_a_could_not_start_refusal_is_launched_once_but_gets_no_daemon_directory_advice(
        tmp_path, monkeypatch):
    message = "CBM CLI could not start: no writable cache directory"
    log = _fake_backend(tmp_path, monkeypatch, _refuses(message))
    report = GraphProvider().probe(str(tmp_path))

    assert message in report["detail"], report["detail"]
    assert "cbm-daemon" not in report["remediation"], report["remediation"]
    assert "address what the backend says" in report["remediation"], report["remediation"]
    assert len(_launches(log)) == 1, "a backend that will not start will not start the second time"


def test_a_backend_that_exits_silently_says_so_instead_of_inventing_a_reason(tmp_path, monkeypatch):
    log = _fake_backend(tmp_path, monkeypatch, "exit 4")
    report = GraphProvider().probe(str(tmp_path))

    assert "exited 4 on `list_projects` without saying why" in report["detail"], report["detail"]
    assert "refused" not in report["detail"], "there is no refusal to quote"
    assert "no reason" in report["remediation"], report["remediation"]
    assert len(_launches(log)) == 2, "silence is not a refusal to run — the fallback still gets its try"


# --------------------------------------------------------------------------- the other three kinds

def test_a_backend_that_outlasts_the_budget_is_classified_as_a_timeout(tmp_path, monkeypatch):
    # `exec` so the kill lands on `sleep` itself — a shell parent would die and leave the sleep
    # holding the stderr pipe, and `subprocess.run` would then wait out the whole sleep.
    log = _fake_backend(tmp_path, monkeypatch, "exec sleep 30")
    provider = GraphProvider()

    # Not shorter: the first execution of a freshly written file can be held up by the OS for
    # hundreds of milliseconds (macOS scans it), and a budget that expires before the script's
    # first line leaves no launch in the log to count.
    started = time.monotonic()
    report = provider.probe(str(tmp_path), timeout_ms=2000)
    elapsed = time.monotonic() - started

    assert elapsed < 15, "the budget was not honoured"
    miss = provider._last_failure
    assert isinstance(miss, Missing) and miss.kind == "timeout", miss
    assert "did not answer" in report["detail"] and "within 2s" in report["detail"], report["detail"]
    assert "refused" not in report["detail"], "a timeout is not a refusal"
    assert len(_launches(log)) == 1, (
        "the budget was spent on the first launch; a second one has nothing left to run in")


def test_a_missing_binary_is_still_not_installed(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    report = GraphProvider().probe(str(tmp_path))
    assert report["installed"] is False and report["runnable"] is False, report
    assert report["detail"] == "codebase-memory-mcp not found on PATH"


def test_a_binary_that_vanishes_after_detection_is_named_as_not_installed(tmp_path, monkeypatch):
    """Detected at construction, gone by the first call — an upgrade replacing the file, an
    uninstall under a long-lived server. Its own words, not "did not answer"."""
    log = _fake_backend(tmp_path, monkeypatch, "exit 0")
    provider = GraphProvider()
    (tmp_path / "bin" / "codebase-memory-mcp").unlink()

    assert provider._run("list_projects", {}, 5000) is None
    miss = provider._last_failure
    assert isinstance(miss, Missing) and "not installed" in miss.describe(), miss
    assert _launches(log) == [], "nothing was launched, so nothing was counted"


def test_a_reply_that_is_not_json_is_still_unparsable_and_launched_once(tmp_path, monkeypatch):
    log = _fake_backend(tmp_path, monkeypatch, "echo 'this is not any dialect at all'\nexit 0")
    provider = GraphProvider()

    assert provider._run("list_projects", {}, 5000) is None
    assert provider._last_failure is not None and provider._last_failure.kind == "unparsable"
    assert len(_launches(log)) == 1, "the raw-JSON form would only get the same dialect back"


def test_an_unreadable_listing_is_reported_as_unreadable_not_as_a_failure_to_run(tmp_path, monkeypatch):
    _fake_backend(tmp_path, monkeypatch, "echo 'this is not any dialect at all'\nexit 0")
    report = GraphProvider().probe(str(tmp_path))

    assert report["installed"] is True and report["runnable"] is False, report
    assert "read" in report["detail"] and "failed/timed out" not in report["detail"], report["detail"]
    assert "refused" not in report["detail"] and "did not answer" not in report["detail"]


# --------------------------------------------------------------------------- the fallback survives

# `$#` is the number of arguments: 2 for the piped-stdin form (`cli list_projects`), 3 for the
# deprecated raw-JSON form (`cli list_projects '{}'`).
_OLD_BACKEND = (
    'if [ "$#" -eq 2 ]; then\n'
    "  echo 'error: unknown option: expected a JSON argument' >&2\n"
    "  exit 2\n"
    "fi\n"
    "echo '{\"projects\": []}'\n"
)


def test_a_usage_error_still_falls_back_to_the_raw_json_form_and_succeeds(tmp_path, monkeypatch):
    """The other half of "do not double-launch": that rule is for a backend that will not run AT
    ALL. An older backend that rejects the stdin form is the case the fallback exists for, and it
    must keep working — launched twice, answered on the second."""
    log = _fake_backend(tmp_path, monkeypatch, _OLD_BACKEND)
    provider = GraphProvider()

    assert provider._run("list_projects", {}, 5000) == {"projects": []}
    assert provider._last_failure is None, "a call that succeeded must not leave a failure behind"
    assert len(_launches(log)) == 2, _launches(log)


@pytest.mark.parametrize("message", [
    "error: unknown option: expected a JSON argument",
    "usage: codebase-memory-mcp cli <tool> [json]",
    "unknown flag --json",
    "project coordination table is empty",
    "",
])
def test_a_usage_error_is_not_mistaken_for_a_refusal_to_run(message):
    """The no-second-launch rule must be narrow. Every message here is something an older backend
    or an ordinary failure might print, and none of them says the backend will not run at all —
    treating one as a refusal would turn off the fallback that keeps an older backend working."""
    from codeintel.graph_backend import is_coordination_refusal, is_launch_refusal

    assert is_launch_refusal(message) is False
    assert is_coordination_refusal(message) is False


def test_the_two_refusals_are_told_apart():
    from codeintel.graph_backend import is_coordination_refusal, is_launch_refusal

    assert is_launch_refusal(_REFUSAL) and is_coordination_refusal(_REFUSAL)
    # Will not run, but not for the reason that has the daemon-directory fix.
    assert is_launch_refusal("CBM CLI could not start: no writable cache directory")
    assert not is_coordination_refusal("CBM CLI could not start: no writable cache directory")


def test_the_first_detailed_failure_survives_the_retry(tmp_path, monkeypatch):
    """Both forms fail, and they fail differently. The retry used to overwrite what the first
    attempt learned with a generic "did not answer", which is how the refusal was lost even where
    it had been captured."""
    body = (
        'if [ "$#" -eq 2 ]; then\n'
        "  echo 'error: unknown option: expected a JSON argument' >&2\n"
        "  exit 2\n"
        "fi\n"
        "echo 'second form failed too' >&2\n"
        "exit 1\n"
    )
    log = _fake_backend(tmp_path, monkeypatch, body)
    provider = GraphProvider()

    assert provider._run("list_projects", {}, 5000) is None
    miss = provider._last_failure
    assert isinstance(miss, Missing) and "unknown option" in miss.describe(), miss
    assert "did not answer" not in miss.describe(), "a generic message replaced a specific one"
    assert len(_launches(log)) == 2


def test_the_backends_stderr_is_cleaned_and_bounded(tmp_path, monkeypatch):
    """A refusal can be buried: log lines above it, a stack of context below it. What reaches a
    report is the tail with the log noise gone, whitespace collapsed, and a ceiling on its size."""
    noise = "\n".join(f"level=info msg=startup.step_{i} detail=none" for i in range(200))
    padding = "\n".join(f"   context line {i}      with   ragged   spacing" for i in range(80))
    body = (f"cat >&2 <<'EOF'\n{noise}\n{padding}\n"
            f"codebase-memory-mcp:   {_REFUSAL}\nEOF\nexit 1")
    _fake_backend(tmp_path, monkeypatch, body)
    provider = GraphProvider()

    provider._run("list_projects", {}, 5000)
    detail = provider._last_failure.describe()

    assert _REFUSAL in detail, "the message that matters is at the END, so it is the tail that is kept"
    assert "level=info" not in detail and "startup.step" not in detail, detail
    assert "  " not in detail and "\n" not in detail, "whitespace was not collapsed"
    assert len(detail) < 600, f"the cleaned stderr was not bounded: {len(detail)} chars"


# --------------------------------------------------------------------------- redaction

def test_a_home_directory_in_the_backends_stderr_is_redacted(tmp_path, monkeypatch):
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    flattened = str(home).replace("\\", "/").strip("/").replace("/", "-")
    message = (f"cannot open {home}/.cache/codebase-memory-mcp/graph.db: permission denied "
               f"(project {flattened}-repo)")
    _fake_backend(tmp_path, monkeypatch, _refuses(message))

    report = GraphProvider().probe(str(tmp_path))
    rendered = json.dumps(report)

    assert "permission denied" in report["detail"], (
        f"the backend's message is the point, and it is absent: {report['detail']!r}")
    assert not contains_home_path(rendered), (
        f"the home directory reached a report in some form: {rendered}")
    assert "~/.cache/codebase-memory-mcp/graph.db" in report["detail"], report["detail"]


# --------------------------------------------------------------------------- the whole doctor

def test_the_doctor_end_to_end_names_a_refusing_backend_and_calls_it_degraded(tmp_path, monkeypatch):
    """Nothing stubbed between the executable and the report: the real subprocess layer, the real
    probe, the real `run_doctor` and the real renderer. This is the incident, start to finish."""
    from codeintel import doctor

    _fake_backend(tmp_path, monkeypatch, _refuses())
    report = doctor.run_doctor(str(tmp_path))

    graph = report["engines"]["graph"]
    assert graph["installed"] is True and graph["status"] == "fail", graph
    assert _REFUSAL in graph["detail"], graph["detail"]
    assert "graph" in report["degraded"], report["degraded"]

    text = doctor.render_doctor_text(report)
    assert _REFUSAL in text and "degraded" in text, text
    assert "cbm-daemon" in text, "the fix is printed where the reader is looking"
    assert "works without it" not in text, "graph is installed here; that reassurance is for an absent one"


# --------------------------------------------------------------------------- the query envelope

def _project_lister(root: Path, query_failure: str) -> str:
    return (
        'case "$2" in\n'
        f"  list_projects) echo '{{\"projects\":[{{\"name\":\"p\",\"root_path\":\"{root}\"}}]}}'; exit 0;;\n"
        f"  *) echo '{query_failure}' >&2; exit 3;;\n"
        "esac\n"
    )


def test_an_unreachable_backend_envelope_carries_the_refusal_and_the_fix(tmp_path, monkeypatch):
    """The query path reports the same incident the doctor does, and used to say "did not respond
    in time" and point at `list_projects`. An agent has no shell to run that in."""
    _fake_backend(tmp_path, monkeypatch, _refuses())
    result = GraphProvider().build_result("callers", "thing", [], 5000, str(tmp_path))

    assert result["result"] is None and result["reason"] == "backend-unreachable", result
    assert _REFUSAL in result["hint"], result["hint"]
    assert "did not respond in time" not in result["hint"], result["hint"]
    assert "cbm-daemon" in result["hint"], "an agent reading only the envelope needs the fix too"
    assert "list_projects" not in result["hint"], result["hint"]


def test_a_query_the_backend_refused_says_what_it_said(tmp_path, monkeypatch):
    """`list_projects` answers and the QUERY is refused: the envelope's `reason` stays
    `backend-error` and the hint now carries the message instead of a bare "did not answer"."""
    root = tmp_path / "repo"
    root.mkdir()
    _fake_backend(tmp_path, monkeypatch, _project_lister(root.resolve(), "index database is locked"))
    result = GraphProvider().build_result("callers", "thing", [], 5000, str(root))

    assert result["result"] is None, result
    assert result["reason"] == "backend-error", result
    assert "index database is locked" in result["hint"], result["hint"]


def test_a_refusal_after_the_project_resolved_carries_the_fix_like_one_before_it(
        tmp_path, monkeypatch):
    """The coordination lock does not care which call it lands on. `list_projects` answers here and
    the QUERY is refused with the lock message: the envelope used to say "Re-ask, or run `codeintel
    doctor`" — the same advice that sent the original incident round in a circle — while a refusal
    one call earlier carried the recovery. Both come from one sentence now."""
    root = tmp_path / "repo"
    root.mkdir()
    _fake_backend(tmp_path, monkeypatch, _project_lister(root.resolve(), _REFUSAL))
    result = GraphProvider().build_result("callers", "thing", [], 5000, str(root))

    assert result["result"] is None and result["reason"] == "backend-error", result
    assert _REFUSAL in result["hint"], result["hint"]
    assert "cbm-daemon" in result["hint"] and "To fix: close every codebase-memory-mcp" in result["hint"]
    assert "Re-ask, or run" not in result["hint"], result["hint"]
    assert "says nothing about whether your repository is indexed" in result["hint"], result["hint"]


# --------------------------------------------------------------------------- the same refusal, elsewhere

def test_a_version_command_that_failed_reports_no_version(tmp_path, monkeypatch):
    # In the incident state the doctor's version column read `level=info msg=version_cohort.cl`:
    # `_cmd_version` ignored the exit code and took the first line it saw. A failed `--version` did
    # not report a version, whatever it printed.
    from codeintel.doctor import _cmd_version
    _fake_backend(tmp_path, monkeypatch, _refuses())

    assert _cmd_version(["codebase-memory-mcp", "--version"]) is None


def test_a_version_printed_after_a_backend_log_line_is_still_read(tmp_path, monkeypatch):
    # 0.10.x writes `level=…` lines before it answers. A version that follows one is still a version.
    from codeintel.doctor import _cmd_version
    _fake_backend(tmp_path, monkeypatch,
                  "echo 'level=info msg=mem.init budget_mb=8601'\necho 'codebase-memory-mcp 0.10.8'")

    assert _cmd_version(["codebase-memory-mcp", "--version"]) == "0.10.8"


def test_a_refused_background_reindex_logs_the_backends_reason(tmp_path, monkeypatch, caplog):
    # The background reindex logged "timed out, crashed, or rejected the call" for every failure —
    # the same three guesses the doctor used to make — while `_run` now knows which one it was.
    import logging

    from codeintel.reindexer import Reindexer
    _fake_backend(tmp_path, monkeypatch, _refuses())

    with caplog.at_level(logging.WARNING, logger="codeintel.reindexer"):
        Reindexer(enabled=False)._graph_reindex(str(tmp_path))

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert _REFUSAL in logged, logged
    assert "timed out, crashed, or rejected" not in logged, logged
