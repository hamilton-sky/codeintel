"""Unit tests for ContentHashCache — the LRU bound added in 0.2.2 so the long-lived server
can't grow the query cache without limit. Real cache object, no mocks."""
from __future__ import annotations

from codeintel.cache import ContentHashCache


def _result(tag: str) -> dict:
    return {"ok": True, "op": "search", "target": tag, "result": f"r-{tag}",
            "engine": "semantic", "cached": False}


def _put(cache: ContentHashCache, tag: str) -> None:
    cache.put("search", tag, "semantic", "", _result(tag), 0)


def _get(cache: ContentHashCache, tag: str):
    return cache.get("search", tag, "semantic", "", 0)


def test_hit_returns_stored_result():
    c = ContentHashCache()
    _put(c, "x")
    got = _get(c, "x")
    assert got is not None and got["result"] == "r-x"


def test_freshness_bump_busts_the_entry():
    c = ContentHashCache()
    _put(c, "x")                                   # stored at freshness 0
    assert c.get("search", "x", "semantic", "", 1) is None  # a completed reindex invalidates it


def test_null_result_is_not_cached():
    c = ContentHashCache()
    c.put("search", "x", "semantic", "", {"ok": True, "result": None}, 0)
    assert _get(c, "x") is None


def test_evicts_least_recently_used_past_capacity():
    c = ContentHashCache(max_entries=3)
    for t in ("t0", "t1", "t2"):
        _put(c, t)
    # Touch t0 so it becomes most-recently-used; t1 is now the LRU.
    assert _get(c, "t0") is not None
    _put(c, "t3")  # over capacity → evict the LRU (t1)

    assert _get(c, "t1") is None       # evicted
    assert _get(c, "t0") is not None   # kept — it was accessed most recently
    assert _get(c, "t2") is not None   # kept
    assert _get(c, "t3") is not None   # just inserted


def test_capacity_is_never_exceeded():
    c = ContentHashCache(max_entries=5)
    for i in range(50):
        _put(c, f"k{i}")
    assert len(c._store) == 5
    # The 5 most recent survive; older keys are gone.
    assert _get(c, "k49") is not None
    assert _get(c, "k0") is None
