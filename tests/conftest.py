"""Shared test fixtures.

A standing hazard worth knowing before you trust a green local run: **this suite behaves
differently depending on which backends are installed.** A dev machine usually has
codebase-memory-mcp and uvx/serena; CI has neither, so the live graph/LSP tests skip there AND
the never-raise envelopes take different `reason`/`hint` paths. A bug reachable only on the
no-backend path passes locally and fails in CI — that is not a flaky runner, it is real coverage
you do not have at your desk. To reproduce CI's shape before pushing::

    env PATH="$(dirname "$(which python)"):/usr/bin:/bin" pytest -q

Prefer tests that pin a contract independently of the environment (see
test_mcp_server.py::test_no_tool_advertises_the_optional_envelope_fields_as_required, which
inspects the derived schema rather than whichever envelope this machine happens to produce).

The MCP server caches ONE gateway for the process (so the content-hash cache and the warmed
serena session survive across an agent's calls). In tests that singleton is cross-test state:
a provider built under one test's monkeypatched PATH would still be answering `code.status` in
the next test. Reset it around every test so each one sees providers built under its own
environment.

Both autouse resets below are IN-PROCESS: they clear state between tests running inside one
interpreter that has already imported every provider module, already warmed a serena session,
already cached a graph wire-format verdict — from some EARLIER test. They cannot express, and are
blind to, a defect that only exists on the very first call of a brand-new process (no prior
import, no prior warm session, no prior cache entry to reset). That class of defect — B1
(docs/eval-2026-08-17.md:65-103): a cold LSP timeout rendered as a confident "(none)" — has its
own tier in tests/test_cold_process.py, which drives `sys.executable -m codeintel` as a real
subprocess instead of relying on these fixtures.
"""
from __future__ import annotations

import os
import shutil
import sys
import sysconfig

import pytest


@pytest.fixture(autouse=True)
def _fresh_gateway():
    from codeintel import server
    server._reset_gateway()
    yield
    server._reset_gateway()


def _interpreter_scripts_dir() -> str:
    """Where console scripts for the interpreter running these tests are installed."""
    return sysconfig.get_path("scripts") or os.path.dirname(sys.executable)


@pytest.fixture
def console_script(monkeypatch) -> str:
    """The absolute path of the `codeintel` console script **belonging to this checkout**.

    Any test that launches the installed command is only meaningful against the code under test,
    but the production `resolve_command()` deliberately uses `shutil.which` — it has to record
    what the agent host will actually launch. On a developer machine that search routinely finds a
    previously-installed global `codeintel` ahead of the editable one, so the test registers,
    launches, and handshakes with a DIFFERENT build, and still passes. The host-config tests in
    test_mcp_handshake.py are the ones exposed to this; they are the only tests that launch a
    command read back out of a config file rather than `sys.executable -m codeintel`.

    So: put this interpreter's script directory at the front of PATH for the duration of the test,
    and skip rather than pass if the console script is not installed for it at all (a source
    checkout that has never been `pip install`ed — there is nothing to launch).
    """
    scripts = _interpreter_scripts_dir()
    monkeypatch.setenv("PATH", scripts + os.pathsep + os.environ.get("PATH", ""))

    resolved = shutil.which("codeintel")
    if resolved is None or os.path.realpath(os.path.dirname(resolved)) != os.path.realpath(scripts):
        pytest.skip(f"no `codeintel` console script installed for {sys.executable} "
                    f"(looked in {scripts}); `pip install -e .` to run these")
    return resolved


@pytest.fixture(autouse=True)
def _reset_graph_wire_format_cache():
    """Clear GraphProvider's process-wide backend-compatibility verdict between tests.

    The verdict is cached at class level because it is a property of the INSTALLED backend rather
    than of any provider instance, and re-probing it costs a subprocess round trip on every
    `doctor`. That makes it global mutable state: on a machine where the real backend IS installed,
    one live call fixes the verdict for the rest of the session, and the unit tests that stub the
    subprocess seam then inherit a verdict that has nothing to do with their stub.
    """
    from codeintel.providers.graph import GraphProvider

    GraphProvider._reset_wire_format_cache()
    yield
    GraphProvider._reset_wire_format_cache()
