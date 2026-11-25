"""Isolation for the trust-fixes acceptance tests (they do not sit under tests/, so tests/conftest.py
does not apply): a throwaway CODEINTEL_HOME, a fresh MCP gateway per test, and no background
reindex unless a test asks for it."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _isolate_codeintel_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CODEINTEL_HOME", str(tmp_path_factory.mktemp("codeintel-home")))


@pytest.fixture(autouse=True)
def _fresh_gateway():
    from codeintel.transports import mcp as server
    server._reset_gateway()
    yield
    server._reset_gateway()


@pytest.fixture
def background_reindex():
    return True


@pytest.fixture(autouse=True)
def _no_background_reindex(request, monkeypatch):
    if "background_reindex" in request.fixturenames:
        return
    from codeintel.gateway.reindexer import Reindexer

    monkeypatch.setattr(Reindexer, "maybe_reindex", lambda self, project_root: False)


@pytest.fixture(autouse=True)
def _no_kept_open_graph_backend(monkeypatch):
    """No kept-open graph backend session outlives the test that opened it.

    A kept-open backend (0.31.0) starts the backend's account-wide daemon for the test's HOME. The
    next test, with a different HOME, then had its own `index_repository` refused ("the active
    account daemon uses a different cache directory"), so these tests failed in any order where a
    live-backend test followed another."""
    from codeintel.engines.graph import locate, session

    # As tests/conftest.py does: a developer venv with the backend installed beside the
    # interpreter would otherwise defeat every test that hides the backend by stubbing PATH.
    monkeypatch.setattr(locate, "own_bin_dirs", list)
    yield
    session.close_all()
