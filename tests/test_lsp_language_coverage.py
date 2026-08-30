"""A language server that booted is not the same as one that will answer for this repository.

serena gets ONE config per project and it names a fixed list of language servers. On an evaluated
monorepo `.serena/project.yml` read `language_servers: [typescript]` while the tree held 69 Python
files under `services/*/src`, so every Python `symbol` query returned an empty body — and
`codeintel doctor --deep` reported the engine `ok / reached READY`, which was true about the process
and false about every answer it would give. Green while the thing it certifies serves nothing is the
worst shape a health check can take.
"""
from __future__ import annotations

import os

import pytest

from codeintel.providers.lsp import LspProvider


def _repo(tmp_path, langs: list[str], files: dict[str, int]):
    serena = tmp_path / ".serena"
    serena.mkdir()
    body = "project_name: t\nlanguage_servers:\n" + "".join(f"- {lang}\n" for lang in langs) + "encoding: utf-8\n"
    (serena / "project.yml").write_text(body, encoding="utf-8")
    for ext, n in files.items():
        d = tmp_path / "src"
        d.mkdir(exist_ok=True)
        for i in range(n):
            (d / f"f{i}{ext}").write_text("x", encoding="utf-8")
    return str(tmp_path)


def test_a_language_the_config_does_not_serve_is_named_with_its_weight(tmp_path):
    root = _repo(tmp_path, ["typescript"], {".py": 69, ".ts": 12})
    note = LspProvider()._unserved_note(root)
    assert note is not None
    detail, remediation = note
    assert "serves only typescript" in detail
    assert "python (69 files)" in detail
    # It must say what the SYMPTOM is, or a reader will look for an error that never appears.
    assert "empty" in detail and "not errors" in detail
    assert "language_servers:" in remediation


def test_a_fully_served_repo_reports_nothing(tmp_path):
    assert LspProvider()._unserved_note(_repo(tmp_path, ["python"], {".py": 40})) is None


def test_a_stray_file_is_not_a_language_the_repo_is_written_in(tmp_path):
    """One `setup.py` beside a TypeScript app must not turn the engine red — a warning that fires
    on ordinary repositories is one nobody reads."""
    root = _repo(tmp_path, ["typescript"], {".ts": 200, ".py": 1})
    assert LspProvider()._unserved_note(root) is None


def test_vendored_trees_do_not_count_toward_a_language(tmp_path):
    """`node_modules` holds more Python than most Python projects. Counting it would report every
    JS repo as an unserved-Python repo."""
    root = _repo(tmp_path, ["typescript"], {".ts": 30})
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    for i in range(50):
        (vendored / f"v{i}.py").write_text("x", encoding="utf-8")
    assert LspProvider()._unserved_note(root) is None


def test_no_serena_config_means_no_claim(tmp_path):
    """Absent config is not evidence of a gap — only the config is authoritative about what is
    served, so without one this check must stay silent rather than guess."""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert LspProvider()._unserved_note(str(tmp_path)) is None


def test_an_unserved_language_makes_the_deep_probe_not_runnable(tmp_path, monkeypatch):
    """The whole point: `runnable` is what the doctor's green tick reads, so the finding has to
    reach that field and not only the prose beside it."""
    root = _repo(tmp_path, ["typescript"], {".py": 69, ".ts": 12})
    p = LspProvider()
    if not p.available:
        pytest.skip("neither serena nor uvx on PATH")
    monkeypatch.setattr(p, "_unserved_note", lambda r: ("— unserved", "fix it"))

    class _Ready:
        state = __import__("codeintel.providers.lsp", fromlist=["_State"])._State.READY
        _lock = __import__("threading").Lock()

    monkeypatch.setattr(p, "_get_or_create_session", lambda root: _Ready())
    out = p.probe(root, deep=True, timeout_s=1.0)
    assert out["runnable"] is False, out
    assert out["remediation"] == "fix it"


def test_the_walk_survives_an_unreadable_tree(tmp_path):
    """Never-raise is the contract for everything the doctor calls; a permissions error inside the
    census must degrade to "no claim", not take the health check down with it."""
    root = _repo(tmp_path, ["typescript"], {".py": 10})
    os.chmod(tmp_path / "src", 0o000)
    try:
        LspProvider()._unserved_note(root)   # must not raise
    finally:
        os.chmod(tmp_path / "src", 0o755)  # noqa: S103
