"""Tests for `codeintel.lang_config` — repairing serena's `language_servers:` list.

Every case builds a throwaway `.serena/project.yml` under `tmp_path`, so nothing here touches a real
repository's config. That matters more than usual: this is the only module in the package that
**writes to a file the user owns**, and a half-written `project.yml` is worse than the defect it
fixes — serena would refuse to start, taking the LSP engine from answering one language to answering
none.

The two groups worth reading first:

* `test_a_c_census_is_written_as_cpp_because_serena_has_no_bare_c` and its neighbour pin the
  validation that stops a working repo being broken. serena's accepted-id list has `cpp` and
  `cpp_ccls` and **no bare `c`**, while codeintel's census language for `.c`/`.h` is `c`. Writing the
  census value straight through would emit `- c` and break serena's startup.
* the surgical-edit group pins that the ~35 lines of comments serena's init writes — including the
  accepted-id list and the warning that some servers need extra setup — survive the write. They are
  the most useful documentation that file has.
"""
from __future__ import annotations

import os

import pytest

from codeintel import lang_config

# A miniature of what serena's init actually writes: a comment above the key, one language, a blank
# line, then more comments and further keys. The shape is what the surgical edit has to survive.
_CONFIG = """\
project_name: "demo"

# list of language servers to start when using the LSP backend; choose from:
#   cpp   csharp   go   java   python   typescript
# Some language servers require additional setup/installations.
language_servers:
- python

# the encoding used by text files in the project
encoding: "utf-8"

# optional shell command to run before the language backend is initialised
initial_prompt: ""
"""


def _project(tmp_path, *, files: dict[str, int], config: str = _CONFIG):
    """Build a fake repo: a serena config plus `files` = {extension: how many}."""
    serena = tmp_path / ".serena"
    serena.mkdir(parents=True, exist_ok=True)
    (serena / "project.yml").write_text(config, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for ext, count in files.items():
        for index in range(count):
            (src / f"f{index}{ext}").write_text("x\n", encoding="utf-8")
    return str(tmp_path)


def _config_text(root: str) -> str:
    with open(os.path.join(root, ".serena", "project.yml"), encoding="utf-8") as fh:
        return fh.read()


# ── planning ──────────────────────────────────────────────────────────────────────────────────────

def test_a_missing_language_is_proposed_but_existing_order_is_kept(tmp_path):
    """The config's own comments say the FIRST entry is the default and the fallback, so reordering by
    file count could silently change which server answers for an ambiguous file."""
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    result = lang_config.plan(root)
    assert result["configured"] == ["python"]
    assert result["additions"] == ["typescript"]
    assert result["proposed"] == ["python", "typescript"]     # python stays first despite 40 > 10


def test_nothing_is_proposed_when_every_language_is_already_served(tmp_path):
    root = _project(tmp_path, files={".py": 10})
    result = lang_config.plan(root)
    assert result["additions"] == []
    assert result["proposed"] == ["python"]


def test_a_language_below_the_file_floor_is_recorded_but_not_proposed(tmp_path):
    """Every entry in this list is a language server serena will BOOT. One stray `.ts` file in a Python
    repo must not cost a whole TypeScript server's startup and memory on every session."""
    root = _project(tmp_path, files={".py": 10, ".ts": 2})
    result = lang_config.plan(root)
    assert result["additions"] == []
    assert result["below_floor"] == ["typescript"]


def test_the_floor_is_the_same_one_the_warning_uses(tmp_path):
    """Not an independent constant — it comes from `LspProvider._UNSERVED_FILE_FLOOR`, so the repair
    and the warning can never disagree about what counts as a language the repo is written in."""
    from codeintel.providers.lsp import LspProvider

    floor = int(LspProvider._UNSERVED_FILE_FLOOR)
    root = _project(tmp_path, files={".py": 10, ".ts": floor})
    assert lang_config.plan(root)["additions"] == ["typescript"]

    root2 = _project(tmp_path / "b", files={".py": 10, ".ts": floor - 1})
    assert lang_config.plan(root2)["additions"] == []


# ── the serena id vocabulary ──────────────────────────────────────────────────────────────────────

def test_a_c_census_is_written_as_cpp_because_serena_has_no_bare_c(tmp_path):
    """The validation that stops a working repo being broken.

    codeintel's census language for `.c`/`.h` is `c`; serena's accepted ids are `cpp` and `cpp_ccls`
    with no bare `c`. Passing the census value through would write `- c` and break serena's startup.
    """
    root = _project(tmp_path, files={".py": 10, ".c": 12})
    result = lang_config.plan(root)
    assert result["additions"] == ["cpp"]
    assert "c" not in result["proposed"]


def test_c_and_cpp_together_produce_one_cpp_entry(tmp_path):
    """Both census languages map onto the same server id, and a duplicated entry is a broken list."""
    root = _project(tmp_path, files={".py": 10, ".c": 8, ".cpp": 9})
    result = lang_config.plan(root)
    assert result["additions"] == ["cpp"]
    assert result["proposed"].count("cpp") == 1


def test_an_unmappable_language_is_reported_rather_than_guessed_at(tmp_path):
    """serena's own comment says its id list "may be outdated", so drift is expected and has to fail
    closed: the cost of a missing map entry is a language we do not offer to configure, never a config
    we corrupt."""
    root = _project(tmp_path, files={".py": 10, ".ts": 20})
    saved = dict(lang_config._SERENA_ID)
    try:
        lang_config._SERENA_ID.pop("typescript")
        result = lang_config.plan(root)
        assert result["additions"] == []
        assert result["unmappable"] == ["typescript"]
        assert result["proposed"] == ["python"]
    finally:
        lang_config._SERENA_ID.clear()
        lang_config._SERENA_ID.update(saved)


# ── refusals ──────────────────────────────────────────────────────────────────────────────────────

def test_a_repo_with_no_serena_config_is_reported_not_scaffolded(tmp_path):
    """No config means serena was never initialised here. Writing a bare one would skip the
    scaffolding and the explanatory comments serena's own init produces."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x\n", encoding="utf-8")
    result = lang_config.apply_plan(str(tmp_path), apply=True)
    assert result["problem"] == "no-serena-config"
    assert result["applied"] is False
    assert not (tmp_path / ".serena").exists()


def test_a_config_without_the_key_is_refused_rather_than_appended_to(tmp_path):
    """Editing a file we could not parse is how a config gets corrupted."""
    root = _project(tmp_path, files={".py": 10},
                    config='project_name: "demo"\nencoding: "utf-8"\n')
    before = _config_text(root)
    result = lang_config.apply_plan(root, apply=True)
    assert result["problem"] == "no-language-servers-key"
    assert result["applied"] is False
    assert _config_text(root) == before


# ── writing ───────────────────────────────────────────────────────────────────────────────────────

def test_a_dry_run_changes_nothing(tmp_path):
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    before = _config_text(root)
    result = lang_config.apply_plan(root, apply=False)
    assert result["additions"] == ["typescript"]
    assert result["applied"] is False
    assert _config_text(root) == before


def test_applying_adds_the_entry_and_preserves_every_comment_and_key(tmp_path):
    """A YAML round trip would discard serena's own comments — the accepted-id list and the note that
    some servers need extra setup. Those are the most useful documentation the file has, so the edit is
    surgical rather than a reserialisation."""
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    result = lang_config.apply_plan(root, apply=True)
    assert result["applied"] is True

    after = _config_text(root)
    assert "- python\n- typescript\n" in after
    for kept in ("# list of language servers to start when using the LSP backend",
                 "# Some language servers require additional setup/installations.",
                 "# the encoding used by text files in the project",
                 'encoding: "utf-8"',
                 'initial_prompt: ""',
                 'project_name: "demo"'):
        assert kept in after, kept


def test_the_write_is_verified_by_re_reading_through_the_providers_own_parser(tmp_path):
    """The one operation here that can leave a repo worse than it started, so success is confirmed
    rather than assumed."""
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    result = lang_config.apply_plan(root, apply=True)
    assert result["verified"] is True
    assert result["configured_after"] == ["python", "typescript"]


def test_applying_twice_is_a_no_op(tmp_path):
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    lang_config.apply_plan(root, apply=True)
    first = _config_text(root)

    second_result = lang_config.apply_plan(root, apply=True)
    assert second_result["additions"] == []
    assert second_result["applied"] is False
    assert _config_text(root) == first


def test_no_temporary_file_is_left_behind(tmp_path):
    """A stray `.project.yml.*.tmp` in the user's `.serena/` is litter that looks like a broken
    config."""
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    lang_config.apply_plan(root, apply=True)
    leftovers = [n for n in os.listdir(os.path.join(root, ".serena")) if n.endswith(".tmp")]
    assert leftovers == []


def test_the_edited_file_is_still_valid_yaml(tmp_path):
    """An independent check: the surgical edit is hand-rolled text manipulation, so something other
    than this package's own hand-rolled parser has to confirm the result.

    Skipped rather than vendored where pyyaml is absent — codeintel does not depend on it, and adding
    a dependency to test a file it writes four words into would be the wrong trade.
    """
    yaml = pytest.importorskip("yaml", reason="pyyaml is not a codeintel dependency")
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    lang_config.apply_plan(root, apply=True)
    parsed = yaml.safe_load(_config_text(root))
    assert parsed["language_servers"] == ["python", "typescript"]
    assert parsed["encoding"] == "utf-8"
    assert parsed["project_name"] == "demo"


# ── reporting ─────────────────────────────────────────────────────────────────────────────────────

def test_describe_names_the_flag_when_it_has_not_applied(tmp_path):
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    text = lang_config.describe(lang_config.apply_plan(root, apply=False))
    assert "would add" in text and "--languages" in text


def test_describe_reports_the_verification_result_when_it_has_applied(tmp_path):
    root = _project(tmp_path, files={".py": 10, ".ts": 40})
    text = lang_config.describe(lang_config.apply_plan(root, apply=True))
    assert "added" in text and "verified" in text


def test_describe_turns_each_problem_into_a_sentence(tmp_path):
    (tmp_path / "src").mkdir()
    text = lang_config.describe(lang_config.plan(str(tmp_path)))
    assert "no .serena/project.yml" in text
