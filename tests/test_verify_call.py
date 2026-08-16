"""Unit tests for `verify_stdio_call` — the handshake that goes on to invoke a tool.

`verify_stdio_server` stops at `tools/list`, which proves a host can START the server but not that
a query answers. Since every codeintel result is a safe envelope with `ok: True`, a release check
that stops there passes against a build that boots cleanly and returns nothing. This is the layer
that reads the body, so its failure modes have to be as sharp as the handshake's: never raise,
never hang, never leak a subprocess, and distinguish "the call failed" from "the call returned
nothing".
"""
from __future__ import annotations

import sys
import textwrap

from codeintel.verify import _extract_text, verify_stdio_call


def _script(tmp_path, body: str) -> list[str]:
    path = tmp_path / "fake_call_server.py"
    path.write_text(textwrap.dedent(body))
    return [sys.executable, str(path)]


def _server(call_response: str) -> str:
    """A well-formed stdio MCP server whose tools/call branch is `call_response`."""
    return """
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "9.9"}}}), flush=True)
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": "code.query"}]}}), flush=True)
        elif method == "tools/call":
            %s
    """ % call_response


_ECHOES_ARGS = _server(
    'print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": ['
    '{"type": "text", "text": "found " + msg["params"]["arguments"]["target"]}]}}), flush=True)'
)


def test_returns_the_tool_response_body(tmp_path):
    argv = _script(tmp_path, _ECHOES_ARGS)
    res = verify_stdio_call(argv[0], argv[1:], tool="code.query",
                            arguments={"target": "reticulate_splines"}, timeout_s=30)
    assert res["ok"] is True
    assert "found reticulate_splines" in res["content"]
    assert res["tools"] == ["code.query"]


def test_arguments_actually_reach_the_tool(tmp_path):
    """Guards the canary's core assertion: the content must be a function of what was asked."""
    argv = _script(tmp_path, _ECHOES_ARGS)
    a = verify_stdio_call(argv[0], argv[1:], tool="code.query",
                          arguments={"target": "alpha"}, timeout_s=30)
    b = verify_stdio_call(argv[0], argv[1:], tool="code.query",
                          arguments={"target": "beta"}, timeout_s=30)
    assert "alpha" in a["content"] and "alpha" not in b["content"]


def test_empty_body_is_not_ok(tmp_path):
    argv = _script(tmp_path, _server(
        'print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], '
        '"result": {"content": []}}), flush=True)'
    ))
    res = verify_stdio_call(argv[0], argv[1:], tool="code.query", timeout_s=30)
    assert res["ok"] is False
    assert "empty body" in res["detail"]


def test_tool_error_is_surfaced(tmp_path):
    argv = _script(tmp_path, _server(
        'print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], '
        '"error": {"code": -32602, "message": "bad args"}}), flush=True)'
    ))
    res = verify_stdio_call(argv[0], argv[1:], tool="code.query", timeout_s=30)
    assert res["ok"] is False
    assert "bad args" in res["detail"]


def test_missing_tool_is_reported_without_calling_it(tmp_path):
    argv = _script(tmp_path, _ECHOES_ARGS)
    res = verify_stdio_call(argv[0], argv[1:], tool="code.nonexistent", timeout_s=30)
    assert res["ok"] is False
    assert "does not expose" in res["detail"] and "code.query" in res["detail"]


def test_a_call_that_never_answers_times_out_without_hanging(tmp_path):
    argv = _script(tmp_path, _server("pass"))  # accepts tools/call, replies never
    start = __import__("time").monotonic()
    res = verify_stdio_call(argv[0], argv[1:], tool="code.query", timeout_s=3)
    assert res["ok"] is False
    assert __import__("time").monotonic() - start < 20


def test_missing_command_is_actionable():
    res = verify_stdio_call("codeintel-does-not-exist-xyz", ["serve"],
                            tool="code.query", timeout_s=5)
    assert res["ok"] is False
    assert "not on PATH" in res["detail"]
    assert res["content"] == "" and res["payload"] is None


def test_result_shape_is_stable_across_outcomes(tmp_path):
    argv = _script(tmp_path, _ECHOES_ARGS)
    for res in (verify_stdio_call(argv[0], argv[1:], tool="code.query",
                                  arguments={"target": "x"}, timeout_s=30),
                verify_stdio_call("nope-xyz", [], tool="code.query", timeout_s=5)):
        assert set(res) >= {"ok", "tools", "server", "detail", "content", "payload"}
        assert isinstance(res["content"], str)


# --------------------------------------------------------------------------- #
# body flattening
# --------------------------------------------------------------------------- #

def test_extract_text_handles_every_body_shape():
    assert "hi" in _extract_text({"content": [{"type": "text", "text": "hi"}]})
    assert "42" in _extract_text({"structuredContent": {"n": 42}})
    # codeintel's handlers return a plain dict; it must still be readable
    assert "reason" in _extract_text({"ok": True, "result": None, "reason": "no-result"})
    assert _extract_text(None) == ""
    assert _extract_text({"content": [{"type": "image"}]}) != ""  # falls back to the raw object


def test_extract_text_never_raises_on_unserializable_input():
    class _Hostile:
        def __repr__(self):
            raise RuntimeError("boom")

    assert _extract_text({"structuredContent": _Hostile()}) == ""
