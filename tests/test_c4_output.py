"""Tests for the output-isolation half of `codeintel.c4` (`plan_output`/`write_model`, the
fact-4 guarantee) and for `codeintel.commands.c4.run` — the CLI wiring on top of it.
"""
from __future__ import annotations

import argparse
import json
from importlib import import_module

import pytest

from codeintel import c4


def _args(defaults: dict | None = None, **overrides) -> argparse.Namespace:
    return argparse.Namespace(**{**(defaults or {}), **overrides})


def _c4_args(**kw):
    return _args({"project_root": None, "out": None, "depth": None, "scope": None,
                 "include_tests": False, "no_index": False, "json": False}, **kw)


def _valid_payload(*, depth=1, table=None):
    return {
        "project": "demo", "engine": "graph", "op": "c4",
        "fit": {"depth": depth, "how": "auto-fit", "table": table or {depth: 1},
               "over_cap": False, "cap": 100},
        "elements": [{"id": "a", "path": "a.py", "title": "a", "kind": "module", "tech": "Python",
                     "files": 1, "churn": 0, "fan_in": 0, "fan_out": 0, "internal_imports": 0}],
        "relations": [], "dropped": [], "stats": dict(c4._EMPTY_STATS), "reason": "",
    }


def _stub_payload(*, depth, how, table, elements_n, relations_n, over_cap=False, cap=100):
    elements = [{"id": f"e{i}", "path": f"e{i}.py", "title": f"e{i}", "kind": "module",
                "tech": "Python", "files": 1, "churn": 0, "fan_in": 0, "fan_out": 0,
                "internal_imports": 0} for i in range(elements_n)]
    relations = ([{"from": "e0", "to": "e1", "n": 1} for _ in range(relations_n)]
                if elements_n >= 2 else [])
    return {
        "project": "demo", "engine": "graph", "op": "c4",
        "fit": {"depth": depth, "how": how, "table": table, "over_cap": over_cap, "cap": cap},
        "elements": elements, "relations": relations, "dropped": [],
        "stats": dict(c4._EMPTY_STATS), "reason": "",
    }


# --------------------------------------------------------------------------- plan_output / write_model

def test_writing_beside_a_foreign_c4_file_is_refused(tmp_path):
    other = tmp_path / "other.c4"
    other.write_text("model {}\n")
    original = other.read_bytes()

    plan = c4.plan_output(str(tmp_path))
    assert plan["ok"] is False

    result = c4.write_model(_valid_payload(), "model {}\n", str(tmp_path))
    assert result["ok"] is False
    assert other.read_bytes() == original


def test_a_rerun_overwrites_only_the_model_it_owns(tmp_path):
    payload = _valid_payload()
    r1 = c4.write_model(payload, "model {} // v1\n", str(tmp_path))
    assert r1["ok"] is True

    plan = c4.plan_output(str(tmp_path))
    assert plan["action"] == "overwrite"

    r2 = c4.write_model(payload, "model {} // v2\n", str(tmp_path))
    assert r2["ok"] is True
    assert (tmp_path / c4.MODEL_FILENAME).read_text() == "model {} // v2\n"


def test_an_empty_directory_is_adopted_and_marked(tmp_path):
    plan = c4.plan_output(str(tmp_path))
    assert plan["action"] == "adopt"

    result = c4.write_model(_valid_payload(depth=3), "model {}\n", str(tmp_path))
    assert result["ok"] is True
    assert (tmp_path / c4.MODEL_FILENAME).exists()

    marker = json.loads((tmp_path / c4.MARKER_FILENAME).read_text())
    assert marker["files"] == [c4.MODEL_FILENAME]
    assert marker["depth"] == 3


def test_a_stale_file_from_a_previous_run_is_removed(tmp_path):
    stale = tmp_path / "stale.c4"
    stale.write_text("stale\n")
    marker = {"generator": "codeintel", "files": [c4.MODEL_FILENAME, "stale.c4"]}
    (tmp_path / c4.MARKER_FILENAME).write_text(json.dumps(marker))

    result = c4.write_model(_valid_payload(), "model {}\n", str(tmp_path))
    assert result["ok"] is True
    assert not stale.exists()
    assert (tmp_path / c4.MODEL_FILENAME).exists()


def test_an_out_path_that_is_a_file_is_refused(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("hello")

    result = c4.write_model(_valid_payload(), "model {}\n", str(readme))
    assert result["ok"] is False
    assert readme.read_text() == "hello"


# --------------------------------------------------------------------------- commands/c4.py

def test_c4_writes_nothing_when_the_model_would_be_empty(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("codeintel.c4.build_c4_payload",
                        lambda root, **kw: {**c4._EMPTY, "reason": "no-source-files"})
    out_dir = tmp_path / "codeintel-c4"
    args = _c4_args(project_root=str(tmp_path), out=str(out_dir))

    assert import_module("codeintel.commands.c4").run(args) == 1
    assert not out_dir.exists()
    assert "no-source-files" in capsys.readouterr().out


def _no_wait(monkeypatch, cmd, *, step=None):
    """Make the settle loop deterministic and instant.

    Stubbing only `sleep` is not enough: with a real `monotonic`, the loop busy-spins for the whole
    SETTLE_SECONDS budget in wall-clock time. The clock has to advance too.
    """
    step = cmd.SETTLE_INTERVAL_SECONDS if step is None else step
    now = {"t": 0.0}
    monkeypatch.setattr(cmd.time, "sleep", lambda s: now.__setitem__("t", now["t"] + step))
    monkeypatch.setattr(cmd.time, "monotonic", lambda: now["t"])


def _payload_sequence(monkeypatch, *payloads):
    """Stub `build_c4_payload` to return each payload in turn, and record how many times it ran."""
    calls = {"n": 0}

    def _build(root, **kw):
        i = calls["n"]
        calls["n"] += 1
        return payloads[min(i, len(payloads) - 1)]

    monkeypatch.setattr("codeintel.c4.build_c4_payload", _build)
    return calls


def test_an_unindexed_repo_is_indexed_and_the_model_produced_in_one_command(
        monkeypatch, capsys, tmp_path):
    """The point of the feature: `codeintel c4` on a never-indexed repo yields a MODEL, not an
    instruction to go run a second command."""
    calls = _payload_sequence(monkeypatch,
                              {**c4._EMPTY, "reason": "project-not-indexed"},
                              _valid_payload())
    indexed: list[str] = []
    monkeypatch.setattr("codeintel.c4.index_repo",
                        lambda root, **kw: (indexed.append(str(root)), {"ok": True, "problem": ""})[1])
    monkeypatch.setattr("codeintel.c4.render_c4_dsl", lambda p: "model {}\n")
    _no_wait(monkeypatch, import_module("codeintel.commands.c4"))
    out_dir = tmp_path / "codeintel-c4"

    assert import_module("codeintel.commands.c4").run(
        _c4_args(project_root=str(tmp_path), out=str(out_dir))) == 0
    assert indexed == [str(tmp_path)]          # it indexed THIS repo
    assert calls["n"] == 2                     # and rebuilt from the fresh index
    assert (out_dir / c4.MODEL_FILENAME).exists()
    out = capsys.readouterr().out
    assert "no graph index yet" in out         # announced before the slow part, never silent


def test_the_nested_repo_reason_also_auto_indexes(monkeypatch, capsys, tmp_path):
    """`project-not-indexed-standalone` is fixed by indexing this repo directly — the same
    remedy, so it takes the same path rather than printing advice."""
    _payload_sequence(monkeypatch,
                      {**c4._EMPTY, "reason": "project-not-indexed-standalone"},
                      _valid_payload())
    monkeypatch.setattr("codeintel.c4.index_repo", lambda root, **kw: {"ok": True, "problem": ""})
    monkeypatch.setattr("codeintel.c4.render_c4_dsl", lambda p: "model {}\n")
    _no_wait(monkeypatch, import_module("codeintel.commands.c4"))

    assert import_module("codeintel.commands.c4").run(
        _c4_args(project_root=str(tmp_path), out=str(tmp_path / "o"))) == 0


def test_no_index_refuses_instead_of_indexing(monkeypatch, capsys, tmp_path):
    """CI needs "the index was missing" to fail the step, not be quietly repaired."""
    calls = _payload_sequence(monkeypatch, {**c4._EMPTY, "reason": "project-not-indexed"})
    monkeypatch.setattr("codeintel.c4.index_repo",
                        lambda root, **kw: pytest.fail("--no-index must not index"))

    assert import_module("codeintel.commands.c4").run(
        _c4_args(project_root=str(tmp_path), no_index=True)) == 1
    assert calls["n"] == 1                     # built once, never retried
    assert "project-not-indexed" in capsys.readouterr().out


def test_a_project_the_backend_publishes_late_is_waited_for_not_declared_missing(
        monkeypatch, capsys, tmp_path):
    """The backend returns from `index_repository` before the new project is queryable. Measured on
    a 246-file repo: the index reported success, the immediate rebuild still resolved to nothing,
    and the next command invocation produced a 62-element model from that same index. A single
    retry printed "✓ indexed" and "project-not-indexed — run `codeintel index`" one line apart.
    """
    cmd = import_module("codeintel.commands.c4")
    # not-indexed, still not-indexed, THEN it appears — exactly the race, no real sleeping
    calls = _payload_sequence(monkeypatch,
                              {**c4._EMPTY, "reason": "project-not-indexed"},
                              {**c4._EMPTY, "reason": "project-not-indexed"},
                              _valid_payload())
    monkeypatch.setattr("codeintel.c4.index_repo", lambda root, **kw: {"ok": True, "problem": ""})
    monkeypatch.setattr("codeintel.c4.render_c4_dsl", lambda p: "model {}\n")
    _no_wait(monkeypatch, cmd)
    out_dir = tmp_path / "o"

    assert cmd.run(_c4_args(project_root=str(tmp_path), out=str(out_dir))) == 0
    assert calls["n"] == 3                       # it kept asking rather than giving up at 2
    assert (out_dir / c4.MODEL_FILENAME).exists()


def test_a_project_that_never_appears_does_not_tell_you_to_index_again(
        monkeypatch, capsys, tmp_path):
    """The old message advised the exact action that had just succeeded. Bounded wait, then an
    accurate reason — and no advice to re-run the step that already worked."""
    cmd = import_module("codeintel.commands.c4")
    _payload_sequence(monkeypatch, {**c4._EMPTY, "reason": "project-not-indexed"})
    monkeypatch.setattr("codeintel.c4.index_repo", lambda root, **kw: {"ok": True, "problem": ""})
    _no_wait(monkeypatch, cmd)

    assert cmd.run(_c4_args(project_root=str(tmp_path))) == 1
    out = capsys.readouterr().out
    assert "still reports no project" in out
    assert "drop --no-index" not in out          # the advice that made no sense here
    assert "run `codeintel index`" not in out


def test_the_settle_wait_is_bounded_by_the_clock_not_the_attempt_count(monkeypatch):
    """A backend that never publishes must not spin forever: the loop is bounded by wall clock, so
    it cannot hang a CI step or this test suite."""
    cmd = import_module("codeintel.commands.c4")
    ticks = iter([0.0] + [cmd.SETTLE_SECONDS + 1.0] * 50)   # first check inside, then past deadline
    monkeypatch.setattr(cmd.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cmd.time, "sleep", lambda s: None)
    builds = {"n": 0}

    def build():
        builds["n"] += 1
        return {**c4._EMPTY, "reason": "project-not-indexed"}

    out = cmd._settle(build, {**c4._EMPTY, "reason": "project-not-indexed"})
    assert out["reason"] == "project-not-indexed"
    assert builds["n"] == 0                      # deadline already passed → no extra attempt


def test_the_settle_helper_degrades_instead_of_raising(monkeypatch):
    cmd = import_module("codeintel.commands.c4")
    original = {**c4._EMPTY, "reason": "project-not-indexed"}
    monkeypatch.setattr(cmd.time, "sleep", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cmd._settle(lambda: _valid_payload(), original) is original


def test_a_failed_auto_index_reports_the_backend_problem_and_does_not_loop(
        monkeypatch, capsys, tmp_path):
    calls = _payload_sequence(monkeypatch, {**c4._EMPTY, "reason": "project-not-indexed"})
    monkeypatch.setattr("codeintel.c4.index_repo",
                        lambda root, **kw: {"ok": False, "problem": "worker crashed"})

    assert import_module("codeintel.commands.c4").run(
        _c4_args(project_root=str(tmp_path))) == 1
    assert calls["n"] == 1                     # no retry after a failed index
    assert "worker crashed" in capsys.readouterr().out


def test_a_reason_that_indexing_cannot_fix_is_not_retried(monkeypatch, capsys, tmp_path):
    """`no-source-files` means the index was READ and holds no source — re-indexing would spend
    minutes to rebuild the identical empty model."""
    calls = _payload_sequence(monkeypatch, {**c4._EMPTY, "reason": "no-source-files"})
    monkeypatch.setattr("codeintel.c4.index_repo",
                        lambda root, **kw: pytest.fail("no-source-files must not trigger an index"))

    assert import_module("codeintel.commands.c4").run(
        _c4_args(project_root=str(tmp_path))) == 1
    assert calls["n"] == 1


def test_json_reports_the_model_from_after_the_auto_index(monkeypatch, capsys, tmp_path):
    """--json must not print the not-indexed envelope the repo had a moment ago."""
    _payload_sequence(monkeypatch,
                      {**c4._EMPTY, "reason": "project-not-indexed"},
                      _valid_payload())
    monkeypatch.setattr("codeintel.c4.index_repo", lambda root, **kw: {"ok": True, "problem": ""})
    _no_wait(monkeypatch, import_module("codeintel.commands.c4"))

    assert import_module("codeintel.commands.c4").run(
        _c4_args(project_root=str(tmp_path), json=True)) == 0
    printed = capsys.readouterr().out
    payload = json.loads(printed[printed.index("{"):])
    assert payload["reason"] == ""
    assert payload["elements"]


def test_index_repo_never_raises_and_reports_an_unavailable_engine(monkeypatch):
    class _Unavailable:
        available = False

    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", _Unavailable)
    result = c4.index_repo("/nope")
    assert result["ok"] is False and "unavailable" in result["problem"]


def test_index_repo_surfaces_a_backend_error_rather_than_claiming_success(monkeypatch):
    """Swallowing a backend error is what let a broken reindex look exactly like a working one."""
    class _Erroring:
        available = True

        def _run(self, method, payload, timeout_ms):
            assert method == "index_repository"
            assert "repo_path" in payload      # NOT project_root — the backend rejects that name
            return {"status": "error", "hint": "repo_path is required"}

    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", _Erroring)
    result = c4.index_repo("/x")
    assert result["ok"] is False and "repo_path is required" in result["problem"]


def test_index_repo_treats_no_response_as_a_failure(monkeypatch):
    class _Silent:
        available = True

        def _run(self, method, payload, timeout_ms):
            return None

    monkeypatch.setattr("codeintel.providers.graph.GraphProvider", _Silent)
    assert c4.index_repo("/x")["ok"] is False


def test_c4_degrades_instead_of_tracebacking(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("codeintel.c4.build_c4_payload",
                        lambda root, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    args = _c4_args(project_root=str(tmp_path))

    assert import_module("codeintel.commands.c4").run(args) == 1
    assert "c4 failed: boom" in capsys.readouterr().out


def test_c4_reports_the_chosen_depth_on_stdout(monkeypatch, capsys, tmp_path):
    payload = _stub_payload(depth=4, how="auto-fit", table={1: 6, 2: 11, 3: 15, 4: 22},
                            elements_n=22, relations_n=10)
    monkeypatch.setattr("codeintel.c4.build_c4_payload", lambda root, **kw: payload)
    monkeypatch.setattr("codeintel.c4.render_c4_dsl", lambda p: "model {}\n")
    out_dir = tmp_path / "codeintel-c4"
    args = _c4_args(project_root=str(tmp_path), out=str(out_dir))

    assert import_module("codeintel.commands.c4").run(args) == 0
    out = capsys.readouterr().out
    assert "depth 4" in out
    assert "22 elements" in out
    assert "auto-fit" in out
    assert "10 relations" in out


def test_c4_warns_when_the_model_exceeds_the_view_cap(monkeypatch, capsys, tmp_path):
    payload = _stub_payload(depth=1, how="auto-fit", table={1: 140}, elements_n=140,
                            relations_n=0, over_cap=True, cap=100)
    monkeypatch.setattr("codeintel.c4.build_c4_payload", lambda root, **kw: payload)
    monkeypatch.setattr("codeintel.c4.render_c4_dsl", lambda p: "model {}\n")
    out_dir = tmp_path / "codeintel-c4"
    args = _c4_args(project_root=str(tmp_path), out=str(out_dir))

    assert import_module("codeintel.commands.c4").run(args) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "100" in out
    assert (out_dir / c4.MODEL_FILENAME).exists()


def test_c4_json_emits_the_payload_and_writes_no_file(monkeypatch, capsys, tmp_path):
    payload = _stub_payload(depth=2, how="auto-fit", table={1: 3, 2: 5}, elements_n=5,
                            relations_n=2)
    monkeypatch.setattr("codeintel.c4.build_c4_payload", lambda root, **kw: payload)
    out_dir = tmp_path / "codeintel-c4"
    args = _c4_args(project_root=str(tmp_path), out=str(out_dir), json=True)

    assert import_module("codeintel.commands.c4").run(args) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["fit"]["depth"] == 2
    assert not out_dir.exists()


def test_c4_reports_other_c4_files_elsewhere_in_the_repo(monkeypatch, capsys, tmp_path):
    (tmp_path / "docs" / "arch").mkdir(parents=True)
    (tmp_path / "docs" / "arch" / "legacy.c4").write_text("model {}\n")
    payload = _stub_payload(depth=1, how="auto-fit", table={1: 1}, elements_n=1, relations_n=0)
    monkeypatch.setattr("codeintel.c4.build_c4_payload", lambda root, **kw: payload)
    monkeypatch.setattr("codeintel.c4.render_c4_dsl", lambda p: "model {}\n")
    out_dir = tmp_path / "codeintel-c4"
    args = _c4_args(project_root=str(tmp_path), out=str(out_dir))

    assert import_module("codeintel.commands.c4").run(args) == 0
    out = capsys.readouterr().out
    assert "legacy.c4" in out or "docs/arch" in out
    assert f"likec4 start {out_dir}" in out
