"""A readiness claim must be one a query can honor.

The gateway is a process-lifetime singleton, but `code.status`/`code.doctor` probe a FRESH
provider for any engine the gateway lacks. So an engine installed mid-session was reported
`installed/runnable/ok` while `code.query` still routed around it — for the life of the MCP host
process. Doctor's own remediation ("install codebase-memory-mcp, then re-check") could never
converge: the re-check passed and the queries stayed degraded.

These pin the fix: an engine that a probe proves present is ADOPTED onto the live gateway, and a
warmed provider is never replaced in the process.
"""
from __future__ import annotations

import codeintel.server as srv
from codeintel.gateway import Gateway


class _FakeProvider:
    """Stands in for a backend that just appeared on PATH."""

    def __init__(self, available: bool = True, marker: str = "graph-answer") -> None:
        self.available = available
        self._marker = marker
        self.build_calls = 0

    def probe(self, *a, **k) -> dict:
        return {"installed": self.available, "runnable": self.available,
                "repo_indexed": True, "detail": "", "remediation": None}

    def build_result(self, op, target, *a, **k) -> dict:
        self.build_calls += 1
        return {"ok": True, "op": op, "target": target, "result": self._marker,
                "engine": "graph", "cached": False}


# --------------------------------------------------------------------------- #
# Gateway.adopt_provider
# --------------------------------------------------------------------------- #

def test_adopt_fills_an_empty_slot_and_queries_then_route_to_it():
    gw = Gateway(graph=None, lsp=None, semantic=None)

    before = gw.query(op="callers", target="foo", engine="graph")
    assert before["result"] is None
    assert before["reason"] == "engine-unavailable"

    provider = _FakeProvider()
    assert gw.adopt_provider("graph", provider) is True

    after = gw.query(op="callers", target="foo", engine="graph")
    assert after["result"] == "graph-answer"
    assert provider.build_calls == 1


def test_adopt_never_replaces_a_live_provider():
    """The singleton exists to preserve the warmed serena session and graph project cache —
    adoption must only ever fill holes, never swap a provider that already holds that state."""
    warmed = _FakeProvider(marker="warmed")
    gw = Gateway(graph=warmed, lsp=None, semantic=None)

    assert gw.adopt_provider("graph", _FakeProvider(marker="fresh")) is False
    assert gw.graph is warmed
    assert gw.query(op="callers", target="foo", engine="graph")["result"] == "warmed"


def test_adopt_rejects_unavailable_providers_unknown_engines_and_none():
    gw = Gateway(graph=None, lsp=None, semantic=None)
    assert gw.adopt_provider("graph", _FakeProvider(available=False)) is False
    assert gw.adopt_provider("graph", None) is False
    assert gw.adopt_provider("nonsense", _FakeProvider()) is False
    assert gw.adopt_provider("graph", object()) is False   # no `available` attribute
    assert gw.graph is None


def test_adopt_never_raises_on_a_hostile_provider():
    class _Hostile:
        @property
        def available(self):
            raise RuntimeError("boom")

    gw = Gateway(graph=None, lsp=None, semantic=None)
    assert gw.adopt_provider("graph", _Hostile()) is False


def test_adopt_invalidates_answers_cached_while_the_engine_was_missing():
    """`overview` auto-falls back to lsp when graph is absent and caches that under the GRAPH key.
    Neither the content hash nor the freshness token can see graph arriving, so adoption must
    drop the cache or the fallback answer outlives the engine's absence."""
    lsp = _FakeProvider(marker="lsp-overview")
    gw = Gateway(graph=None, lsp=lsp, semantic=None)

    first = gw.query(op="overview", target="pkg", project_root="/repo")
    assert first["result"] == "lsp-overview"          # fell back to lsp, cached under "graph"
    assert gw.query(op="overview", target="pkg", project_root="/repo")["cached"] is True

    gw.adopt_provider("graph", _FakeProvider(marker="graph-overview"))

    after = gw.query(op="overview", target="pkg", project_root="/repo")
    assert after["result"] == "graph-overview"        # not the stale fallback


# --------------------------------------------------------------------------- #
# The reported bug, end to end through the handlers
# --------------------------------------------------------------------------- #

def test_status_reporting_an_engine_runnable_implies_query_can_reach_it(monkeypatch):
    """The regression: install a backend mid-session, call code.status, and the readiness it
    reports must be backed by a gateway that can actually serve the query."""
    gw = Gateway(graph=None, lsp=None, semantic=None)   # booted before the backend existed
    monkeypatch.setattr(srv, "_get_gateway", lambda: gw)

    appeared = _FakeProvider()
    import codeintel.doctor as _doctor
    monkeypatch.setattr(_doctor, "_probe_engine", _probe_stub(appeared), raising=True)

    status = srv.code_status_handler({"project_root": "/repo"})
    assert status["readiness"]["graph"]["runnable"] is True
    assert status["graph"] is True

    result = srv.code_query_handler({"op": "callers", "target": "foo", "engine": "graph"})
    assert result["result"] == "graph-answer", (
        "code.status reported graph runnable but code.query could not route to it"
    )


def test_doctor_adopts_an_engine_so_its_own_remediation_converges(monkeypatch):
    gw = Gateway(graph=None, lsp=None, semantic=None)
    monkeypatch.setattr(srv, "_get_gateway", lambda: gw)

    appeared = _FakeProvider()
    import codeintel.doctor as _doctor
    monkeypatch.setattr(_doctor, "_probe_engine", _probe_stub(appeared), raising=True)

    srv.code_doctor_handler({"project_root": "/repo"})
    assert gw.graph is appeared


def _probe_stub(graph_provider):
    """Replace doctor's per-engine probe: graph resolves to `graph_provider` (a backend that just
    appeared), the other two stay absent. Honors the real `on_provider` contract."""
    def _probe(engine, provider, build, call, on_provider=None):
        p = provider if provider is not None else (graph_provider if engine == "graph" else None)
        installed = p is not None and getattr(p, "available", False) is True
        if on_provider is not None and installed:
            on_provider(engine, p)
        return {"engine": engine, "status": "ok" if installed else "fail",
                "installed": installed, "runnable": installed,
                "repo_indexed": True if installed else None,
                "detail": "", "remediation": None}
    return _probe


# --------------------------------------------------------------------------- #
# query-path self-heal (no code.status call required)
# --------------------------------------------------------------------------- #

def test_query_path_picks_up_a_backend_installed_after_boot(monkeypatch):
    gw = Gateway(graph=None, lsp=None, semantic=None)
    monkeypatch.setattr(srv, "_get_gateway", lambda: gw)

    appeared = _FakeProvider()
    monkeypatch.setattr(srv, "GraphProvider", lambda: appeared)
    monkeypatch.setattr(srv, "LspProvider", lambda: _FakeProvider(available=False))
    monkeypatch.setattr(srv, "SemanticProvider", lambda: _FakeProvider(available=False))

    result = srv.code_query_handler({"op": "callers", "target": "foo", "engine": "graph"})
    assert result["result"] == "graph-answer"
    assert gw.graph is appeared


def test_engine_refresh_is_throttled_and_skipped_once_all_engines_are_present(monkeypatch):
    gw = Gateway(graph=None, lsp=None, semantic=None)
    builds = {"n": 0}

    def _count():
        builds["n"] += 1
        return _FakeProvider(available=False)   # never adopted, so the slot stays empty

    monkeypatch.setattr(srv, "GraphProvider", _count)
    monkeypatch.setattr(srv, "LspProvider", _count)
    monkeypatch.setattr(srv, "SemanticProvider", _count)

    srv._refresh_missing_engines(gw)
    assert builds["n"] == 3          # one construction per missing engine
    srv._refresh_missing_engines(gw)
    assert builds["n"] == 3          # throttled — no re-probe within the interval

    # all slots filled ⇒ the refresh short-circuits before the throttle even matters
    gw.graph = gw.lsp = gw.semantic = _FakeProvider()
    srv._LAST_ENGINE_REFRESH = 0.0
    srv._refresh_missing_engines(gw)
    assert builds["n"] == 3


def test_engine_refresh_never_raises_when_a_provider_constructor_explodes(monkeypatch):
    gw = Gateway(graph=None, lsp=None, semantic=None)

    def _boom():
        raise RuntimeError("backend blew up during construction")

    monkeypatch.setattr(srv, "GraphProvider", _boom)
    monkeypatch.setattr(srv, "LspProvider", _boom)
    monkeypatch.setattr(srv, "SemanticProvider", _boom)

    srv._refresh_missing_engines(gw)   # must not propagate
    assert gw.graph is None
