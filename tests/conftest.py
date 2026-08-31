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
def _isolate_codeintel_home(tmp_path_factory, monkeypatch):
    """Point ``~/.codeintel`` at a throwaway directory for EVERY test.

    Two problems, one fix.

    *Pollution.* Tests that build a repo and query it write into the real per-machine semantic
    cache unless they remember to redirect it. Most do; the ones that don't left a row per run
    behind forever — a working machine had ~90 orphaned ``pytest-of-<user>`` project roots in
    ``~/.codeintel/semantic.db``. Rows are partitioned by ``project_root`` so results stayed
    correct, but ``doctor`` reports each dead tmp directory as a healthy indexed project. This is
    the same class the ``_reap_leaked_backend_projects`` fixture below handles for the graph
    backend; opting in per test is what failed, so this is not opt-in.

    *Contamination in the other direction.* ``load_config`` merges a machine-wide
    ``config.toml`` under the project's. A developer with one on disk was silently running the
    suite against different defaults than CI — a green local run proving nothing about the
    shipped values. An isolated home means no test can read it.

    Env var rather than patching ``semantic_db._base_dir``: it also covers config and auth, and it
    survives into the subprocesses ``test_cold_process`` launches. Tests that set the variable
    themselves still win — ``monkeypatch`` applies theirs after this one.
    """
    monkeypatch.setenv("CODEINTEL_HOME", str(tmp_path_factory.mktemp("codeintel-home")))


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


@pytest.fixture(scope="session", autouse=True)
def _release_embedding_runtimes():
    """Destroy every fastembed/onnxruntime session while the interpreter is still healthy.

    `Searcher` and `Indexer` each cache a `TextEmbedding` on the instance, lazily, and this suite
    builds them from ~100 construction sites. Each one owns an onnxruntime `InferenceSession` with
    native threads. Nothing released them, so they were all finalised together during interpreter
    shutdown — and on one CI runner (3.12, ubuntu) that ended in
    ``terminate called without an active exception`` and a core dump AFTER the last test had already
    passed. The suite was green; the process exit code was 134, which fails the job just the same
    and points at nothing.

    Freeing them here makes the teardown deterministic and moves it inside Python's lifetime, where
    a failure would be a normal exception rather than a C++ abort in a finalising interpreter. It is
    a mitigation of a native-teardown race, not a proof that no such race exists: it removes the
    pile-up that made it likely, and it cannot make things worse, because dropping a reference at
    session end is exactly what the interpreter was about to do less carefully.
    """
    yield
    try:
        import gc

        from fastembed import TextEmbedding
    except Exception:
        return                          # fastembed absent — nothing to release
    try:
        gc.collect()
        # Drop the provider-side caches first, so the only remaining references are the ones the
        # collector below is about to clear.
        for obj in list(gc.get_objects()):
            try:
                if isinstance(obj, TextEmbedding):
                    for attr in ("model", "_model"):
                        if hasattr(obj, attr):
                            try:
                                setattr(obj, attr, None)
                            except Exception:
                                pass
            except ReferenceError:
                continue
        gc.collect()
    except Exception:
        pass                            # best-effort: never turn a green run red


@pytest.fixture(scope="session", autouse=True)
def _reap_leaked_backend_projects():
    """Delete the backend project registrations that live tests leave behind.

    Several live tests (RBAC scoping, the MCP handshake, `code.query` over a real index, the
    reindexer) build a repo under pytest's ``tmp_path`` and index it into the real codebase-memory-mcp
    backend — and nothing ever deleted it. One dev DB had accumulated **572** dead
    ``pytest-of-<user>`` registrations before this existed, one per indexed tmp repo across every run,
    which is noise in `list_projects` and a standing source of stale-index confusion the graph engine
    already fights.

    A pytest tmp dir never outlives its session, so any backend project whose ``root_path`` sits under
    a ``/pytest-of-`` path is junk by construction — including the backlog from earlier runs, which
    this also clears. The match is deliberately narrow: it can only ever name an ephemeral pytest
    directory, never a real project (codeintel, the corpus, or a user's own repo), so it cannot delete
    anything that matters. Runs once at session end (deletes measured at ~0ms each), best-effort, and
    a no-op where the backend is not installed (CI)."""
    yield
    try:
        from codeintel.providers.graph import GraphProvider

        p = GraphProvider()
        if not p.available:
            return
        raw = p._run("list_projects", {}, 30000)
        entries = (raw or {}).get("projects", []) if isinstance(raw, dict) else (raw or [])
        from tests._backend_reaper import leaked_project_names

        for name in leaked_project_names(entries):
            p._run("delete_project", {"project": name}, 30000)
    except Exception:
        pass  # cleanup is best-effort — it must never turn a green run red


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
