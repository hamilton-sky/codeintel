from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import MagicMock

from codeintel.injector import Injector
from codeintel.mapper import (
    _RANK_LABELS,
    MapGenerator,
    _is_populated_map,
    _minimal_map,
)
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

    # _run for query_graph calls. Dispatched on the query TEXT rather than call order: the ranking
    # issues one query per node label (see mapper._RANK_LABELS), so a positional side_effect list
    # would break — and silently, by StopIteration — every time that label set changes. `ranked`
    # rows are served for the `Function` label only, the way the real backend serves each label its
    # own disjoint rows, so a fixture is never duplicated across labels.
    ranked_rows = ranked if ranked is not None else []
    entry_rows = entry if entry is not None else []

    def _dispatch(method, payload, timeout_ms):
        q = str(payload.get("query", ""))
        if "is_entry_point" in q:
            return entry_rows
        if "(fn:Function)" in q:
            return ranked_rows
        return []

    provider._run.side_effect = _dispatch
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
    def _dispatch(method, payload, timeout_ms):
        q = str(payload.get("query", ""))
        if "is_entry_point" in q:
            return entry
        return ranked if "(fn:Function)" in q else []

    provider._run.side_effect = _dispatch

    out = MapGenerator(provider).generate("/repo")
    assert "## Ranked Symbols (by caller count)" in out
    assert "`query`" in out and "29" in out
    assert "`Result`" in out
    assert "<python-builtins>" not in out and "`str`" not in out  # noise filtered
    assert "pyproject.toml" not in out
    assert "## Entry Points" in out and "`main`" in out
    # The backend returns aggregate counts as strings; ranking merges several per-label queries and
    # so sorts client-side. Lexicographic order would put "29" above "22" by luck but "9" above
    # "22" by bug, so pin that the higher count leads.
    assert out.index("`query`") < out.index("`Result`")


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


# --------------------------------------------------------------------------- AGENTS.md / CLAUDE.md

def test_inject_refuses_to_create_agents_md_without_consent(tmp_path):
    """No CLAUDE.md, no AGENTS.md, and no ``create=True``: nothing is written. Writing into a file
    that shapes an agent's future behaviour needs consent, not a default."""
    from codeintel.injector import Injector

    path, action = Injector().inject(str(tmp_path))
    assert (path, action) == (None, "no-context-file")
    assert not (tmp_path / "AGENTS.md").exists()


def test_inject_creates_agents_md_with_consent(tmp_path):
    from codeintel.injector import Injector

    path, action = Injector().inject(str(tmp_path), create=True)
    assert path == str(tmp_path / "AGENTS.md")
    assert action == "created"
    content = (tmp_path / "AGENTS.md").read_text()
    assert "<!-- codeintel-map-start -->" in content
    assert "code.query" in content
    assert (tmp_path / "USING_CODEINTEL.md").exists()


def test_inject_agents_md_is_idempotent(tmp_path):
    from codeintel.injector import Injector

    Injector().inject(str(tmp_path), create=True)
    Injector().inject(str(tmp_path), create=True)
    content = (tmp_path / "AGENTS.md").read_text()
    assert content.count("<!-- codeintel-map-start -->") == 1
    assert content.count("<!-- codeintel-map-end -->") == 1


def test_inject_preserves_user_prose_in_agents_md(tmp_path):
    from codeintel.injector import Injector

    (tmp_path / "AGENTS.md").write_text("# Team rules\n\nDo not touch payments/.\n")
    Injector().inject(str(tmp_path), create=True)
    content = (tmp_path / "AGENTS.md").read_text()
    assert "Do not touch payments/." in content
    assert "code.query" in content


def test_inject_gives_claude_md_a_one_line_agents_import_when_both_exist(tmp_path):
    from codeintel.injector import Injector

    (tmp_path / "CLAUDE.md").write_text("# Project rules\n")
    Injector().inject(str(tmp_path), create=True)
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert "@AGENTS.md" in claude
    assert "code.query" not in claude   # the full block lives in AGENTS.md only


def test_inject_migrates_a_pre_agents_md_claude_block_to_an_import(tmp_path):
    """A CLAUDE.md that already carries codeintel's OLD full block (written before AGENTS.md
    existed) gets it replaced with the one-line import once AGENTS.md becomes the canonical
    target — otherwise the stale block would sit there forever."""
    from codeintel.injector import Injector

    (tmp_path / "CLAUDE.md").write_text(
        "# rules\n<!-- codeintel-map-start -->\nold stale block\n<!-- codeintel-map-end -->\n"
    )
    Injector().inject(str(tmp_path), create=True)
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert "old stale block" not in claude
    assert "@AGENTS.md" in claude


def test_inject_without_consent_falls_back_to_claude_md_full_block(tmp_path):
    """No AGENTS.md and no consent to create one: CLAUDE.md keeps getting the full block, so an
    existing CLAUDE.md-only setup is not a silent no-op."""
    from codeintel.injector import Injector

    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    path, action = Injector().inject(str(tmp_path))
    assert path == str(tmp_path / "CLAUDE.md")
    assert action == "appended"
    assert "code.query" in (tmp_path / "CLAUDE.md").read_text()


def test_using_codeintel_md_names_the_tools_and_confidence_caveat(tmp_path):
    from codeintel.injector import Injector

    Injector().inject(str(tmp_path), create=True)
    content = (tmp_path / "USING_CODEINTEL.md").read_text()
    for needle in ("code.query", "code.map", "code.status", "code.doctor", "confidence", "gaps"):
        assert needle in content


def test_injected_block_names_the_tools_and_trigger_phrases(tmp_path):
    from codeintel.injector import Injector

    Injector().inject(str(tmp_path), create=True)
    content = (tmp_path / "AGENTS.md").read_text()
    for needle in ("code.query", "code.map", "code.status", "code.doctor", "changed",
                   "who calls X", "confidence", "gaps"):
        assert needle in content


# --------------------------------------------------------------------------- offer_injection

def test_offer_injection_prompts_on_a_tty_and_injects_on_yes(tmp_path, monkeypatch):
    from codeintel.injector import offer_injection

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    result = offer_injection(str(tmp_path))
    assert result["action"] == "created"
    assert (tmp_path / "AGENTS.md").exists()


def test_offer_injection_declines_on_no(tmp_path, monkeypatch):
    from codeintel.injector import offer_injection

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    result = offer_injection(str(tmp_path))
    assert result["action"] == "declined"
    assert not (tmp_path / "AGENTS.md").exists()


def test_offer_injection_prints_command_off_a_tty(tmp_path, monkeypatch, capsys):
    from codeintel.injector import offer_injection

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    result = offer_injection(str(tmp_path))
    assert result["action"] == "printed"
    assert "codeintel map --inject" in capsys.readouterr().out
    assert not (tmp_path / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# Ranked-symbols label discipline
#
# The table these tests guard is WRITTEN to CODE_INTEL.md and committed, under the claim that it
# shows "the load-bearing code at a glance". Two real repos falsified that claim: brightsky-ai
# ranked `logger` (a Folder node) at 716 and `error` (a Channel) at 403, and pathly-adapters ranked
# YAML and JSON keys as its most load-bearing symbols. Both are reproduced below with the rows those
# repos actually produced.
# ---------------------------------------------------------------------------

def _capture_queries(ranked_by_label=None, entry=None):
    """Provider that records every Cypher it is asked to run. *ranked_by_label* maps a label name to
    the rows that label's query should return."""
    queries: list[str] = []
    by_label = ranked_by_label or {}

    provider = MagicMock()
    provider.available = True
    provider._resolve_project.return_value = ProjectResolution(
        name="proj", matched_root="/repo", scope="exact")
    provider.build_result.return_value = {
        "ok": True, "op": "overview", "target": "", "result": "## Arch",
        "engine": "graph", "cached": False,
    }

    def _dispatch(method, payload, timeout_ms):
        q = str(payload.get("query", ""))
        queries.append(q)
        if "is_entry_point" in q:
            return entry or []
        for label, rows in by_label.items():
            if f"(fn:{label})" in q:
                return rows
        return []

    provider._run.side_effect = _dispatch
    return provider, queries


def test_ranked_symbols_never_requests_a_non_callable_label():
    """The fix lives in the QUERY, not in a post-filter: the backend applies `LIMIT` server-side, so
    label noise that reaches the client has already consumed the window and no Python-side filter
    can recover the real symbols it displaced. Assert the request itself is constrained."""
    provider, queries = _capture_queries()
    MapGenerator(provider).generate("/repo")

    ranking = [q for q in queries if "in_degree" in q]
    assert ranking, "no fan-in query was issued at all"
    for label in ("Variable", "Folder", "Module", "File", "Section",
                  "Channel", "EnvVar", "Decorator"):
        assert not any(f"(fn:{label})" in q for q in ranking), (
            f"{label} is not a callable and must never be ranked by caller count")
    for q in ranking:
        assert any(f"(fn:{lbl})" in q for lbl in _RANK_LABELS), (
            f"fan-in query constrains no callable label, so every node type competes: {q}")


def test_ranked_symbols_counts_calls_not_usage():
    """`USAGE` is what inflated a `logger` variable to 716 "callers" — every mention of that name in
    the tree. The heading promises caller count; `CALLS` is what delivers it."""
    provider, queries = _capture_queries()
    MapGenerator(provider).generate("/repo")

    for q in (q for q in queries if "in_degree" in q):
        assert "[:CALLS]" in q, f"fan-in must rank on CALLS alone: {q}"
        assert "USAGE" not in q, f"USAGE inflates a caller count with bare-name mentions: {q}"


def test_ranked_symbols_drops_a_framework_decorator_bound_to_a_spec_file():
    """brightsky-ai regression. NestJS's `Injectable`/`Inject`/`Optional` are `Function` nodes — the
    label filter cannot touch them — bound by name collision to a `.spec.ts` under `__tests__/`.
    They were the table's top three rows. `_looks_like_test` recognised neither convention."""
    spec = "backend/src/__tests__/agent/ack-checkpoint.service.spec.ts"
    provider, _ = _capture_queries(ranked_by_label={"Function": {
        "columns": ["fn.name", "fn.qualified_name", "fn.file_path", "in_degree"],
        "rows": [
            ["Injectable", "backend.Injectable", spec, "138"],
            ["Inject", "backend.Inject", spec, "59"],
            ["useAppDispatch", "frontend.store.hooks.useAppDispatch",
             "frontend/src/store/hooks.ts", "46"],
        ],
    }})
    out = MapGenerator(provider).generate("/repo")

    assert "`Injectable`" not in out and "`Inject`" not in out
    assert "`useAppDispatch`" in out, "a real symbol must survive the widened test filter"


def test_ranked_symbols_drops_yaml_and_json_keys():
    """pathly-adapters regression: `flow` from a .flow.yaml (144), `feature` from a .schema.json
    (126) and `name` from an architect.yaml (99) ranked above every real function. A YAML/JSON key
    is a `Variable` node, so constraining the label is what keeps it out."""
    provider, _ = _capture_queries(ranked_by_label={
        "Variable": {  # the label the ranking must never ask for — served anyway to prove it doesn't
            "columns": ["fn.name", "fn.file_path", "in_degree"],
            "rows": [["flow", "src/pathly_data/core/flows/quick-fix.flow.yaml", "144"]],
        },
        "Function": {
            "columns": ["fn.name", "fn.file_path", "in_degree"],
            "rows": [
                ["feature", "src/pathly_data/schemas/state.schema.json", "126"],
                ["get_db", "src/pathly_orchestrator/db/connection.py", "579"],
            ],
        },
    })
    out = MapGenerator(provider).generate("/repo")

    assert "`flow`" not in out, "a Variable label must never be requested"
    assert "quick-fix.flow.yaml" not in out
    assert "`feature`" not in out, "_is_data_file must drop a JSON key even at a callable label"
    assert "`get_db`" in out and "579" in out


def test_ranked_symbols_merges_labels_in_numeric_order():
    """Counts arrive as strings and now several queries are merged client-side, so the sort is ours
    to get wrong. Lexicographically "9" outranks "40" — pin that it does not."""
    provider, _ = _capture_queries(ranked_by_label={
        "Function": {"columns": ["fn.name", "fn.file_path", "in_degree"],
                     "rows": [["small", "src/a.py", "9"]]},
        "Method": {"columns": ["fn.name", "fn.file_path", "in_degree"],
                   "rows": [["big", "src/b.py", "40"]]},
    })
    out = MapGenerator(provider).generate("/repo")

    assert "`big`" in out and "`small`" in out
    assert out.index("`big`") < out.index("`small`"), "40 callers must outrank 9"


def test_ranked_symbols_survives_one_label_query_failing():
    """Never-raise: a backend that rejects one label still yields a ranking from the rest."""
    def _dispatch(method, payload, timeout_ms):
        q = str(payload.get("query", ""))
        if "(fn:Route)" in q:
            raise RuntimeError("backend does not know this label")
        if "(fn:Function)" in q:
            return {"columns": ["fn.name", "fn.file_path", "in_degree"],
                    "rows": [["survivor", "src/a.py", "5"]]}
        return []

    provider = MagicMock()
    provider.available = True
    provider._resolve_project.return_value = ProjectResolution(
        name="proj", matched_root="/repo", scope="exact")
    provider.build_result.return_value = {
        "ok": True, "op": "overview", "target": "", "result": "## Arch",
        "engine": "graph", "cached": False,
    }
    provider._run.side_effect = _dispatch

    out = MapGenerator(provider).generate("/repo")
    assert "`survivor`" in out
