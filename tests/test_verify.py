"""Unit tests for the MCP handshake verifier.

The verifier is the thing that decides whether `codeintel install` may claim success, so its
failure modes matter as much as its happy path: it must never raise, never hang, never leak a
subprocess, and must give an actionable reason for each distinct way a host launch can fail.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time

from codeintel.verify import verify_stdio_server


def _script(tmp_path, body: str) -> list[str]:
    """Write a fake stdio server and return the argv that runs it."""
    path = tmp_path / "fake_server.py"
    path.write_text(textwrap.dedent(body))
    return [sys.executable, str(path)]


_GOOD_SERVER = """
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "9.9"}}}), flush=True)
        elif msg.get("method") == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": "a.one"}, {"name": "a.two"}]}}), flush=True)
"""


def test_successful_handshake_reports_server_and_tools(tmp_path):
    argv = _script(tmp_path, _GOOD_SERVER)
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=30)
    assert res["ok"] is True
    assert res["tools"] == ["a.one", "a.two"]
    assert res["server"] == {"name": "fake", "version": "9.9"}
    assert "fake 9.9" in res["detail"]


def test_missing_command_is_actionable():
    res = verify_stdio_server("codeintel-does-not-exist-xyz", ["serve"], timeout_s=5)
    assert res["ok"] is False
    assert "not on PATH" in res["detail"]      # the exact failure a real user hits


def test_process_that_exits_immediately_is_detected(tmp_path):
    argv = _script(tmp_path, "import sys; sys.exit(3)")
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=15)
    assert res["ok"] is False and "exited" in res["detail"]


def test_server_that_never_answers_times_out_without_hanging(tmp_path):
    argv = _script(tmp_path, "import time; time.sleep(120)")
    start = time.monotonic()
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=3)
    elapsed = time.monotonic() - start
    assert res["ok"] is False and "initialize" in res["detail"]
    assert elapsed < 20  # bounded by the deadline, not by the server


def test_initialize_error_is_surfaced(tmp_path):
    argv = _script(tmp_path, """
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32600, "message": "nope"}}), flush=True)
    """)
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=15)
    assert res["ok"] is False and "nope" in res["detail"]


def test_server_exposing_no_tools_is_not_ok(tmp_path):
    argv = _script(tmp_path, _GOOD_SERVER.replace(
        '[{"name": "a.one"}, {"name": "a.two"}]', "[]"
    ))
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=15)
    assert res["ok"] is False and "no tools" in res["detail"]


def test_noise_on_stdout_does_not_break_the_handshake(tmp_path):
    """A server that prints a banner before speaking protocol should still verify — the
    alternative is a false negative that blocks a working install."""
    argv = _script(tmp_path, _GOOD_SERVER.replace(
        "    import json, sys",
        '    import json, sys\n    print("starting up...", flush=True)',
        1,
    ))
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=30)
    assert res["ok"] is True and res["tools"] == ["a.one", "a.two"]


def test_initialized_notification_is_sent_before_tools_list(tmp_path):
    """Per spec a server may withhold tools until it sees notifications/initialized."""
    argv = _script(tmp_path, """
        import json, sys
        seen_initialized = False
        for line in sys.stdin:
            msg = json.loads(line)
            method = msg.get("method")
            if method == "initialize":
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "serverInfo": {"name": "strict", "version": "1"}}}), flush=True)
            elif method == "notifications/initialized":
                seen_initialized = True
            elif method == "tools/list":
                tools = [{"name": "ready"}] if seen_initialized else []
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"tools": tools}}), flush=True)
    """)
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=30)
    assert res["ok"] is True and res["tools"] == ["ready"]


def test_subprocess_is_always_reaped(tmp_path):
    """A verifier that leaks a language-server-sized subprocess per run is worse than none."""
    argv = _script(tmp_path, _GOOD_SERVER)
    import subprocess as sp
    before = sp.run([sys.executable, "-c", "pass"])  # noqa: F841  (warm the interpreter)
    verify_stdio_server(argv[0], argv[1:], timeout_s=30)
    # The verifier holds no handle after returning; a lingering child would keep the pipe open.
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=30)
    assert res["ok"] is True


def test_never_raises_on_a_hostile_response(tmp_path):
    argv = _script(tmp_path, """
        import sys
        sys.stdout.write("\\x00\\x01 not json at all\\n")
        sys.stdout.flush()
    """)
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=5)
    assert res["ok"] is False and isinstance(res["detail"], str)


def test_result_shape_is_stable_across_outcomes(tmp_path):
    """Callers (the CLI, the installer result) read these keys unconditionally."""
    argv = _script(tmp_path, _GOOD_SERVER)
    for res in (verify_stdio_server(argv[0], argv[1:], timeout_s=30),
                verify_stdio_server("nope-xyz", [], timeout_s=5)):
        assert {"ok", "tools", "server", "detail"} <= set(res)
        assert isinstance(res["tools"], list) and isinstance(res["detail"], str)


def test_json_payloads_are_newline_delimited(tmp_path):
    """Regression guard on the transport assumption: MCP stdio is newline-delimited JSON,
    not Content-Length framed. A framed writer here would deadlock the handshake."""
    argv = _script(tmp_path, """
        import json, sys
        line = sys.stdin.readline()
        msg = json.loads(line)          # one complete JSON object per line, no framing header
        assert msg["method"] == "initialize"
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"serverInfo": {"name": "nl", "version": "1"}}}), flush=True)
        for line in sys.stdin:
            m = json.loads(line)
            if m.get("method") == "tools/list":
                print(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                                  "result": {"tools": [{"name": "t"}]}}), flush=True)
    """)
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=30)
    assert res["ok"] is True


# --------------------------------------------------------------------------- why no reply
#
# `await_id` returning None used to mean three different things, all reported as a timeout. The worst
# case was a server that DIED on startup being told "no response within 15s" when ~15s had not
# elapsed — the wait ended on EOF almost immediately — which sends whoever is diagnosing it hunting a
# performance problem in a process that exited with a code. `verify_stdio_server` backs the
# registration proof and the release canary, so a wrong reason there is worse than no reason.

def test_a_process_that_exits_is_named_as_exited_not_as_a_timeout(tmp_path):
    """Also the regression this fixes: `Popen.poll()` was a single non-blocking check, and there is a
    real window between the child's stdout closing (which ends `await_id`) and `waitpid` seeing the
    exit code. Under CI load `poll()` lost that race and this came back as a 15s timeout."""
    argv = _script(tmp_path, "import sys; sys.exit(3)")
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=15)
    assert res["ok"] is False
    assert "exited" in res["detail"]
    assert "code 3" in res["detail"]                  # the actual status, not just "it died"
    assert "within 15s" not in res["detail"]          # the claim that was false


def test_a_process_that_exits_is_detected_fast_not_after_the_whole_budget(tmp_path):
    """The bounded wait must close the race without spending the caller's budget: a dead child is
    reported in well under the timeout, or the fix would have traded a wrong message for a slow one.
    """
    argv = _script(tmp_path, "import sys; sys.exit(3)")
    started = time.monotonic()
    verify_stdio_server(argv[0], argv[1:], timeout_s=15)
    assert time.monotonic() - started < 5.0


def test_a_server_that_closes_stdout_but_keeps_running_says_so(tmp_path):
    """The third outcome, which had no message of its own. It is a distinct failure with a distinct
    remedy — the server is alive and needs to answer on stdout, not to be restarted or given longer.

    `os.close(1)` rather than `sys.stdout.close()`: the latter closes Python's buffered wrapper while
    leaving fd 1 open, so the parent never sees EOF and this test would silently exercise the timeout
    path instead. That mistake was made once while building this.
    """
    argv = _script(tmp_path, "import os, time\nos.close(1)\ntime.sleep(30)")
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=3)
    assert res["ok"] is False
    assert "closed stdout" in res["detail"]
    assert "still running" in res["detail"]
    assert "within 3s" not in res["detail"]


def test_a_live_silent_server_still_reports_a_real_timeout(tmp_path):
    """The negative control. Narrowing the timeout message must not remove it — a server that is up,
    holding stdout open and simply not answering IS a timeout, and saying so is correct."""
    argv = _script(tmp_path, "import time; time.sleep(30)")
    res = verify_stdio_server(argv[0], argv[1:], timeout_s=3)
    assert res["ok"] is False
    assert "no `initialize` response within 3s" in res["detail"]


def test_the_reason_is_recorded_by_the_reader_that_knows_it(tmp_path):
    """`gave_up` exists because only the read loop can tell EOF from an expired deadline. Pinned
    directly so a refactor cannot quietly drop the distinction and leave `_why_no_reply` guessing."""
    from codeintel.verify import _Conn

    argv = _script(tmp_path, "import os, time\nos.close(1)\ntime.sleep(30)")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    try:
        conn = _Conn(proc, time.monotonic() + 3.0)
        conn.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert conn.await_id(1) is None
        assert conn.gave_up == "closed"
        assert proc.poll() is None            # and it really is still alive
    finally:
        proc.kill()

    proc2 = subprocess.Popen(_script(tmp_path, "import time; time.sleep(30)"),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    try:
        conn2 = _Conn(proc2, time.monotonic() + 1.0)
        conn2.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert conn2.await_id(1) is None
        assert conn2.gave_up == "deadline"
    finally:
        proc2.kill()
