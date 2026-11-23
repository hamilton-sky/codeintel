"""Story: a user cloning an untrusted repo.

`codeintel map`, `index`, `code.map` and `map --inject` never write outside the repo through a
symlink, and never write codeintel's own CODE_INTEL.md through a link at all. The CLI is run as a
real subprocess (`python -m codeintel`), the way a user runs it; `code.map` through its MCP handler.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from codeintel.transports.mcp import code_map_handler

ORIGINAL = "ORIGINAL victim content\n"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    e = dict(os.environ)
    e.update({
        "HOME": str(home),
        "CODEINTEL_HOME": str(home / ".codeintel"),
        "CODEBASE_MEMORY_HOME": str(tmp_path / "cbm-cache"),
        "CODEINTEL_REINDEX": "off",
        # A blocked model download, failing fast: the index pass is not what is under test.
        "HF_HUB_OFFLINE": "1",
        "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed-empty"),
        "NO_COLOR": "1",
    })
    # No graph backend: `map` writes its stub, which is exactly the content that went through the
    # link in the finding.
    e["PATH"] = os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin"
    return e


def _repo(tmp_path) -> str:
    root = tmp_path / "mal"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "m.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
    return str(root)


def _victim(tmp_path, name: str = "victim.txt") -> str:
    d = tmp_path / "victimdir"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(ORIGINAL)
    return str(p)


def _cli(env: dict, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "codeintel", *args], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------------- CODE_INTEL.md

# Spec: When `CODE_INTEL.md` is a symlink, wherever it points (outside the repo ...), `codeintel
# map` ... write[s] nothing through it. The file it points at is unchanged, and the command reports
# the refusal (`map` exits 1 and names the path).
def test_map_refuses_a_code_intel_md_symlink_pointing_outside_the_repo(tmp_path, env):
    root = _repo(tmp_path)
    victim = _victim(tmp_path)
    os.symlink(victim, os.path.join(root, "CODE_INTEL.md"))
    out = _cli(env, "map", root)
    assert _read(victim) == ORIGINAL, "codeintel map wrote through the symlink"
    assert out.returncode == 1, out.stdout + out.stderr
    assert "CODE_INTEL.md" in out.stdout + out.stderr
    assert os.path.islink(os.path.join(root, "CODE_INTEL.md"))


# Spec: When `CODE_INTEL.md` is a symlink, wherever it points (... inside it ...), `codeintel map`
# ... write[s] nothing through it ... (`map` exits 1 and names the path).
# Edge case: `CODE_INTEL.md` symlinked to a file inside the repo -> refused: codeintel's own file is
# never written through a link.
def test_map_refuses_a_code_intel_md_symlink_pointing_inside_the_repo(tmp_path, env):
    root = _repo(tmp_path)
    os.makedirs(os.path.join(root, "docs"))
    inside = os.path.join(root, "docs", "notes.md")
    with open(inside, "w", encoding="utf-8") as fh:
        fh.write(ORIGINAL)
    os.symlink(os.path.join("docs", "notes.md"), os.path.join(root, "CODE_INTEL.md"))
    out = _cli(env, "map", root)
    assert _read(inside) == ORIGINAL, "codeintel map wrote through the symlink"
    assert out.returncode == 1, out.stdout + out.stderr
    assert "CODE_INTEL.md" in out.stdout + out.stderr


# Spec: When `CODE_INTEL.md` is a symlink, wherever it points (... or nowhere), `codeintel map` ...
# write[s] nothing through it ... (`map` exits 1 and names the path).
# Edge case: `CODE_INTEL.md` is a dangling symlink -> refused, never created through the link.
def test_map_refuses_a_dangling_code_intel_md_symlink(tmp_path, env):
    root = _repo(tmp_path)
    nowhere = str(tmp_path / "victimdir" / "created-by-map.txt")
    os.symlink(nowhere, os.path.join(root, "CODE_INTEL.md"))
    out = _cli(env, "map", root)
    assert not os.path.exists(nowhere), "codeintel map created a file through a dangling link"
    assert out.returncode == 1, out.stdout + out.stderr
    assert "CODE_INTEL.md" in out.stdout + out.stderr


# Spec: When `CODE_INTEL.md` is a symlink, wherever it points ..., ... `codeintel index` ... write[s]
# nothing through it. The file it points at is unchanged, and the command reports the refusal.
def test_index_does_not_refresh_a_code_intel_md_symlink(tmp_path, env):
    root = _repo(tmp_path)
    victim = _victim(tmp_path)
    os.symlink(victim, os.path.join(root, "CODE_INTEL.md"))
    out = _cli(env, "index", root)
    assert _read(victim) == ORIGINAL, "codeintel index wrote through the symlink"
    assert "CODE_INTEL.md" in out.stdout + out.stderr


# Spec: When `CODE_INTEL.md` is a symlink, wherever it points ..., ... `code.map` write[s] nothing
# through it. The file it points at is unchanged, and the command reports the refusal.
def test_code_map_refuses_a_code_intel_md_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(tmp_path / "cbm-cache"))
    monkeypatch.setattr("codeintel.engines.graph.provider.shutil.which", lambda x: None)
    root = _repo(tmp_path)
    victim = _victim(tmp_path)
    os.symlink(victim, os.path.join(root, "CODE_INTEL.md"))
    res = code_map_handler({"project_root": root})
    assert _read(victim) == ORIGINAL, "code.map wrote through the symlink"
    assert not res.get("wrote"), res
    assert "CODE_INTEL.md" in str(res), res


# ------------------------------------------------------------------------- AGENTS.md / CLAUDE.md

@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "USING_CODEINTEL.md"])
# Spec: When `AGENTS.md`, `CLAUDE.md` or `USING_CODEINTEL.md` ... is a symlink that resolves outside
# the repo root ..., `map --inject` ... write[s] nothing through it, the target is unchanged, and the
# refusal is reported.
def test_map_inject_refuses_a_context_file_symlink_that_resolves_outside_the_repo(
        tmp_path, env, name):
    root = _repo(tmp_path)
    victim = _victim(tmp_path)
    if name == "USING_CODEINTEL.md":
        with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write("# rules\n")
    os.symlink(victim, os.path.join(root, name))
    out = _cli(env, "map", "--inject", root)
    assert _read(victim) == ORIGINAL, f"map --inject wrote through the {name} symlink"
    assert name in out.stdout + out.stderr, out.stdout + out.stderr


@pytest.mark.parametrize("git_target", [".git/config", ".git/hooks/pre-commit"])
# Spec: When `AGENTS.md` ... is a symlink that resolves ... into the repo's `.git` directory
# (`.git/config`, `.git/hooks/…`), `map --inject` ... write[s] nothing through it, the target is
# unchanged, and the refusal is reported.
# Edge case: `AGENTS.md` symlinked to `.git/config` or `.git/hooks/pre-commit` -> refused, target
# unchanged.
def test_map_inject_refuses_an_agents_md_symlink_into_the_git_directory(tmp_path, env, git_target):
    root = _repo(tmp_path)
    target = os.path.join(root, git_target)
    if not os.path.exists(target):
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
    before = _read(target)
    os.symlink(git_target, os.path.join(root, "AGENTS.md"))
    out = _cli(env, "map", "--inject", root)
    assert _read(target) == before, f"map --inject wrote into {git_target}"
    assert "AGENTS.md" in out.stdout + out.stderr


# Spec: When `AGENTS.md` ... is a symlink that resolves outside the repo root ..., ... `code.map`
# with `inject` write[s] nothing through it, the target is unchanged, and the refusal is reported.
def test_code_map_inject_refuses_an_agents_md_symlink_outside_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBASE_MEMORY_HOME", str(tmp_path / "cbm-cache"))
    monkeypatch.setattr("codeintel.engines.graph.provider.shutil.which", lambda x: None)
    root = _repo(tmp_path)
    victim = _victim(tmp_path)
    os.symlink(victim, os.path.join(root, "AGENTS.md"))
    res = code_map_handler({"project_root": root, "inject": True})
    assert _read(victim) == ORIGINAL, "code.map inject wrote through the symlink"
    assert "AGENTS.md" in str(res.get("inject")), res


# Spec: An `AGENTS.md`/`CLAUDE.md` symlink that resolves to a regular file elsewhere inside the work
# tree (a shared rules file) is still written through, as before.
# Edge case: `CLAUDE.md` symlinked to a file inside the repo -> written through, link kept.
# (Paired with an outside link in the same repo: the in-tree link alone already works today.)
def test_map_inject_follows_an_in_tree_rules_link_but_refuses_an_outside_one(tmp_path, env):
    root = _repo(tmp_path)
    os.makedirs(os.path.join(root, "rules"))
    shared = os.path.join(root, "rules", "shared.md")
    with open(shared, "w", encoding="utf-8") as fh:
        fh.write("# team rules\n")
    os.symlink(os.path.join("rules", "shared.md"), os.path.join(root, "AGENTS.md"))
    victim = _victim(tmp_path)
    os.symlink(victim, os.path.join(root, "CLAUDE.md"))
    _cli(env, "map", "--inject", root)
    assert os.path.islink(os.path.join(root, "AGENTS.md")), "the in-tree link was replaced"
    assert "codeintel" in _read(shared).lower(), "the block did not reach the in-tree rules file"
    assert _read(victim) == ORIGINAL, "map --inject wrote through the outside CLAUDE.md link"


# Spec: `codeintel map --inject` in a repo with no AGENTS.md or CLAUDE.md creates AGENTS.md. The
# advice `codeintel install` prints therefore works.
def test_map_inject_creates_agents_md_when_there_is_no_context_file(tmp_path, env):
    root = _repo(tmp_path)
    out = _cli(env, "map", "--inject", root)
    agents = os.path.join(root, "AGENTS.md")
    assert os.path.isfile(agents), out.stdout + out.stderr
    assert "codeintel" in _read(agents).lower()
