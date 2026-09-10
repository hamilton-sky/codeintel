"""A background index pass that failed must not keep reporting itself as in progress.

`_start_background_index` runs a cold pass on a daemon thread and the request that started it
returns `indexing-in-progress` immediately. The thread's `finally` then clears
`_BG_INDEX_STARTED` — so once the pass dies, the next request finds no index and no job in
flight, starts another doomed pass, and answers `indexing-in-progress` again. Forever.

That is the worst shape this codebase can produce: a confident claim (work is underway) about
something that is not true (the work died), served to an agent whose only channel is the envelope,
while the one actionable sentence goes to a server log it cannot read. An agent told "indexing is
in progress" retries. It does not go and fix its proxy.

The fix mirrors `providers/lsp.py`'s failed-boot cooldown rather than inventing a second policy:
remember the cause, report it, retry exactly once per window.
"""
from __future__ import annotations

import time

import pytest

import codeintel.providers.semantic as sem
from codeintel.providers.semantic import (
    _BG_INDEX_COOLDOWN_S,
    SemanticProvider,
    _background_index_failure,
    _index_key,
    _record_background_failure,
)


@pytest.fixture(autouse=True)
def _clean_module_state():
    """This state is module-level on purpose (see its comment) — so tests must not leak into it."""
    sem._BG_INDEX_STARTED.clear()
    sem._BG_INDEX_FAILED.clear()
    yield
    sem._BG_INDEX_STARTED.clear()
    sem._BG_INDEX_FAILED.clear()


# --------------------------------------------------------------------------------------------- #
# The memory itself
# --------------------------------------------------------------------------------------------- #

def test_a_recorded_failure_is_returned_during_the_cooldown(tmp_path):
    _record_background_failure(_index_key(str(tmp_path)), "ProxyError: 403 Forbidden")
    assert _background_index_failure(str(tmp_path)) == "ProxyError: 403 Forbidden"


def test_the_failure_expires_so_a_transient_one_clears_itself(tmp_path):
    """The window is what licenses exactly one retry. Without expiry a proxy that came back, or a
    disk that was freed, would need a process restart to be noticed."""
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_FAILED[key] = (time.monotonic() - _BG_INDEX_COOLDOWN_S - 1, "ProxyError: 403")

    assert _background_index_failure(str(tmp_path)) is None


def test_an_expired_entry_is_dropped_rather_than_left_to_accumulate(tmp_path):
    """A long-lived MCP server sees many roots; a dict that only grows is a slow leak."""
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_FAILED[key] = (time.monotonic() - _BG_INDEX_COOLDOWN_S - 1, "ProxyError: 403")

    _background_index_failure(str(tmp_path))
    assert key not in sem._BG_INDEX_FAILED


def test_the_key_is_the_realpath_like_every_other_entry(tmp_path):
    """A trailing slash must not be remembered as a second, independent repo."""
    _record_background_failure(_index_key(str(tmp_path)), "boom")
    assert _background_index_failure(str(tmp_path) + "/") == "boom"


def _no_real_thread(monkeypatch, started: list):
    monkeypatch.setattr(
        sem.threading, "Thread",
        lambda **kw: type("_T", (), {"start": lambda self: started.append("go")})(),
    )


def test_the_start_helper_refuses_while_a_failure_stands(tmp_path, monkeypatch):
    """The cooldown is enforced under the same lock that marks the start, not by the caller.

    A check in the caller is a check-then-act race with a real losing interleaving: a request
    looks up the failure and sees none because the thread has not recorded yet; the thread then
    records and clears `_BG_INDEX_STARTED`; the request, finding no job in flight, starts another
    pass. An earlier version also destroyed the fresh cause on the way past, so concurrent polling
    — exactly what "retry shortly" tells an agent to do — could bypass the cooldown indefinitely.
    """
    started: list[str] = []
    _no_real_thread(monkeypatch, started)
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_FAILED[key] = (time.monotonic(), "ProxyError: 403")

    assert sem._start_background_index(str(tmp_path), ":memory:", {}) is False
    assert started == []                          # no pass launched
    assert key in sem._BG_INDEX_FAILED            # and the cause survives the attempt


def test_the_start_helper_allows_the_one_retry_once_expired(tmp_path, monkeypatch):
    started: list[str] = []
    _no_real_thread(monkeypatch, started)
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_FAILED[key] = (time.monotonic() - _BG_INDEX_COOLDOWN_S - 1, "ProxyError: 403")

    assert sem._start_background_index(str(tmp_path), ":memory:", {}) is True
    assert started == ["go"]
    assert key not in sem._BG_INDEX_FAILED


def test_a_failure_landing_mid_request_is_not_reported_as_progress(monkeypatch, tmp_path):
    """The losing interleaving, driven deterministically.

    The caller's first lookup sees nothing; the failure lands before it starts a pass. The helper
    refuses, and the caller re-reads rather than reporting the refusal as `indexing-in-progress`.
    """
    p = _cold_repo_provider(monkeypatch, tmp_path)
    key = _index_key(str(tmp_path))
    calls: list[int] = []

    real_lookup = sem._background_index_failure

    def _racy(root):
        calls.append(1)
        if len(calls) == 1:
            return None                                    # nothing recorded yet
        return real_lookup(root)

    monkeypatch.setattr(sem, "_background_index_failure", _racy)
    # The thread records its failure in the window between the caller's check and its start.
    monkeypatch.setattr(sem, "_start_background_index",
                        lambda *a, **k: (_record_background_failure(key, "ProxyError: 403"), False)[1])

    r = p.build_result("search", "x", [], 0, str(tmp_path))

    assert r["reason"] == "index-failed"
    assert "403" in r["hint"]


def test_expired_entries_are_pruned_globally_not_only_on_their_own_lookup(tmp_path):
    """A long-lived server sees many one-off roots. Pruning only the key being looked up left
    every never-revisited root in the dict forever — a cleanup claim the code did not keep."""
    old_at = time.monotonic() - _BG_INDEX_COOLDOWN_S - 1
    for i in range(5):
        sem._BG_INDEX_FAILED[f"/never/queried/again/{i}"] = (old_at, "ProxyError: 403")

    _record_background_failure(_index_key(str(tmp_path)), "a new failure elsewhere")

    assert [k for k in sem._BG_INDEX_FAILED if k.startswith("/never/")] == []
    assert _background_index_failure(str(tmp_path)) == "a new failure elsewhere"


def test_pruning_does_not_evict_entries_that_are_still_live(tmp_path):
    """The sweep must not become its own cooldown bypass."""
    sem._BG_INDEX_FAILED["/still/cooling"] = (time.monotonic(), "ProxyError: 403")

    _record_background_failure(_index_key(str(tmp_path)), "another failure")

    assert "/still/cooling" in sem._BG_INDEX_FAILED


# --------------------------------------------------------------------------------------------- #
# What the caller actually receives
# --------------------------------------------------------------------------------------------- #

def _cold_repo_provider(monkeypatch, tmp_path):
    """A non-blocking provider (the MCP/HTTP shape) pointed at a repo with no index."""
    class FakeSearcher:
        def __init__(self, db, model_name="m"):
            self.last_stale = self.last_unverifiable = 0
            self.last_query_error = None

        def has_index(self, project_root):
            return False

    class FakeDb:
        def __init__(self, *a, **k):
            pass

        def init(self):
            pass

    monkeypatch.setattr(sem, "_DEPS_OK", True, raising=False)
    monkeypatch.setattr("codeintel.searcher.Searcher", FakeSearcher)
    monkeypatch.setattr("codeintel.semantic_db.SemanticDb", FakeDb)
    monkeypatch.delenv("CODEINTEL_REINDEX", raising=False)
    return SemanticProvider(blocking_index=False)


def test_a_failed_pass_is_reported_as_index_failed_not_as_progress(monkeypatch, tmp_path):
    """The whole point. `indexing-in-progress` about a dead pass is a false statement, and the one
    an agent is least equipped to see through."""
    p = _cold_repo_provider(monkeypatch, tmp_path)
    _record_background_failure(_index_key(str(tmp_path)), "ProxyError: 403 Forbidden")

    r = p.build_result("search", "parse_config", [], 0, str(tmp_path))

    assert r["ok"] is True
    assert r["result"] is None
    assert r["reason"] == "index-failed"
    assert "403 Forbidden" in r["hint"]
    assert "NOT 'still indexing'" in r["hint"]


def test_no_second_pass_is_started_during_the_cooldown(monkeypatch, tmp_path):
    """Retrying the same doomed work on every request is the other half of the defect: it is not
    just a wrong answer, it is a wrong answer that costs a thread and a model download attempt."""
    p = _cold_repo_provider(monkeypatch, tmp_path)
    started: list[str] = []
    monkeypatch.setattr(sem, "_start_background_index",
                        lambda *a, **k: started.append("started") or True)
    _record_background_failure(_index_key(str(tmp_path)), "ProxyError: 403 Forbidden")

    p.build_result("search", "x", [], 0, str(tmp_path))
    assert started == []


def test_exactly_one_retry_once_the_window_elapses(monkeypatch, tmp_path):
    """A permanent failure stays legible; a transient one recovers without intervention."""
    p = _cold_repo_provider(monkeypatch, tmp_path)
    started: list[str] = []
    monkeypatch.setattr(sem, "_start_background_index",
                        lambda *a, **k: started.append("started") or True)

    key = _index_key(str(tmp_path))
    sem._BG_INDEX_FAILED[key] = (time.monotonic() - _BG_INDEX_COOLDOWN_S - 1, "ProxyError: 403")

    r = p.build_result("search", "x", [], 0, str(tmp_path))
    assert started == ["started"]
    assert r["reason"] == "indexing-in-progress"


def test_a_fresh_repo_still_reports_progress(monkeypatch, tmp_path):
    """The new branch must not swallow the case it was carved out of."""
    p = _cold_repo_provider(monkeypatch, tmp_path)
    monkeypatch.setattr(sem, "_start_background_index", lambda *a, **k: True)

    r = p.build_result("search", "x", [], 0, str(tmp_path))
    assert r["reason"] == "indexing-in-progress"


def test_index_failed_is_a_could_not_ask_in_the_fanout(monkeypatch, tmp_path):
    """`index-failed` is already in the gateway's `unreachable` set, so a `context` fan-out where
    it is the only outcome says so rather than "that symbol does not exist"."""
    from codeintel.gateway import Gateway

    class _Null:
        available = True

        def __init__(self, engine, reason):
            self.engine, self.reason = engine, reason

        def build_result(self, op, target, *_a, **_k):
            return {"ok": True, "op": op, "target": target, "result": None,
                    "engine": self.engine, "cached": False, "reason": self.reason}

    gw = Gateway(graph=_Null("graph", "engine-unavailable"),
                 lsp=_Null("lsp", "boot-failed"),
                 semantic=_Null("semantic", "index-failed"))
    r = gw.query(op="context", target="x", engine="all", project_root=str(tmp_path))

    assert r["reason"] == "engines-unavailable"
    assert "NOT evidence the target does not exist" in r["hint"]


# --------------------------------------------------------------------------------------------- #
# doctor / code.status must tell the same story
# --------------------------------------------------------------------------------------------- #

def test_probe_reports_the_failure_rather_than_progress(monkeypatch, tmp_path):
    """`doctor` repeating "indexing in progress" about a dead pass is the diagnostic command
    repeating the misdiagnosis — the one place that must not."""
    _record_background_failure(_index_key(str(tmp_path)), "ProxyError: 403 Forbidden")

    probe = sem._not_indexed_probe(str(tmp_path), "no semantic index database yet")

    assert "background index pass failed" in probe["detail"]
    assert "403 Forbidden" in probe["detail"]
    assert "indexing in progress" not in probe["detail"]
    assert "codeintel index" in probe["remediation"]


def test_a_failed_pass_is_reported_as_not_runnable(tmp_path):
    """`runnable: true` beside a known execution failure is a contradiction in one payload.

    `code.status` hands these raw fields to an agent that reads them rather than the prose, and
    this probe already reports `runnable: False` for the sibling case ("semantic cache present but
    unreadable"). The rolled-up status was already `fail` via `repo_indexed`, so this is about the
    field a consumer reads directly.
    """
    from codeintel.doctor import _status_for

    _record_background_failure(_index_key(str(tmp_path)), "ProxyError: 403 Forbidden")
    probe = sem._not_indexed_probe(str(tmp_path), "no semantic index database yet")

    assert probe["installed"] is True          # the deps are importable; that part is true
    assert probe["runnable"] is False
    assert _status_for(probe) == "fail"


def test_runnable_returns_once_the_cooldown_expires(tmp_path):
    """Scoped to the window, like the retry policy — a permanent red row would outlive its cause."""
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_FAILED[key] = (time.monotonic() - _BG_INDEX_COOLDOWN_S - 1, "ProxyError: 403")

    probe = sem._not_indexed_probe(str(tmp_path), "no semantic index database yet")
    assert probe["runnable"] is True
    assert "background index pass failed" not in probe["detail"]


def test_probe_still_reports_a_genuinely_running_pass(monkeypatch, tmp_path):
    sem._BG_INDEX_STARTED[_index_key(str(tmp_path))] = time.monotonic()

    probe = sem._not_indexed_probe(str(tmp_path), "no semantic index database yet")
    assert "indexing in progress" in probe["detail"]


def test_a_failure_outranks_a_concurrently_recorded_start(tmp_path):
    """Both can be set — the thread records its failure before the `finally` clears the start.
    The failure is the newer, truer fact, so it wins."""
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_STARTED[key] = time.monotonic()
    _record_background_failure(key, "ProxyError: 403 Forbidden")

    probe = sem._not_indexed_probe(str(tmp_path), "no semantic index database yet")
    assert "background index pass failed" in probe["detail"]


def test_a_crash_on_the_thread_is_recorded_too(monkeypatch, tmp_path):
    """`index()` never raises, but `db.init()` and the imports around it can.

    A caller that gets `indexing-in-progress` forever cannot tell a crashed pass from a failed
    one, so both have to be remembered.
    """
    import codeintel.semantic_db as sdb

    monkeypatch.setattr(sdb.SemanticDb, "init",
                        lambda self: (_ for _ in ()).throw(RuntimeError("disk gone")))
    captured: list = []
    monkeypatch.setattr(sem.threading, "Thread",
                        lambda target, **kw: type("_T", (), {"start": lambda self: captured.append(target)})())

    sem._start_background_index(str(tmp_path), ":memory:", {})
    captured[0]()                                    # run the thread body inline

    assert "RuntimeError" in (_background_index_failure(str(tmp_path)) or "")


def test_the_lookup_never_raises_into_the_caller(monkeypatch, tmp_path):
    """New code on the never-raise path: a fault here must cost the answer, not the envelope."""
    p = _cold_repo_provider(monkeypatch, tmp_path)
    monkeypatch.setattr(sem, "_background_index_failure",
                        lambda root: (_ for _ in ()).throw(RuntimeError("boom")))

    r = p.build_result("search", "x", [], 0, str(tmp_path))
    assert r["ok"] is True
    assert r["result"] is None


def test_a_close_fault_does_not_overwrite_the_real_cause(monkeypatch, tmp_path):
    """`db.close()` runs after the cause is recorded and can itself raise.

    Landing in the outer handler then would replace "could not load embedding model … check
    network/proxy access" with a database-close error — swapping the actionable cause for a
    downstream symptom of it.
    """
    import codeintel.semantic_db as sdb
    from codeintel.indexer import Indexer

    monkeypatch.setattr(sdb.SemanticDb, "init", lambda self: None)
    monkeypatch.setattr(sdb.SemanticDb, "close",
                        lambda self: (_ for _ in ()).throw(RuntimeError("close blew up")))
    monkeypatch.setattr(Indexer, "index", lambda self, root: -1)
    monkeypatch.setattr(Indexer, "__init__", lambda self, db, **kw: setattr(
        self, "last_error", "could not load embedding model … check network/proxy access"))

    captured: list = []
    monkeypatch.setattr(sem.threading, "Thread",
                        lambda target, **kw: type("_T", (), {"start": lambda self: captured.append(target)})())

    sem._start_background_index(str(tmp_path), ":memory:", {})
    captured[0]()

    cause = _background_index_failure(str(tmp_path)) or ""
    assert "embedding model" in cause
    assert "close blew up" not in cause
