from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import MagicMock

from codeintel.injector import Injector
from codeintel.mapper import MapGenerator, _is_populated_map, _minimal_map
from codeintel.providers.graph import ProjectResolution

_POPULATED = (
    "# CODE_INTEL.md — repo\n\n"
    "## Ranked Symbols (by caller count)\n"
    "| Symbol | File | Callers |\n|--------|------|---------|\n| `foo` | a.py | 9 |\n"
)


def _make_provider(ranked=None, entry=None, arch=None):
    """Return a mocked GraphProvider with configurable return values."""
    provider = MagicMock()
    provider.available = True
    provider._resolve_project.return_value = ProjectResolution(
        name="test-project", matched_root="/repo", scope="exact")

    # build_result for "overview"
    if arch is None:
        provider.build_result.return_value = {
            "ok": True,
            "op": "overview",
            "target": "",
            "result": "## Modules\n- src/codeintel/mapper.py",
            "engine": "graph",
            "cached": False,
        }
    else:
        provider.build_result.return_value = arch

    # _run for query_graph calls — returns ranked then entry
    ranked_rows = ranked if ranked is not None else []
    entry_rows = entry if entry is not None else []
    provider._run.side_effect = [ranked_rows, entry_rows]
    return provider


# ---------------------------------------------------------------------------
# MapGenerator tests
# ---------------------------------------------------------------------------

def test_generate_with_empty_graph():
    provider = _make_provider(ranked=[], entry=[])
    gen = MapGenerator(provider)
    result = gen.generate("/tmp/repo")
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_with_no_provider():
    gen = MapGenerator(None)
    result = gen.generate("/tmp/repo")
    assert isinstance(result, str)
    assert "not available" in result.lower() or "not" in result.lower()


def test_generate_byte_budget_enforced():
    ranked = [
        {"fn.name": f"func_{i}", "fn.file_path": f"src/module_{i}.py", "in_degree": 30 - i}
        for i in range(30)
    ]
    provider = _make_provider(ranked=ranked, entry=[])
    gen = MapGenerator(provider)
    budget = 500
    result = gen.generate("/tmp/repo", budget_bytes=budget)
    assert len(result.encode("utf-8")) <= budget + len(b"> Content truncated to fit")
    assert "truncated" in result.lower()


def test_generate_deterministic():
    ranked = [
        {"fn.name": "alpha", "fn.file_path": "src/alpha.py", "in_degree": 10},
        {"fn.name": "beta", "fn.file_path": "src/beta.py", "in_degree": 5},
    ]

    def make_gen():
        p = _make_provider(ranked=ranked, entry=[])
        return MapGenerator(p)

    # The rendered map is now stamped with a generation timestamp (staleness disclosure — see
    # mapper.py's `_stamp_line`), so byte-for-byte determinism across two calls requires pinning
    # the clock the same way the ranked/provider inputs are already pinned: without `now` fixed,
    # two real-time calls a moment apart would legitimately differ in that one line. This is a
    # genuine behavior change, not a weakened test — the content IS supposed to carry the
    # generation time now.
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    result1 = make_gen().generate("/tmp/repo", now=fixed_now)
    result2 = make_gen().generate("/tmp/repo", now=fixed_now)
    assert result1 == result2


def test_generate_stamps_generation_time_and_index_counts():
    """CODE_INTEL.md is a static, committed snapshot read long after it was generated — this
    repo's own committed copy was found 172 nodes / 1,413 edges behind its live index with
    nothing on the page to reveal it. The stamp must carry both."""
    arch = {
        "ok": True, "op": "overview", "target": "",
        "result": "## Architecture: repo\n120 nodes, 340 edges\n",
        "engine": "graph", "cached": False,
    }
    provider = _make_provider(ranked=[], entry=[], arch=arch)
    gen = MapGenerator(provider)
    fixed_now = datetime(2026, 1, 1, 12, 30, 0, tzinfo=UTC)

    result = gen.generate("/tmp/repo", now=fixed_now)

    assert "2026-01-01T12:30:00Z" in result
    assert "120 nodes / 340 edges" in result


def test_generate_stamp_omits_counts_when_overview_unavailable():
    """No architecture section (backend down, repo unindexed at the moment of the overview call)
    means no counts to stamp — the stamp must still carry a generation time, just not a false
    node/edge count."""
    arch = {
        "ok": True, "op": "overview", "target": "", "result": "## Modules\n- src/alpha.py",
        "engine": "graph", "cached": False,
    }
    provider = _make_provider(ranked=[], entry=[], arch=arch)
    gen = MapGenerator(provider)
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)

    result = gen.generate("/tmp/repo", now=fixed_now)

    assert "2026-01-01T00:00:00Z" in result
    assert "from an index of" not in result


def test_minimal_map_stub_carries_a_generation_timestamp():
    """A stub ("not indexed", "graph unavailable") is itself a claim with a shelf life — a reader
    should be able to tell when even a stub note was produced."""
    stub = _minimal_map("/tmp/repo", note="project not yet indexed",
                         generated_at="2026-01-01T00:00:00Z")
    assert "2026-01-01T00:00:00Z" in stub
    # backward compatible: omitting generated_at is still valid (existing direct callers).
    plain = _minimal_map("/tmp/repo", note="x")
    assert "Auto-generated by `codeintel map`." in plain


def test_generate_reads_columns_rows_shape():
    """The real backend returns {columns, rows}; the old mapper only accepted a list and so
    always rendered an empty table. This locks the real shape + the builtin/project noise filter."""
    ranked = {
        "columns": ["fn.name", "fn.qualified_name", "fn.file_path", "in_degree"],
        "rows": [
            ["query", "codeintel.gateway.Gateway.query", "src/codeintel/gateway.py", "29"],
            ["str", "builtins.str", "<python-builtins>", "27"],       # noise → dropped
            ["codeintel", "codeintel.pyproject", "pyproject.toml", "30"],  # noise → dropped
            ["Result", "codeintel.provider.Result", "src/codeintel/provider.py", "22"],
        ],
    }
    entry = {"columns": ["fn.name", "fn.file_path"], "rows": [["main", "src/codeintel/__main__.py"]]}
    provider = MagicMock()
    provider.available = True
    provider._resolve_project.return_value = ProjectResolution(
        name="proj", matched_root="/repo", scope="exact")
    provider.build_result.return_value = {
        "ok": True, "op": "overview", "target": "", "result": "## Arch",
        "engine": "graph", "cached": False,
    }
    provider._run.side_effect = [ranked, entry]

    out = MapGenerator(provider).generate("/repo")
    assert "## Ranked Symbols (by caller count)" in out
    assert "`query`" in out and "29" in out
    assert "`Result`" in out
    assert "<python-builtins>" not in out and "`str`" not in out  # noise filtered
    assert "pyproject.toml" not in out
    assert "## Entry Points" in out and "`main`" in out


def test_write_creates_file():
    gen = MapGenerator(None)
    with tempfile.TemporaryDirectory() as d:
        path, wrote = gen.write(d, "# content")
        expected = os.path.join(d, "CODE_INTEL.md")
        assert path == expected and wrote is True
        assert os.path.exists(expected)
        assert open(expected).read() == "# content"


def test_write_preserves_populated_map_when_new_is_a_stub():
    # the dogfooding bug: `codeintel index`'s best-effort refresh produced a stub (graph empty/
    # unindexed at that moment) and clobbered a rich, populated CODE_INTEL.md. It must NOT.
    gen = MapGenerator(None)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "CODE_INTEL.md")
        open(path, "w").write(_POPULATED)
        stub = _minimal_map(d, note="project not yet indexed")
        assert not _is_populated_map(stub) and _is_populated_map(_POPULATED)  # sanity
        p, wrote = gen.write(d, stub)
        assert p == path and wrote is False          # preserved → reported as not written
        assert open(path).read() == _POPULATED        # preserved, not stubbed


def test_write_stub_ok_when_no_existing_map():
    # first-ever generation of a stub is fine — there's nothing to preserve
    gen = MapGenerator(None)
    with tempfile.TemporaryDirectory() as d:
        _, wrote = gen.write(d, _minimal_map(d, note="x"))
        assert wrote is True
        assert "Auto-generated" in open(os.path.join(d, "CODE_INTEL.md")).read()


def test_write_populated_replaces_a_stub():
    # a real map SHOULD replace a prior stub — the normal refresh-improves case still works
    gen = MapGenerator(None)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "CODE_INTEL.md")
        open(path, "w").write(_minimal_map(d, note="stale"))
        _, wrote = gen.write(d, _POPULATED)
        assert wrote is True
        assert _is_populated_map(open(path).read())


def test_entry_points_only_map_counts_as_populated():
    # a sparse-but-real render (entry points, no arch/ranked table) is NOT a stub — it must refresh,
    # not be misclassified and skipped (regression for the review's false-negative finding)
    entry_only = ("# CODE_INTEL.md — repo\n\n## Ranked Symbols\n_(no symbols found)_\n\n"
                  "## Entry Points\n- `main` (app.py)\n")
    assert _is_populated_map(entry_only)
    gen = MapGenerator(None)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "CODE_INTEL.md")
        open(path, "w").write(_POPULATED)
        _, wrote = gen.write(d, entry_only)   # real content replaces the old map, not skipped
        assert wrote is True and "Entry Points" in open(path).read()


# ---------------------------------------------------------------------------
# Injector tests
# ---------------------------------------------------------------------------

def test_inject_appends_block():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        claude = os.path.join(d, "CLAUDE.md")
        open(claude, "w").write("# Project\n")
        path, action = inj.inject(d)
        assert action == "appended"
        assert path == claude
        content = open(claude).read()
        assert "<!-- codeintel-map-start -->" in content
        assert "<!-- codeintel-map-end -->" in content


def test_inject_is_idempotent():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        claude = os.path.join(d, "CLAUDE.md")
        open(claude, "w").write("# Project\n")
        inj.inject(d)
        inj.inject(d)
        content = open(claude).read()
        assert content.count("<!-- codeintel-map-start -->") == 1
        assert content.count("<!-- codeintel-map-end -->") == 1


def test_inject_no_context_file():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        result = inj.inject(d)
        assert result == (None, "no-context-file")


def test_inject_updates_existing_block():
    inj = Injector()
    with tempfile.TemporaryDirectory() as d:
        claude = os.path.join(d, "CLAUDE.md")
        initial = (
            "# Project\n\n"
            "<!-- codeintel-map-start -->\nold content\n<!-- codeintel-map-end -->\n"
        )
        open(claude, "w").write(initial)
        path, action = inj.inject(d)
        assert action == "updated"
        assert path == claude
        content = open(claude).read()
        assert content.count("<!-- codeintel-map-start -->") == 1
        assert content.count("<!-- codeintel-map-end -->") == 1
        assert "old content" not in content


# --------------------------------------------------------------------------- injector safety

def test_inject_never_duplicates_the_users_own_content(tmp_path, monkeypatch):
    """A CLAUDE.md holding an END marker without its START — a hand-edit, a bad merge, or this
    function's own append branch — made every subsequent --inject re-emit the text between the
    stray marker and the block, growing without bound. CLAUDE.md is prompt context, so this
    silently degraded the agent it serves."""
    from codeintel.injector import _END_MARKER, Injector

    monkeypatch.chdir(tmp_path)
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(f"# rules\n{_END_MARKER}\nImportant instruction B\n")

    sizes, counts = [], []
    for _ in range(3):
        Injector().inject(str(tmp_path))
        text = claude.read_text()
        sizes.append(len(text))
        counts.append(text.count("Important instruction B"))

    assert counts == [1, 1, 1], f"user content duplicated: {counts}"
    assert sizes[0] == sizes[1] == sizes[2], f"file grew on every run: {sizes}"


def test_inject_preserves_file_mode_and_line_endings(tmp_path, monkeypatch):
    """os.replace installs a NEW inode, so the original's permissions were dropped for the umask
    and universal-newline mode rewrote CRLF to LF — a whole-file diff on every --inject."""
    from codeintel.injector import Injector

    monkeypatch.chdir(tmp_path)
    claude = tmp_path / "CLAUDE.md"
    claude.write_bytes(b"# rules\r\nline A\r\n")
    os.chmod(claude, 0o600)

    Injector().inject(str(tmp_path))

    assert os.stat(claude).st_mode & 0o777 == 0o600, "file permissions were widened"
    assert b"\r\n" in claude.read_bytes(), "the file's CRLF line endings were rewritten"


def test_inject_writes_through_a_symlinked_rules_file(tmp_path, monkeypatch):
    """A symlinked CLAUDE.md (dotfile manager, shared team rules) was replaced by a regular file,
    orphaning the real source."""
    from codeintel.injector import Injector

    monkeypatch.chdir(tmp_path)
    real = tmp_path / "shared-rules.md"
    real.write_text("# team rules\n")
    (tmp_path / "CLAUDE.md").symlink_to(real)

    Injector().inject(str(tmp_path))

    assert (tmp_path / "CLAUDE.md").is_symlink(), "the symlink was replaced by a regular file"
    assert "codeintel" in real.read_text().lower(), "the block did not reach the link target"
