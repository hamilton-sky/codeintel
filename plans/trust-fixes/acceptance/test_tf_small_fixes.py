"""Small fixes: the MCP wire bound, `codeintel prompt`, path-with-spaces rows, `reset`, Ctrl-C and
docs/deploy.md."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest
from _fakegraph import Repo

from codeintel.transports.mcp import MCP_ENVELOPE_BUDGET_BYTES, compact_for_mcp

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BIN = os.path.dirname(sys.executable)


def _env(tmp_path, **extra: str) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    e = dict(os.environ)
    e.update({
        "HOME": str(home),
        "CODEINTEL_HOME": str(home / ".codeintel"),
        "HF_HUB_OFFLINE": "1",
        "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed-empty"),
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
        "PATH": "/usr/bin:/bin",
    })
    e.pop("CODEBASE_MEMORY_HOME", None)
    e.update(extra)
    return e


def _cli(env: dict, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "codeintel", *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------------- MCP wire bytes

def _wire_text(envelope: dict) -> str:
    """The text block the installed `mcp` server actually sends for a dict tool result."""
    from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content

    blocks = _convert_to_content(envelope)
    assert len(blocks) == 1
    return blocks[0].text


# Spec: An MCP `code.query` answer's text block is at most 40,000 bytes as sent on the wire.
def test_mcp_answer_text_block_is_at_most_40000_bytes_on_the_wire():
    def envelope(n: int) -> dict:
        rows = [{"relation": "caller", "name": f"rün_{i}", "qualified_name": f"pkg.mod_{i}.rün_{i}",
                 "file": f"pkg/mod_{i}.py", "module_scope": False, "edge": "CALLS",
                 "verified": True, "qualifier": None, "qualifier_seen": None,
                 "evidence": "resolved", "strategy": "import_map", "confidence": None,
                 "why": "the caller's file imports this symbol"} for i in range(n)]
        return {"ok": True, "op": "impact", "target": "run", "engine": "graph", "cached": False,
                "result": "## Impact of run\n" + "\n".join(f"- pkg.mod_{i}.rün_{i}" for i in range(n)),
                "rows": rows, "confidence": "complete", "outcome": "answered"}

    # The largest answer whose COMPACT JSON fits the budget: pretty-printed it does not.
    n = 1
    while len(json.dumps(envelope(n + 1), default=str).encode()) <= MCP_ENVELOPE_BUDGET_BYTES:
        n += 1
    big = envelope(n)
    assert len(_wire_text(big).encode("utf-8")) > MCP_ENVELOPE_BUDGET_BYTES  # precondition

    sent = _wire_text(compact_for_mcp(big, "impact", "run"))
    assert len(sent.encode("utf-8")) <= MCP_ENVELOPE_BUDGET_BYTES, len(sent.encode("utf-8"))


# ------------------------------------------------------------------------- codeintel prompt

# Spec: `codeintel prompt` bases its steps on doctor's `reason`: no `pip install` when the package
# is installed and only the model download failed. It lists `code.status`.
def test_prompt_does_not_reinstall_when_only_the_model_download_failed(tmp_path):
    if shutil.which("codebase-memory-mcp", path=_BIN) is None:
        pytest.skip("codebase-memory-mcp is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
    # The graph engine is installed and indexed; uv is on PATH; only the model cannot download.
    env = _env(tmp_path, PATH=_BIN + os.pathsep + os.path.expanduser("~/.local/bin")
               + os.pathsep + "/usr/bin:/bin")
    _cli(env, "index", str(root))
    report = json.loads(_cli(env, "doctor", str(root), "--json").stdout)
    assert report["engines"]["semantic"]["installed"] is True
    assert report["engines"]["graph"]["status"] == "ok", report["engines"]["graph"]

    out = _cli(env, "prompt", str(root))
    text = out.stdout
    assert "pip install" not in text, text
    assert "code.status" in text, text


# ------------------------------------------------------------------------- path with spaces

# Spec: Rows from a repo path with spaces (`ünï cødé/日本/uni.py`) render as
# `ünï cødé.日本.uni.unicaller`.
def test_rows_from_a_path_with_spaces_render_without_the_project_id(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch, name="mixed")
    repo.write("ünï cødé/日本/uni.py",
               "def unihelper():\n    return 1\n\n\ndef unicaller():\n    return unihelper()\n")
    repo.commit()
    g = repo.graph
    helper = g.node("ünï cødé.日本.uni.unihelper", "ünï cødé/日本/uni.py")
    caller = g.node("ünï cødé.日本.uni.unicaller", "ünï cødé/日本/uni.py")
    g.edge(caller, helper, strategy="same_module", confidence="0.90")
    repo.fresh_index()
    env = repo.ask("callers", "unihelper")
    text = str(env.get("result") or "")
    assert re.search(r"^- ünï cødé\.日本\.uni\.unicaller\b", text, re.M), text
    assert g.project not in text, text


# ------------------------------------------------------------------------- reset

# Spec: `reset --help` says it clears the semantic and graph indexes.
def test_reset_help_says_it_clears_the_semantic_and_graph_indexes(tmp_path):
    out = _cli(_env(tmp_path), "reset", "--help")
    text = out.stdout.lower()
    assert "semantic" in text and "graph" in text, out.stdout
    first = text.split("\n\n")[1] if "\n\n" in text else text
    assert "graph" in first, f"the description line does not mention the graph: {first!r}"


def _graph_db(tmp_path, env: dict, root: str) -> str:
    cache = os.path.join(env["HOME"], ".cache", "codebase-memory-mcp")
    os.makedirs(cache, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", os.path.realpath(root)).strip("-")
    db = os.path.join(cache, slug + ".db")
    with open(db, "wb") as fh:
        fh.write(b"SQLite format 3\x00")
    return db


# Spec: `reset --semantic-only` keeps the graph.
def test_reset_semantic_only_keeps_the_graph_index(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def a():\n    return 1\n")
    env = _env(tmp_path)
    db = _graph_db(tmp_path, env, str(root))
    out = _cli(env, "reset", str(root), "--semantic-only", "--yes")
    assert out.returncode == 0, out.stdout + out.stderr
    assert os.path.exists(db), "reset --semantic-only removed the graph index"


# Spec: ... and doctor's corrupt-DB fix uses it.
def test_doctor_fix_for_a_corrupt_semantic_db_uses_reset_semantic_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def a():\n    return 1\n")
    env = _env(tmp_path)
    os.makedirs(env["CODEINTEL_HOME"], exist_ok=True)
    with open(os.path.join(env["CODEINTEL_HOME"], "semantic.db"), "wb") as fh:
        fh.write(os.urandom(4096))
    report = json.loads(_cli(env, "doctor", str(root), "--json").stdout)
    fix = str(report["engines"]["semantic"].get("remediation") or "")
    assert "codeintel reset" in fix, fix
    assert "--semantic-only" in fix, fix


# ------------------------------------------------------------------------- Ctrl-C

# Spec: Ctrl-C during any command prints one line, `interrupted`, and exits 130, with no traceback.
# Edge case: Ctrl-C while waiting on the index lock -> `interrupted`, exit 130.
def test_ctrl_c_while_index_waits_on_the_lock_prints_interrupted_and_exits_130(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def a():\n    return 1\n")
    env = _env(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import os, sys\n"
         "from codeintel.core.filelock import exclusive, lock_path\n"
         "with exclusive(lock_path(os.path.realpath(sys.argv[1]))) as got:\n"
         "    print('held' if got else 'busy', flush=True)\n"
         "    sys.stdin.read()\n",
         str(root)],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proc = None
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == b"held"
        proc = subprocess.Popen([sys.executable, "-m", "codeintel", "index", str(root)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        deadline = time.time() + 60
        seen = ""
        while "waiting for another codeintel process" not in seen and time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            seen += line
        assert "waiting for another codeintel process" in seen, seen
        proc.send_signal(signal.SIGINT)
        rest_out, err = proc.communicate(timeout=60)
        out = seen + rest_out
        assert proc.returncode == 130, (proc.returncode, out, err)
        assert "Traceback" not in out + err, err
        assert "KeyboardInterrupt" not in out + err, err
        lines = [ln for ln in (out[len(seen):] + err).splitlines() if ln.strip()]
        assert lines == ["interrupted"], lines
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        assert holder.stdin is not None and holder.stdout is not None
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()


# ------------------------------------------------------------------------- docs/deploy.md

# Spec: `docs/deploy.md` says the snippet symlink hole is closed via `open_contained`.
def test_deploy_doc_says_the_snippet_symlink_hole_is_closed_via_open_contained():
    text = (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "open_contained" in text
