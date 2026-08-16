"""Unit tests for ContentHashCache — the LRU bound added in 0.2.2 so the long-lived server
can't grow the query cache without limit. Real cache object, no mocks."""
from __future__ import annotations

import hashlib

from codeintel.cache import ContentHashCache, _compute_hash


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


# --------------------------------------------------------------------------- relative targets

def test_a_relative_target_hashes_the_file_inside_the_project_root(tmp_path, monkeypatch):
    """`overview` takes a REPO-RELATIVE path (it becomes serena's `relative_path`). This resolved
    the target against the process cwd instead, so for a long-lived server answering for arbitrary
    project roots the file was almost never found — and every such entry silently fell back to
    hashing the string, which cannot change when the file does.
    """
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "app.py"
    f.write_text("def a(): pass\n")
    monkeypatch.chdir(tmp_path.parent)          # cwd deliberately NOT the project root

    before = _compute_hash("src/app.py", str(tmp_path))
    assert before != hashlib.sha256(b"src/app.py").hexdigest(), "fell back to hashing the string"

    f.write_text("def a(): pass\ndef b(): pass\n")
    assert _compute_hash("src/app.py", str(tmp_path)) != before, "an edit must change the hash"


def test_an_absolute_target_still_hashes_its_content(tmp_path):
    f = tmp_path / "app.py"
    f.write_text("x = 1\n")
    before = _compute_hash(str(f), str(tmp_path))
    f.write_text("x = 2\n")
    assert _compute_hash(str(f), str(tmp_path)) != before


def test_a_relative_target_escaping_the_root_is_not_hashed(tmp_path):
    """Containment still applies after joining — `../secret.py` must not be read just because it
    resolves to a real file."""
    outside = tmp_path / "secret.py"
    outside.write_text("token = 'sekrit'\n")
    proj = tmp_path / "proj"
    proj.mkdir()

    assert _compute_hash("../secret.py", str(proj)) == \
        hashlib.sha256(b"../secret.py").hexdigest()


def test_a_symbol_target_that_is_not_a_file_still_hashes_the_string(tmp_path):
    """Most targets are symbol names, not paths; they must keep falling back cleanly."""
    assert _compute_hash("safe_null_result", str(tmp_path)) == \
        hashlib.sha256(b"safe_null_result").hexdigest()
