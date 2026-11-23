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

import codeintel.engines.semantic.provider as sem
from codeintel.engines.semantic.provider import (
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


def test_a_lookup_also_prunes_roots_nobody_asks_about_again(tmp_path):
    """The hole a write-only sweep leaves.

    A burst of one-off roots fails, then failures stop — no later write ever arrives to trigger a
    sweep, so every entry survives for the process lifetime. Pruning per-key on reads has the
    mirror-image hole: it only touches roots someone asks about again. Sweeping on both closes
    both, and this asserts the read half.
    """
    old_at = time.monotonic() - _BG_INDEX_COOLDOWN_S - 1
    for i in range(5):
        sem._BG_INDEX_FAILED[f"/burst/then/silence/{i}"] = (old_at, "ProxyError: 403")

    # An ordinary lookup for an unrelated root — no failure is recorded anywhere.
    assert _background_index_failure(str(tmp_path)) is None

    assert [k for k in sem._BG_INDEX_FAILED if k.startswith("/burst/")] == []


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
    monkeypatch.setattr("codeintel.engines.semantic.searcher.Searcher", FakeSearcher)
    monkeypatch.setattr("codeintel.engines.semantic.db.SemanticDb", FakeDb)
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
    from codeintel.gateway.gateway import Gateway

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
    from codeintel.setup.doctor import _status_for

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
    import codeintel.engines.semantic.db as sdb

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
    import codeintel.engines.semantic.db as sdb
    from codeintel.engines.semantic.indexer import Indexer

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


def test_a_setup_failure_survives_a_failing_close(monkeypatch, tmp_path):
    """The other direction, and the one a "already recorded" flag could not cover.

    An exception raised in a `finally` REPLACES the one already propagating, demoting it to
    `__context__`. So `db.init()` raising and `db.close()` then raising handed the outer handler
    the close error — recording a downstream symptom as the cause, and losing the only sentence
    that says what to fix. Guarding the close at its own site fixes both directions at once.
    """
    import codeintel.engines.semantic.db as sdb

    monkeypatch.setattr(sdb.SemanticDb, "init",
                        lambda self: (_ for _ in ()).throw(RuntimeError("the REAL cause")))
    monkeypatch.setattr(sdb.SemanticDb, "close",
                        lambda self: (_ for _ in ()).throw(OSError("close blew up")))

    captured: list = []
    monkeypatch.setattr(sem.threading, "Thread",
                        lambda target, **kw: type("_T", (), {"start": lambda self: captured.append(target)})())

    sem._start_background_index(str(tmp_path), ":memory:", {})
    captured[0]()

    cause = _background_index_failure(str(tmp_path)) or ""
    assert "the REAL cause" in cause
    assert "close blew up" not in cause


def test_a_close_failure_alone_does_not_mark_the_pass_failed(monkeypatch, tmp_path):
    """A pass that indexed successfully and then failed to close its handle DID succeed.

    Recording it as a failed pass would suppress querying for a whole cooldown over cleanup.
    """
    import codeintel.engines.semantic.db as sdb
    from codeintel.engines.semantic.indexer import Indexer

    monkeypatch.setattr(sdb.SemanticDb, "init", lambda self: None)
    monkeypatch.setattr(sdb.SemanticDb, "close",
                        lambda self: (_ for _ in ()).throw(OSError("close blew up")))
    monkeypatch.setattr(Indexer, "__init__", lambda self, db, **kw: None)
    monkeypatch.setattr(Indexer, "index", lambda self, root: 7)      # a successful pass

    captured: list = []
    monkeypatch.setattr(sem.threading, "Thread",
                        lambda target, **kw: type("_T", (), {"start": lambda self: captured.append(target)})())

    sem._start_background_index(str(tmp_path), ":memory:", {})
    captured[0]()

    assert _background_index_failure(str(tmp_path)) is None


def test_a_setup_failure_surfaces_the_recorded_cause_not_a_bare_provider_error(
    monkeypatch, tmp_path,
):
    """`db.init()` runs long before the branch that consults the registry.

    A persistent cause — a SQLite lock, an unwritable cache — fails the background pass and then
    fails the request too, landing in the outer handler. That returned `provider-error` with no
    hint at all, while an actionable sentence sat in the registry unread.
    """
    import codeintel.engines.semantic.db as sdb

    monkeypatch.setattr(sem, "_DEPS_OK", True, raising=False)
    monkeypatch.setattr(sdb.SemanticDb, "init",
                        lambda self: (_ for _ in ()).throw(
                            RuntimeError("database is locked")))
    _record_background_failure(_index_key(str(tmp_path)), "OperationalError: database is locked")

    r = SemanticProvider(blocking_index=False).build_result(
        "search", "x", [], 0, str(tmp_path),
    )

    assert r["ok"] is True
    assert r["reason"] == "index-failed"
    assert "database is locked" in r["hint"]
    # Both facts, no causation claimed between them.
    assert "RuntimeError" in r["hint"]
    assert "background index pass" in r["hint"]


def test_a_setup_failure_with_no_recorded_cause_is_still_provider_error(monkeypatch, tmp_path):
    """The new branch must not swallow the case it was carved out of, and must not invent a
    background failure for a request that simply could not start."""
    import codeintel.engines.semantic.db as sdb

    monkeypatch.setattr(sem, "_DEPS_OK", True, raising=False)
    monkeypatch.setattr(sdb.SemanticDb, "init",
                        lambda self: (_ for _ in ()).throw(RuntimeError("something else")))

    r = SemanticProvider(blocking_index=False).build_result(
        "search", "x", [], 0, str(tmp_path),
    )

    assert r["reason"] == "provider-error"


def test_the_recovery_lookup_cannot_escape_the_never_raise_boundary(monkeypatch, tmp_path):
    """This runs inside `build_result`'s own handler — a fault there would escape the provider."""
    import codeintel.engines.semantic.db as sdb

    monkeypatch.setattr(sem, "_DEPS_OK", True, raising=False)
    monkeypatch.setattr(sdb.SemanticDb, "init",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sem, "_background_index_failure",
                        lambda root: (_ for _ in ()).throw(RuntimeError("lookup exploded")))

    r = SemanticProvider(blocking_index=False).build_result(
        "search", "x", [], 0, str(tmp_path),
    )

    assert r["ok"] is True
    assert r["reason"] == "provider-error"


def test_the_probe_reads_both_facts_under_one_lock(tmp_path, monkeypatch):
    """A failure landing between two separate lookups let the probe report progress mid-cooldown.

    Milder than the query-path race — nothing acts on it, where that one would have STARTED a pass
    — but a diagnostic contradicting the query path about the same state is its own wrong answer,
    and this is the command people run once they have stopped trusting the others.
    """
    key = _index_key(str(tmp_path))
    sem._BG_INDEX_STARTED[key] = time.monotonic()

    # Drive the interleaving: the failure lands during the read, exactly as the thread would
    # record it between two separate lookups.
    real = sem._unexpired_failure_locked

    def _racy(k):
        sem._BG_INDEX_FAILED[k] = (time.monotonic(), "ProxyError: 403 Forbidden")
        return real(k)

    monkeypatch.setattr(sem, "_unexpired_failure_locked", _racy)

    probe = sem._not_indexed_probe(str(tmp_path), "no semantic index database yet")

    assert "background index pass failed" in probe["detail"]
    assert "indexing in progress" not in probe["detail"]
    assert probe["runnable"] is False


# --------------------------------------------------------------------------------------------- #
# The failure is persisted, so a NEW process does not retry the blocked download for ~40 s
# --------------------------------------------------------------------------------------------- #
#
# `_BG_INDEX_FAILED` lives in one process. Every new process (each CLI call, a restarted server)
# started with it empty, retried the download, and after the cooldown the state flipped back to
# "first-time indexing ~0s ago". The record under `codeintel_home()` carries the failure across.

class _BlockedEmbedding:
    """fastembed's model class on a host where the weights cannot be fetched."""

    built = 0

    def __init__(self, *args, **kwargs):
        type(self).built += 1
        raise ValueError("Could not load model BAAI/bge-small-en-v1.5 from any source.")

    @classmethod
    def list_supported_models(cls):
        return [{"model": "BAAI/bge-small-en-v1.5"}]


class _WorkingEmbedding:
    """fastembed's model class once the weights are reachable."""

    def __init__(self, *args, **kwargs):
        pass

    def embed(self, texts):
        import numpy as np
        return [np.full(384, 0.1, dtype=np.float32) for _ in list(texts)]


@pytest.fixture
def blocked_host(tmp_path, monkeypatch):
    """A repo, an empty fastembed cache and a model that cannot load. Returns the repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core.py").write_text("def helper():\n    return 1\n")
    cache = tmp_path / "fastembed-empty"
    cache.mkdir()
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
    monkeypatch.delenv("CODEINTEL_REINDEX", raising=False)
    monkeypatch.setattr(sem, "_DEPS_OK", True)
    _BlockedEmbedding.built = 0
    monkeypatch.setattr("fastembed.TextEmbedding", _BlockedEmbedding)
    return str(repo)


def _record_path():
    from codeintel.core.paths import codeintel_home
    return codeintel_home() / "semantic_failure.json"


def _fail_once(root: str) -> dict:
    """One inline pass that fails to load the model, as `codeintel query` would run it."""
    r = SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, root)
    assert r["reason"] == "index-failed", r
    assert _BlockedEmbedding.built == 1
    return r


def _as_a_new_process() -> None:
    sem._BG_INDEX_STARTED.clear()
    sem._BG_INDEX_FAILED.clear()


def test_a_model_load_failure_is_written_to_a_record_under_codeintel_home(blocked_host):
    import json
    import os

    _fail_once(blocked_host)

    record = json.loads(_record_path().read_text())
    assert set(record) == {"cause", "at", "model", "fastembed_cache_path", "model_files_present"}
    assert "could not load embedding model" in record["cause"]
    assert record["model"] == "BAAI/bge-small-en-v1.5"
    assert record["fastembed_cache_path"] == os.environ["FASTEMBED_CACHE_PATH"]
    assert record["model_files_present"] is False
    assert abs(record["at"] - time.time()) < 60
    # Written atomically: no temp file is left beside it.
    assert sorted(p.name for p in _record_path().parent.iterdir()
                  if p.name.startswith("semantic_failure") or p.name.startswith(".semantic")) == [
        "semantic_failure.json"]


@pytest.mark.parametrize("blocking", [False, True])
def test_a_new_process_fails_fast_with_the_recorded_cause(blocked_host, blocking, monkeypatch):
    _fail_once(blocked_host)
    _as_a_new_process()
    started: list[str] = []
    real_start = sem._start_background_index
    monkeypatch.setattr(sem, "_start_background_index",
                        lambda *a, **k: started.append("bg") or real_start(*a, **k))

    t0 = time.monotonic()
    r = SemanticProvider(blocking_index=blocking).build_result(
        "search", "helper", [], 0, blocked_host)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0
    assert r["reason"] == "index-failed"
    assert "could not load" in r["hint"]
    assert "first-time indexing" not in r["hint"]
    assert "codeintel index" in r["hint"]
    assert "FASTEMBED_CACHE_PATH" in r["hint"]
    assert _BlockedEmbedding.built == 1          # the model load was not tried again
    assert started == []                         # and no background pass was started


def test_a_changed_fastembed_cache_path_lets_an_index_be_tried_again(
        blocked_host, tmp_path, monkeypatch):
    _fail_once(blocked_host)
    _as_a_new_process()
    elsewhere = tmp_path / "fastembed-elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(elsewhere))

    SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert _BlockedEmbedding.built == 2


def test_a_different_model_lets_an_index_be_tried_again(blocked_host, monkeypatch):
    import json

    _fail_once(blocked_host)
    _as_a_new_process()
    record = json.loads(_record_path().read_text())
    record["model"] = "some/other-model"
    _record_path().write_text(json.dumps(record))

    SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert _BlockedEmbedding.built == 2


def test_model_files_that_appear_let_an_index_be_tried_again(blocked_host, monkeypatch):
    _fail_once(blocked_host)
    _as_a_new_process()
    monkeypatch.setattr("codeintel.engines.semantic.db.model_is_cached", lambda *a, **k: True)

    SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert _BlockedEmbedding.built == 2


@pytest.mark.parametrize("damage", ["older-than-10-minutes", "future-at", "nan-at", "not-json",
                                    "not-an-object", "at-not-a-number", "cause-not-text"])
def test_a_record_that_cannot_be_trusted_lets_an_index_be_tried_again(blocked_host, damage):
    import json

    _fail_once(blocked_host)
    _as_a_new_process()
    record = json.loads(_record_path().read_text())
    if damage == "older-than-10-minutes":
        record["at"] = time.time() - 601
        text = json.dumps(record)
    elif damage == "future-at":
        record["at"] = time.time() + 3600
        text = json.dumps(record)
    elif damage == "nan-at":
        # `json.loads` reads `NaN` as float('nan'): every age comparison with it is False, so a
        # record dated NaN must not be read as forever recent.
        record["at"] = float("nan")
        text = json.dumps(record)
        assert '"at": NaN' in text
    elif damage == "not-json":
        text = "{not json"
    elif damage == "not-an-object":
        text = json.dumps([record])
    elif damage == "at-not-a-number":
        record["at"] = "now"
        text = json.dumps(record)
    else:
        record["cause"] = {"text": "x"}
        text = json.dumps(record)
    _record_path().write_text(text)

    r = SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert _BlockedEmbedding.built == 2
    # A record that is not trusted is not trusted for its cause either: this answer's cause is
    # the pass just run, never the planted text.
    assert r["reason"] == "index-failed"
    assert "could not load embedding model" in r["hint"]


def test_a_record_reached_through_a_symlink_is_ignored(blocked_host, tmp_path):
    """The record is read only under `codeintel_home()`: a link out of it is not followed."""
    import json

    _fail_once(blocked_host)
    _as_a_new_process()
    outside = tmp_path / "planted.json"
    outside.write_text(_record_path().read_text())
    _record_path().unlink()
    _record_path().symlink_to(outside)
    assert json.loads(_record_path().read_text())["model"]          # the link itself resolves

    SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert _BlockedEmbedding.built == 2


def test_a_successful_index_pass_removes_the_record(blocked_host, monkeypatch):
    _fail_once(blocked_host)
    _as_a_new_process()
    assert _record_path().exists()
    monkeypatch.setattr("codeintel.engines.semantic.db.model_is_cached", lambda *a, **k: True)
    monkeypatch.setattr("fastembed.TextEmbedding", _WorkingEmbedding)

    r = SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert r["result"] is not None, r
    assert not _record_path().exists()


def test_a_failed_background_pass_writes_the_record_too(blocked_host, monkeypatch):
    captured: list = []
    monkeypatch.setattr(sem.threading, "Thread",
                        lambda target, **kw: type("_T", (), {"start": lambda self: captured.append(target)})())

    first = SemanticProvider(blocking_index=False).build_result(
        "search", "helper", [], 0, blocked_host)
    assert first["reason"] == "indexing-in-progress"
    captured[0]()                                    # run the thread body inline
    assert _record_path().exists()

    _as_a_new_process()
    second = SemanticProvider(blocking_index=False).build_result(
        "search", "helper", [], 0, blocked_host)
    assert second["reason"] == "index-failed"
    assert "first-time indexing" not in second["hint"]
    assert len(captured) == 1                        # no second pass started


def test_an_unwritable_codeintel_home_costs_only_the_record(blocked_host, tmp_path, monkeypatch):
    """Persisting the failure is best effort: it must never replace the answer."""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    monkeypatch.setattr("codeintel.engines.semantic.db._base_dir", lambda: db_dir)
    not_a_dir = tmp_path / "home-is-a-file"
    not_a_dir.write_text("x")
    monkeypatch.setenv("CODEINTEL_HOME", str(not_a_dir))

    r = SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert r["reason"] == "index-failed"
    assert "could not load embedding model" in r["hint"]


def _fail_without_touching_the_model(monkeypatch) -> None:
    """Make every index pass fail for a reason that is not the model: an unwritable database."""
    def _broken(self, project_root):
        raise PermissionError("attempt to write a readonly database")
    monkeypatch.setattr("codeintel.engines.semantic.indexer.Indexer._index", _broken)


def test_a_failure_that_is_not_a_model_load_writes_no_record(blocked_host, monkeypatch):
    """The record blocks a model download for 10 minutes; only a failed model load may set it."""
    _fail_without_touching_the_model(monkeypatch)

    r = SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)

    assert r["reason"] == "index-failed"
    assert "PermissionError" in r["hint"]
    assert not _record_path().exists()
    _as_a_new_process()
    SemanticProvider(blocking_index=True).build_result("search", "helper", [], 0, blocked_host)
    assert not _record_path().exists()


def test_a_background_failure_that_is_not_a_model_load_writes_no_record(blocked_host, monkeypatch):
    _fail_without_touching_the_model(monkeypatch)
    captured: list = []
    monkeypatch.setattr(sem.threading, "Thread",
                        lambda target, **kw: type("_T", (), {"start": lambda self: captured.append(target)})())

    SemanticProvider(blocking_index=False).build_result("search", "helper", [], 0, blocked_host)
    captured[0]()                                    # run the thread body inline

    assert sem._background_index_failure(blocked_host) is not None   # the pass did fail
    assert not _record_path().exists()
