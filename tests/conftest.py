"""Shared test fixtures.

The MCP server caches ONE gateway for the process (so the content-hash cache and the warmed
serena session survive across an agent's calls). In tests that singleton is cross-test state:
a provider built under one test's monkeypatched PATH would still be answering `code.status` in
the next test. Reset it around every test so each one sees providers built under its own
environment.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_gateway():
    from codeintel import server
    server._reset_gateway()
    yield
    server._reset_gateway()
