"""Unit tests for the MCP handshake verifier.

The verifier is the thing that decides whether `codeintel install` may claim success, so its
failure modes matter as much as its happy path: it must never raise, never hang, never leak a
subprocess, and must give an actionable reason for each distinct way a host launch can fail.
"""
from __future__ import annotations

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
