#!/usr/bin/env python3
"""Release canary — prove the BUILT ARTIFACT works the way a user will actually meet it.

The unit suite runs against the source tree with pytest's imports; it cannot catch a packaging
mistake, a missing entry point, a host config file written where nobody reads it, or a server that
boots and answers nothing. Those are exactly the failures this project has shipped before, twice,
with a green CI.

Nor can the CLI catch them: `codeintel status` and `codeintel query` are never-raise and always
exit 0, and every MCP result is a safe envelope whose `ok` is always True. A smoke test that runs
those commands and checks the exit code passes against a completely inert build. So this asserts
on CONTENT — the answer text, and the config bytes on disk — at every step.

Run it against a clean-venv install of the wheel, NOT the source checkout:

    python -m venv /tmp/canary
    /tmp/canary/bin/python -m pip install dist/*.whl
    /tmp/canary/bin/python scripts/release_canary.py

Exits 0 only if every step passes. Every side effect lands in a temp HOME that is removed on exit.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

FIXTURE = {
    "reticulator.py": '''"""Spline reticulation utilities."""


def reticulate_splines(count):
    """Reticulate `count` splines and report how many were reticulated."""
    reticulated = 0
    for _ in range(count):
        reticulated += 1
    return reticulated
''',
    "calc.py": '''"""Arithmetic helpers."""


def add(a, b):
    """Return the sum of two numbers."""
    return a + b


def total(items):
    """Sum a sequence by repeatedly calling add."""
    running = 0
    for item in items:
        running = add(running, item)
    return running
''',
    "README.md": "# canary fixture\n\nA tiny repo the release canary indexes and queries.\n",
}

# The distinctive symbol the semantic engine must surface. Deliberately not a word that appears in
# codeintel itself, so a result can only come from the fixture index.
NEEDLE_QUERY = "reticulate splines"
NEEDLE_SYMBOL = "reticulate_splines"

_failures: list[str] = []
_step = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _step
    _step += 1
    print(f"{'PASS' if ok else 'FAIL'}  {_step}. {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")
    if not ok:
        _failures.append(name)
    return ok


def run(argv: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, **kw)


def first_json(text: str):
    """The first JSON object in `text` — MCP bodies arrive as a text block, a structuredContent
    dump, or both joined."""
    for candidate in [text, *text.splitlines()]:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


def main() -> int:
    # Test the artifact belonging to the interpreter running this, however it was invoked
    # (`/tmp/verify/bin/python scripts/release_canary.py` does NOT put that venv on PATH). The
    # registered config launches the BARE name `codeintel`, exactly as a host would, so the
    # command has to resolve through PATH rather than being handed an absolute path here.
    #
    # sysconfig, NOT `Path(sys.executable).parent`: a venv's `bin/python` is a symlink to the base
    # interpreter, so resolving it lands in the SYSTEM bin and the canary silently tests whatever
    # `codeintel` happens to be installed globally — a green run against the wrong artifact, which
    # is the exact failure mode this script exists to catch. Observed doing it, hence the assert.
    scripts_dir = sysconfig.get_path("scripts")
    os.environ["PATH"] = os.pathsep.join([scripts_dir, os.environ.get("PATH", "")])

    cli = shutil.which("codeintel")
    if not check("`codeintel` resolves on PATH", bool(cli), cli or "not found"):
        return 1
    if not check("the `codeintel` on PATH is the artifact under test",
                 os.path.dirname(cli) == scripts_dir,
                 f"resolved: {cli}\nexpected in: {scripts_dir}"):
        return 1

    # Everything the tool writes — agent configs, the semantic DB, per-project config — is under
    # $HOME. Redirect it so the canary can never touch the real machine, and so the agent config
    # assertions below read files this run definitely created.
    home = Path(tempfile.mkdtemp(prefix="codeintel-canary-"))
    repo = home / "fixture-repo"
    repo.mkdir()
    for name, body in FIXTURE.items():
        (repo / name).write_text(body, encoding="utf-8")

    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)          # Windows equivalent
    os.environ.pop("CODEX_HOME", None)             # exercise the documented default layout
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("XDG_CONFIG_HOME", None)

    try:
        return _canary(cli, home, repo)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _canary(cli: str, home: Path, repo: Path) -> int:
    from codeintel.verify import verify_stdio_call

    # ---- 1. the entry point exists and reports a version --------------------------------------
    res = run([cli, "--version"])
    check("`codeintel --version` runs from the installed artifact",
          res.returncode == 0 and "codeintel" in res.stdout,
          (res.stdout + res.stderr).strip())

    # ---- 2. registration: the file, the right file, and a real handshake ----------------------
    # `install` verifies by launching the registered command and completing an MCP handshake, and
    # exits non-zero when that fails — so this gates on the host being able to START the server,
    # not merely on a config file existing.
    codex = run([cli, "install", "--agent", "codex"])
    check("`codeintel install --agent codex` succeeds and verifies",
          codex.returncode == 0 and "NOT verified" not in codex.stdout,
          (codex.stdout + codex.stderr).strip())

    codex_cfg = home / ".codex" / "config.toml"
    codex_text = codex_cfg.read_text(encoding="utf-8") if codex_cfg.exists() else ""
    check("Codex config holds an [mcp_servers.codeintel] TOML table",
          "[mcp_servers.codeintel]" in codex_text,
          f"{codex_cfg}\n{codex_text.strip() or '<missing>'}")

    claude = run([cli, "install", "--agent", "claude"])
    check("`codeintel install --agent claude` succeeds and verifies",
          claude.returncode == 0 and "NOT verified" not in claude.stdout,
          (claude.stdout + claude.stderr).strip())

    claude_cfg = home / ".claude.json"
    launch = {}
    try:
        launch = json.loads(claude_cfg.read_text(encoding="utf-8"))["mcpServers"]["codeintel"]
    except Exception:
        pass
    check("Claude Code config holds mcpServers.codeintel in ~/.claude.json",
          bool(launch.get("command")),
          f"{claude_cfg}\n{json.dumps(launch) if launch else '<missing>'}")

    # A user's next session launches EXACTLY this argv. Everything below drives that, rather than
    # a command this script picked — so the thing verified is the thing the host will run.
    command = launch.get("command") or "codeintel"
    args = list(launch.get("args") or ["serve"])

    # ---- 3. index the fixture ------------------------------------------------------------------
    indexed = run([cli, "index", str(repo)])
    check("`codeintel index` indexes a fresh repo",
          indexed.returncode == 0, (indexed.stdout + indexed.stderr).strip()[-800:])

    # ---- 4. the registered command answers a REAL query ----------------------------------------
    # The whole point of the canary: not `ok`, which is always True, but whether the answer
    # actually contains the symbol only this fixture's index could produce.
    q = verify_stdio_call(
        command, args, tool="code.query", timeout_s=180,
        arguments={"op": "search", "target": NEEDLE_QUERY,
                   "project_root": str(repo), "engine": "semantic"},
    )
    check("the registered command completes an MCP handshake and answers code.query",
          q["ok"], q["detail"])
    check(f"code.query returns the fixture symbol `{NEEDLE_SYMBOL}` (content, not just ok:true)",
          NEEDLE_SYMBOL in q.get("content", ""),
          (q.get("content") or "<empty>")[:600])

    # ---- 5. status tells the truth about the repo it just indexed -------------------------------
    s = verify_stdio_call(
        command, args, tool="code.status", timeout_s=120,
        arguments={"project_root": str(repo)},
    )
    status = first_json(s.get("content", "")) or {}
    semantic = ((status.get("readiness") or {}).get("semantic")) or {}
    check("code.status reports the semantic engine runnable on the indexed repo",
          semantic.get("installed") is True and semantic.get("runnable") is True,
          json.dumps(semantic) if semantic else (s.get("content") or "<empty>")[:400])
    check("code.status reports the fixture repo as indexed",
          status.get("indexed") is True or semantic.get("repo_indexed") is True,
          json.dumps({k: status.get(k) for k in ("indexed", "engines", "healthy")}))

    print()
    if _failures:
        print(f"CANARY FAILED — {len(_failures)} of {_step} checks failed:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"CANARY PASSED — {_step}/{_step} checks against the installed artifact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
