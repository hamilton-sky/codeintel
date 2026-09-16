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


# ── The other half of "will it answer for this repo's code?" ──────────────────────────────────
#
# The tests above cover a language the config never names. These cover the quieter shape: the
# language IS served and the server IS ready, but `tsserver` has no project to resolve in, so it
# returns each definition and an EMPTY reference list. That empty list reaches a caller as
# `## References (0)` at `confidence: complete` — a confident "nothing references this" about the
# question asked just before deleting code. Reproduced on `bench/fixtures/corpus_ts`, where the
# same query returned 0 references with `doctor --deep` reporting `3 / 3 engines ready`, and 17
# references once a plain tsconfig was dropped in.


def test_typescript_without_a_project_is_named_with_its_weight(tmp_path):
    root = _repo(tmp_path, ["typescript"], {".ts": 19})
    note = LspProvider()._no_tsproject_note(root)
    assert note is not None
    detail, remediation = note
    assert "19 TypeScript files" in detail
    # The symptom has to be spelled out. "No tsconfig" alone reads as a lint nit; "references come
    # back empty and that is not the same as none" is the fact that changes what a reader does.
    assert "EMPTY" in detail and "nothing references this" in detail
    assert "tsconfig.json" in remediation


def test_a_tsconfig_settles_it(tmp_path):
    root = _repo(tmp_path, ["typescript"], {".ts": 19})
    (tmp_path / "tsconfig.json").write_text('{"include": ["src/**/*.ts"]}', encoding="utf-8")
    assert LspProvider()._no_tsproject_note(root) is None


def test_a_project_file_anywhere_is_enough(tmp_path):
    """A monorepo keeps tsconfigs per package. Calling such a repo unconfigured because the ROOT
    has none would be the same false confidence this check exists to catch, aimed the other way."""
    root = _repo(tmp_path, ["typescript"], {".ts": 19})
    pkg = tmp_path / "packages" / "app"
    pkg.mkdir(parents=True)
    (pkg / "tsconfig.build.json").write_text("{}", encoding="utf-8")
    assert LspProvider()._no_tsproject_note(root) is None


def test_a_handful_of_loose_files_is_not_a_typescript_project(tmp_path):
    assert LspProvider()._no_tsproject_note(_repo(tmp_path, ["typescript"], {".ts": 2})) is None


def test_plain_javascript_is_not_charged_for_a_missing_tsconfig(tmp_path):
    """`.js` without a jsconfig is how most JavaScript repositories look, and a warning that fires
    on ordinary repositories is one nobody reads. Only `.ts`/`.tsx` count."""
    assert LspProvider()._no_tsproject_note(_repo(tmp_path, ["typescript"], {".js": 40})) is None


def test_an_unserved_typescript_repo_is_not_double_reported(tmp_path):
    """When the config does not serve typescript at all, `_unserved_note` already says so and says
    something more useful. Two findings about one cause is how a remediation gets ignored."""
    root = _repo(tmp_path, ["python"], {".ts": 40})
    assert LspProvider()._no_tsproject_note(root) is None


def test_a_missing_typescript_project_makes_the_deep_probe_not_runnable(tmp_path, monkeypatch):
    """`runnable` is what the doctor's green tick reads, so this has to reach that field."""
    root = _repo(tmp_path, ["typescript"], {".ts": 19})
    p = LspProvider()
    if not p.available:
        pytest.skip("neither serena nor uvx on PATH")

    class _Ready:
        state = __import__("codeintel.providers.lsp", fromlist=["_State"])._State.READY
        _lock = __import__("threading").Lock()

    monkeypatch.setattr(p, "_get_or_create_session", lambda root: _Ready())
    out = p.probe(root, deep=True, timeout_s=1.0)
    assert out["runnable"] is False, out
    assert "tsconfig.json" in (out["remediation"] or "")


def test_the_tsproject_walk_survives_an_unreadable_tree(tmp_path):
    """Never-raise is the contract for everything the doctor calls."""
    root = _repo(tmp_path, ["typescript"], {".ts": 10})
    os.chmod(tmp_path / "src", 0o000)
    try:
        LspProvider()._no_tsproject_note(root)   # must not raise
    finally:
        os.chmod(tmp_path / "src", 0o755)  # noqa: S103


# ── The same finding, on the path that actually reaches a caller ──────────────────────────────
#
# `doctor` is advisory and an agent need never run it. `_empty_references_unsound` is the half that
# reaches whoever asked: it decides whether an EMPTY reference list is the answer "nothing
# references this" or the non-answer "this backend was never in a position to tell you".


def test_an_empty_reference_list_with_no_tsproject_is_unknown_not_none(tmp_path):
    root = _repo(tmp_path, ["typescript"], {".ts": 19})
    missing = LspProvider()._empty_references_unsound(root, "src/f1.ts")
    assert missing is not None
    assert missing.kind == "unresolvable"
    # It has to deny the reading that makes the emptiness dangerous, not merely mention tsconfig.
    assert "UNKNOWN rather than none" in missing.describe()
    assert "tsconfig.json" in missing.describe()


def test_an_empty_reference_list_with_a_tsproject_is_a_real_answer(tmp_path):
    root = _repo(tmp_path, ["typescript"], {".ts": 19})
    (tmp_path / "tsconfig.json").write_text('{"include": ["src/**/*.ts"]}', encoding="utf-8")
    assert LspProvider()._empty_references_unsound(root, "src/f1.ts") is None


def test_doubt_is_scoped_to_the_language_that_could_not_resolve(tmp_path):
    """A polyglot tree with loose TypeScript and no tsconfig must not cast doubt on a PYTHON answer.

    The unsound emptiness belongs to the language whose server could not resolve, not to the
    repository. Scoping this to the repo would attach a scary gap to answers that were resolved
    perfectly well — and a gap that appears on correct answers is one nobody reads."""
    root = _repo(tmp_path, ["typescript", "python"], {".ts": 19, ".py": 20})
    assert LspProvider()._empty_references_unsound(root, "src/thing.py") is None
    assert LspProvider()._empty_references_unsound(root, "src/thing.ts") is not None


def test_an_unserved_typescript_repo_raises_no_reference_doubt(tmp_path):
    root = _repo(tmp_path, ["python"], {".ts": 40})
    assert LspProvider()._empty_references_unsound(root, "src/f1.ts") is None


def test_the_unsound_check_survives_an_unreadable_tree(tmp_path):
    root = _repo(tmp_path, ["typescript"], {".ts": 10})
    os.chmod(tmp_path / "src", 0o000)
    try:
        LspProvider()._empty_references_unsound(root, "src/f1.ts")   # must not raise
    finally:
        os.chmod(tmp_path / "src", 0o755)  # noqa: S103
