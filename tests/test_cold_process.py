"""Cold-process tier — call #1 in a PRISTINE process, never call #2 in a warm one.

Every autouse fixture in conftest.py (`_fresh_gateway`, `_reset_graph_wire_format_cache`) resets
state BETWEEN tests, but all of that happens inside one interpreter that has already imported
every provider module, already warmed a serena session in a prior test, already cached a graph
wire-format verdict from a prior test. `tests/test_corpus.py::_indexed_graph` goes further and
indexes the corpus BEFORE any subprocess in that file ever queries it. None of that is the shape
of the defect this file exists to catch: B1 (docs/eval-2026-08-17.md:65-103) was a cold LSP
session that timed out on the very first call of a fresh process, and the empty reference list
that produced rendered as a confident "(none)" — call #2, against the same warmed session,
answered correctly. A suite that always resets between tests rather than ever measuring a truly
first call cannot express "call #1 disagreed with call #2", so this file drives the CLI and the
gateway as `sys.executable -m codeintel` / `sys.executable -c` subprocesses, each one launched
into a from-scratch `$HOME` that has never run codeintel before.

Two tiers:
  * Tests 1-2 are the DEFAULT tier: no external backend required, they run on every machine and
    in every CI matrix cell (see the `lsp-contract` job's new "Cold-process tier" step).
  * Tests 3-4 are OPT-IN (`CODEINTEL_COLD_PROCESS=1`) because they launch a real `uvx`/serena
    session and cost roughly 12-25s (test 3) or up to the full 120s query budget (test 4) each.

MEASURED VERDICT on `CODEBASE_MEMORY_HOME`/`CODEBASE_MEMORY_CACHE_DIR` (2026-08-18, macOS,
codebase-memory-mcp 0.9.0 — see `test_a_cold_query_writes_nothing_outside_its_sandbox`): the
external binary DOES honour both overrides. A `list_projects` call run with both pointed at a
sandbox directory left no trace of the calling repo's path-slug project id anywhere under the
real `~/.cache/codebase-memory-mcp`. (The test checks for that trace rather than a raw
before/after mtime of the shared directory: this dev machine runs OTHER, unrelated
codebase-memory-mcp/codeintel MCP server processes concurrently — `ps aux` shows them — and they
continuously touch that directory's shared files on their own, which would make a whole-directory
mtime comparison fail for reasons that have nothing to do with this test's own cold query.)
`_HOME_WRITE_EXCEPTIONS` below is therefore empty by measurement, not by assumption —
`reset.py:120`'s honouring assumption held.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

import codeintel
import codeintel.providers

# --------------------------------------------------------------------------------------------- #
# Reason-family constants — shared by every assertion below. `reason` values, not test-specific
# guesses: they are the vocabulary every provider's `safe_null_result` already speaks.
# --------------------------------------------------------------------------------------------- #

_COULD_NOT_ASK = {
    "engine-unavailable", "backend-unreachable", "backend-error", "backend-incompatible",
    "boot-failed", "warming", "timeout", "unparsable", "provider-error", "error",
    "project-not-indexed", "project-not-indexed-standalone",
}
_ASKED_AND_FOUND_NOTHING = {"not-in-graph", "no-result", "below-floor", "not-found"}
_EMPTINESS_MARKERS = ("(none)", "(none found)", "(no matches)", "(0)")

# One representative (op, target) per engine, keyed by the provider module's own filename — the
# DOMAIN this list must cover is derived from disk in the tests below, never hand-typed here.
_ENGINE_PROBE: dict[str, tuple[str, str]] = {
    "lsp": ("symbol", "make_widget"),
    "graph": ("callers", "make_widget"),
    "semantic": ("search", "where is the widget factory"),
}

# KNOWN cold-process defects, per engine, recorded as DATA rather than assumed. Empty by
# measurement (2026-08-18): the graph provider's `result_text is None` branch (graph.py, just
# before it returns `reason="not-in-graph"`) genuinely does not consult `self._last_failure` — a
# real, still-live defect — but it is NOT reachable through THIS probe. This probe's shim is dead
# from the first byte, so `GraphProvider._lookup_project` always fails at `list_projects` (which
# runs BEFORE `_dispatch`) and reports `reason="backend-unreachable"`, a member of
# `_COULD_NOT_ASK` — the buggy branch is only reached when `list_projects` SUCCEEDS and a later,
# op-specific call fails, which a uniformly-dead binary can never produce. Verified by planting
# `{"graph": "..."}` here with a self-destructing assertion (see the loop below) and watching it
# fail immediately with "the recorded defect is gone" — proving the claim false for this probe,
# not merely absent. If a future probe DOES observe it, add an entry here paired with the same
# self-destructing assertion rather than widening `_COULD_NOT_ASK` to paper over it.
_KNOWN_COLD_DEFECTS: dict[str, str] = {}

# Paths (relative to the hermetic repo) that a cold query is known to write, and why.
_REPO_WRITE_EXCEPTIONS = {
    ".serena/": "serena writes document-symbol pickles into the repo it only reads (measured "
                "2026-08-18, D5); gitignored by .serena/.gitignore, so `git status` hides it",
}

# Exact repo-relative... no — exact ABSOLUTE paths under the real (non-sandboxed) home that a cold
# query is known to touch despite the sandbox env vars, and why. Empty by measurement — see the
# module docstring. Written as an EXACT-match assertion (not a prefix) so it fails the moment the
# backend starts honouring the override, per project rule 4 ("record exceptions as data").
_HOME_WRITE_EXCEPTIONS: dict[str, str] = {}

_COLD_PROCESS_OPT_IN = os.environ.get("CODEINTEL_COLD_PROCESS", "").strip().lower() not in (
    "1", "true", "on", "yes",
)


# --------------------------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------------------------- #

def _hermetic_repo(tmp_path: pathlib.Path) -> str:
    """A 2-file Python repo, no clone, no network. `make_widget` sits on a KNOWN line (2) of
    `widget.py`, referenced from `consumer.py` so every engine has a real caller/reference to find."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "widget.py").write_text(
        '"""Widget module."""\n'
        "def make_widget(name):\n"
        '    return {"name": name}\n'
    )
    (repo / "consumer.py").write_text(
        "from widget import make_widget\n"
        "\n"
        "\n"
        "def build():\n"
        '    return make_widget("w")\n'
    )
    (repo / "pyproject.toml").write_text('[project]\nname = "coldfixture"\nversion = "0"\n')
    return str(repo)


def _sandbox_env(tmp_path: pathlib.Path, *, shim: bool) -> dict[str, str]:
    """A pristine process environment — no inherited state from the machine running the suite,
    beyond (when `shim` is False) whatever real backends happen to be on PATH."""
    home = tmp_path / "home"
    cache = home / ".cache"
    codeintel_home = home / ".codeintel"
    cbm_home = cache / "codebase-memory-mcp"
    for d in (home, cache, codeintel_home, cbm_home):
        d.mkdir(parents=True, exist_ok=True)

    interp_dir = os.path.dirname(sys.executable)
    if shim:
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        for name in ("uvx", "serena", "codebase-memory-mcp"):
            script = shim_dir / name
            script.write_text("#!/bin/sh\nexit 1\n")
            script.chmod(0o755)
        path = os.pathsep.join([str(shim_dir), "/usr/bin", "/bin", interp_dir])
    else:
        # Real backends if installed — this is the measurement tier, never a mock.
        path = os.pathsep.join([os.environ.get("PATH", ""), interp_dir])

    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "CODEINTEL_HOME": str(codeintel_home),
        "CODEBASE_MEMORY_HOME": str(cbm_home),
        "CODEBASE_MEMORY_CACHE_DIR": str(cbm_home),
        "PYTHONPATH": str(pathlib.Path(codeintel.__file__).parents[1]),
        "PATH": path,
    }


def _query(
    repo: str, env: dict[str, str], *, op: str, target: str, engine: str, timeout: int = 120,
) -> dict:
    """Run one `codeintel query --json` as call #1 in a fresh process. Never mocked."""
    argv = [
        sys.executable, "-m", "codeintel", "query",
        "--op", op, "--target", target, "--engine", engine, "--json",
        "--project-root", repo,
    ]
    try:
        proc = subprocess.run(
            argv, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{engine}: cold query never returned within its {timeout}s budget — a call "
                    f"that never returns is itself the defect this tier exists to catch, not "
                    f"something to skip")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{engine}: --json run produced unparseable stdout ({exc}). "
            f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
        )


def _discovered_engines() -> list[str]:
    """The DOMAIN test 1 must probe, derived from the FILESYSTEM — never hand-typed. `none.py`
    always answers `reason="no-engine"` by construction and has nothing to be cold about."""
    stems = {p.stem for p in pathlib.Path(codeintel.providers.__file__).parent.glob("*.py")}
    return sorted(stems - {"__init__", "none"})


# --------------------------------------------------------------------------------------------- #
# Test 1 — DEFAULT TIER: a dead backend on call #1 must be reported as unasked, never as empty.
# --------------------------------------------------------------------------------------------- #

def test_a_dead_backend_on_call_one_is_reported_as_unasked_not_as_empty(tmp_path):
    engines = _discovered_engines()
    for engine in engines:
        assert engine in _ENGINE_PROBE, (
            f"a new provider was added and the cold tier does not probe it: {engine!r} "
            f"(src/codeintel/providers/{engine}.py) has no entry in _ENGINE_PROBE — add one to "
            f"tests/test_cold_process.py before this can ship"
        )

    repo = _hermetic_repo(tmp_path)
    env = _sandbox_env(tmp_path, shim=True)

    for engine in engines:
        op, target = _ENGINE_PROBE[engine]
        envelope = _query(repo, env, op=op, target=target, engine=engine)

        assert envelope.get("ok") is True, f"{engine}: envelope was not ok: {envelope}"
        reason = envelope.get("reason")
        result = envelope.get("result")

        if engine in _KNOWN_COLD_DEFECTS:
            # Self-destructing: the day this stops being true, the recorded defect is gone.
            assert reason == "not-in-graph", (
                f"the recorded defect is gone — delete the _KNOWN_COLD_DEFECTS entry for "
                f"{engine!r} ({_KNOWN_COLD_DEFECTS[engine]}). envelope={envelope}"
            )
            continue

        if result is None:
            assert reason in _COULD_NOT_ASK, (
                f"{engine}: a dead backend on call #1 produced reason={reason!r}, which is not in "
                f"the could-not-ask family — an agent reading this would take it as a real "
                f"negative answer about the code, not as \"the engine never ran\". "
                f"envelope={envelope}"
            )
            assert reason not in _ASKED_AND_FOUND_NOTHING, (
                f"{engine}: reason={reason!r} claims the engine asked the code and found nothing, "
                f"but the backend was a dead shim that never answered anything. envelope={envelope}"
            )
        else:
            confident_empty = (
                any(marker in result for marker in _EMPTINESS_MARKERS)
                and envelope.get("confidence") == "complete"
            )
            assert not confident_empty, (
                f"{engine}: a dead backend rendered a confident empty answer — the exact B1 shape "
                f"(docs/eval-2026-08-17.md:65-103): an engine that never ran producing "
                f'"(none)" with confidence="complete". envelope={envelope}'
            )

        hint = envelope.get("hint") or ""
        assert "is not in the graph index" not in hint, (
            f"{engine}: hint claims the target is not in the graph index, but the backend was "
            f"dead and never actually asked the graph anything. envelope={envelope}"
        )


# --------------------------------------------------------------------------------------------- #
# Test 2 — DEFAULT TIER: a cold query must write nothing outside its own sandbox.
# --------------------------------------------------------------------------------------------- #

def _tree_snapshot(root: str) -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for p in pathlib.Path(root).rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return snap


def _slug_named_files(directory: pathlib.Path, slug: str) -> set[str]:
    """Every filename under `directory` that encodes `slug` — the flattened absolute path a
    backend uses as a path-slug project id (see graph.py's `_match_project` docstring). A name
    containing THIS hermetic repo's own slug can only be written by a process that resolved THIS
    repo's `project_root` — i.e. our subprocess, and nothing else running on the machine."""
    if not directory.exists():
        return set()
    return {str(p.relative_to(directory)) for p in directory.rglob("*") if slug in p.name}


def test_a_cold_query_writes_nothing_outside_its_sandbox(tmp_path):
    repo = _hermetic_repo(tmp_path)
    env = _sandbox_env(tmp_path, shim=False)

    real_home = pathlib.Path(os.path.expanduser("~"))
    real_codeintel_home = real_home / ".codeintel"
    real_graph_cache = real_home / ".cache" / "codebase-memory-mcp"

    # NOT a whole-directory mtime/existence diff: this dev machine (and any shared CI runner
    # reusing a persistent MCP host) can have OTHER, unrelated codebase-memory-mcp/codeintel
    # processes live at the same time — `ps aux` on the machine this was authored on shows
    # standing `codebase-memory-mcp` MCP servers for OTHER repos, which continuously touch shared
    # files (`_config.db`, per-project `.db-wal`/`.db-shm`) regardless of anything this test does.
    # A raw before/after mtime comparison of the shared directory is therefore not evidence of
    # anything OUR subprocess wrote — it is evidence of whatever ELSE happens to be running.
    # `tmp_path` is unique per test run, so a filename containing ITS slug is unambiguous: nothing
    # but a process that resolved THIS test's `project_root` could have produced it.
    slug = repo.strip("/").replace("/", "-")

    before_repo = _tree_snapshot(repo)
    before_codeintel_slug = _slug_named_files(real_codeintel_home, slug)
    before_graph_slug = _slug_named_files(real_graph_cache, slug)

    for engine in _discovered_engines():
        op, target = _ENGINE_PROBE[engine]
        # Real backends if installed; if none are, this still asserts nothing was written — a
        # meaningful pass, not a skip.
        _query(repo, env, op=op, target=target, engine=engine)

    after_repo = _tree_snapshot(repo)
    offenders = sorted(
        rel for rel, after_v in after_repo.items()
        if before_repo.get(rel) != after_v
        and not any(rel.startswith(prefix) for prefix in _REPO_WRITE_EXCEPTIONS)
    )
    assert not offenders, (
        "cold queries wrote inside the hermetic repo, outside every recorded exception:\n  "
        + "\n  ".join(offenders)
    )

    new_codeintel = _slug_named_files(real_codeintel_home, slug) - before_codeintel_slug
    new_graph = _slug_named_files(real_graph_cache, slug) - before_graph_slug
    home_offenders = []
    if new_codeintel:
        home_offenders.append(f"{real_codeintel_home}: {sorted(new_codeintel)}")
    if new_graph:
        home_offenders.append(f"{real_graph_cache}: {sorted(new_graph)}")

    for path in home_offenders:
        assert path in _HOME_WRITE_EXCEPTIONS, (
            f"a cold query left a trace of THIS hermetic repo in the REAL home cache at {path}, "
            f"outside the sandbox, even though CODEINTEL_HOME/CODEBASE_MEMORY_HOME/"
            f"CODEBASE_MEMORY_CACHE_DIR were all set to redirect it — either an override stopped "
            f"being honoured, or a new code path writes there without consulting it. Investigate "
            f"before adding an exception."
        )
    for path, note in _HOME_WRITE_EXCEPTIONS.items():
        assert path in home_offenders, (
            f"{path} is recorded in _HOME_WRITE_EXCEPTIONS ({note}) but was NOT touched this "
            f"run — the backend appears to honour the override now; delete this entry"
        )


# --------------------------------------------------------------------------------------------- #
# Test 3 — OPT-IN TIER: call #1 in a fresh process is never MORE confident than call #2.
#
# Cost: one subprocess per test run, ~12-25s (dominated by a real `uvx`/serena boot). Gated on
# CODEINTEL_COLD_PROCESS=1, same style as tests/test_corpus.py's CODEINTEL_CORPUS gate, but
# applied per-function (not module-level) because tests 1-2 in this file must stay in the default
# tier.
# --------------------------------------------------------------------------------------------- #

def _rows(envelope: dict) -> int:
    return len(re.findall(r"^- \S+:\d+", envelope.get("result") or "", re.M))


@pytest.mark.skipif(
    _COLD_PROCESS_OPT_IN,
    reason="cold-process live tier is opt-in: set CODEINTEL_COLD_PROCESS=1 (launches a real uvx/serena session)",
)
def test_call_one_in_a_fresh_process_is_never_more_confident_than_call_two(tmp_path):
    repo = _hermetic_repo(tmp_path)
    env = _sandbox_env(tmp_path, shim=False)

    # Drive the GATEWAY, not the CLI: commands/query.py:87-94 loops on reason=="warming" and would
    # hide call #1 behind its own retry. `a` below is the literal first `gw.query()` this process
    # ever makes.
    #
    # `a` is measured (2026-08-18) to reliably land in WARMING and return in <1ms — session
    # creation is lazy and boot takes ~19s on this machine, so `a` structurally never reaches
    # `_dispatch`. That means a fault planted INSIDE `_dispatch` (as the falsifiability proof for
    # this test requires: "give LspProvider a first-call fault") can only ever be observed on `b`,
    # the first call that reaches a READY session — not on `a`. So this program also takes a third
    # call, `c`, immediately after `b`, and applies the SAME invariant to the (b, c) pair as the
    # spec applies to (a, b). Without it, a `_dispatch`-level first-call fault is invisible to this
    # test: both `a` (still warming) and a faulty `b` would show 0 rows, and the (a, b) comparison
    # alone is vacuously satisfied either way.
    #
    # `c` asks with `op="context"` rather than repeating `op="symbol"`: the gateway's
    # ContentHashCache key is `(op, target, engine, project_root)` (cache.py:54) with no budget/
    # role component, so a byte-identical repeat of `b`'s own query is served straight out of the
    # cache — proven while writing this test, where a `_dispatch`-level fault plant on `b` was
    # silently invisible because `c` just replayed `b`'s (wrong) cached answer instead of
    # re-dispatching. `context` still routes to `LspProvider._op_symbol` for the same target
    # (lsp.py:370-372), so it is a genuine independent dispatch of an equivalent question, not a
    # different invariant.
    prog = f"""
import json, time
from codeintel import server

gw = server._build_gateway(oneshot=True)
kwargs = dict(op="symbol", target="make_widget", engine="lsp", role="", budget=30000,
              project_root={repo!r})

a = gw.query(**kwargs)

b = a
deadline = time.monotonic() + 90.0
while time.monotonic() < deadline:
    time.sleep(1.0)
    b = gw.query(**kwargs)
    if b.get("result") is not None or b.get("reason") != "warming":
        break

c = gw.query(**{{**kwargs, "op": "context"}})
print(json.dumps([a, b, c]))
"""
    proc = subprocess.run(
        [sys.executable, "-c", prog], cwd=repo, env=env, capture_output=True, text=True,
        timeout=150,
    )
    if proc.returncode != 0:
        pytest.fail(f"cold LSP gateway subprocess failed (exit {proc.returncode}):\n"
                    f"{proc.stderr[-4000:]}")
    try:
        a, b, c = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"unparseable stdout from cold LSP gateway subprocess: {exc}\n"
                    f"stdout={proc.stdout!r}")

    if a.get("reason") == "engine-unavailable" or b.get("reason") == "engine-unavailable":
        pytest.skip("lsp engine unavailable in this sandbox (no uvx/serena on PATH)")
    if b.get("result") is None and b.get("reason") == "boot-failed":
        pytest.skip("serena failed to boot in this sandbox")

    assert a.get("ok") is True, a

    ra, rb, rc = _rows(a), _rows(b), _rows(c)

    def _asked_and_marked_partial_or_unasked(envelope: dict) -> bool:
        if envelope.get("result") is None:
            return envelope.get("reason") in _COULD_NOT_ASK
        return envelope.get("confidence") == "partial" and bool(envelope.get("gaps"))

    if ra < rb:
        assert _asked_and_marked_partial_or_unasked(a), (
            f"call #1 had fewer reference rows than call #2 (rows(a)={ra}, rows(b)={rb}) yet "
            f"call #1 was not marked partial/unasked.\na={a!r}\nb={b!r}"
        )

    if a.get("result") is None:
        assert a.get("reason") in _COULD_NOT_ASK, a
        assert a.get("reason") not in _ASKED_AND_FOUND_NOTHING, a

    if (a.get("result") is not None and any(m in a["result"] for m in _EMPTINESS_MARKERS)
            and rb > 0):
        assert a.get("confidence") != "complete", (
            f"call #1's body reads as empty ({_EMPTINESS_MARKERS}) while call #2 found {rb} "
            f"references, yet call #1 claims confidence=complete.\na={a!r}\nb={b!r}"
        )

    # See the docstring above: this is the extension that makes a `_dispatch`-level first-call
    # fault actually observable, since `a` never reaches `_dispatch` on this backend.
    if rb < rc:
        assert _asked_and_marked_partial_or_unasked(b), (
            f"the first call to actually reach a dispatched answer (b) had fewer reference rows "
            f"than an identical immediate follow-up (c) — rows(b)={rb}, rows(c)={rc} — yet b was "
            f"not marked partial/unasked. This is the B1 shape one layer deeper: a fault local to "
            f"the first-ever LSP dispatch call, not to session warm-up.\nb={b!r}\nc={c!r}"
        )


# --------------------------------------------------------------------------------------------- #
# Test 4 — OPT-IN TIER: a cold semantic query returns within its budget.
#
# MEASURED (2026-08-18, macOS, fastembed model already warm on this machine): a cold
# `search`/semantic query with CODEINTEL_REINDEX=off against the hermetic fixture returned in
# well under a second. It did NOT reproduce the >5-minute hang the eval doc records for
# `CODEINTEL_HOME=<empty dir> CODEINTEL_REINDEX=off`. No xfail marker is applied: if a future
# regression reintroduces the hang, `_query`'s `timeout=120` turns it into a named `pytest.fail`
# rather than a silent multi-minute stall — which is the outcome that made the original hang
# findable in the first place.
# --------------------------------------------------------------------------------------------- #

@pytest.mark.skipif(
    _COLD_PROCESS_OPT_IN,
    reason="cold-process live tier is opt-in: set CODEINTEL_COLD_PROCESS=1 (downloads the embedding model on a cold cache)",
)
def test_a_cold_semantic_query_returns_within_its_budget(tmp_path):
    repo = _hermetic_repo(tmp_path)
    env = _sandbox_env(tmp_path, shim=False)
    env["CODEINTEL_REINDEX"] = "off"

    envelope = _query(
        repo, env, op="search", target="where is the widget factory", engine="semantic",
        timeout=120,
    )
    assert envelope.get("ok") is True, envelope


def test_a_starved_budget_never_renders_an_emptiness_claim(tmp_path):
    """No answer produced under a budget nothing could meet may claim to be a complete absence.

    READ THIS BEFORE TRUSTING THIS TIER TO CATCH B1 — IT CANNOT, AND THAT IS MEASURED.

    This file was built for B1: a cold backend call that times out and gets rendered as a confident
    "(none)". It does not catch it. Reintroducing the exact regression (swallow the Missing and
    return an empty References section) leaves every test in this file GREEN. Two reasons, and the
    second is structural rather than incidental:

      1. On a fast machine with warm caches the cold call simply SUCCEEDS, so the failing branch is
         never reached.
      2. Starving the budget does not help either. B1 needs a DIFFERENTIAL failure — find_symbol
         SUCCEEDS and find_referencing_symbols then times out — because the reference renderer is
         only reached once a definition has resolved. A single global budget starves the FIRST call,
         so the op returns a null result with reason "backend-error" and the reference branch is
         never entered. Measured: CODEINTEL_BUDGET_MS=1 gives result=None, reason=backend-error.
         No process-level knob can produce "first call fine, second call not".

    B1's rendering defect is therefore guarded IN-PROCESS, where the two tool calls can be stubbed
    independently: tests/test_incompleteness.py::test_starved_symbol_never_claims_zero_references.
    That one DOES go red on the planted regression — verified the same way.

    What this test is worth: it pins the weaker, environment-level invariant that a real subprocess
    under an impossible budget still returns an honest envelope rather than a confident absence. Keep
    it, but do not let this file's existence persuade anyone that B1 is covered here.
    """
    repo = _hermetic_repo(tmp_path)
    env = _sandbox_env(tmp_path, shim=False)
    env["CODEINTEL_BUDGET_MS"] = "1"          # nothing answers in 1ms

    for op, target, engine in (("symbol", "make_widget", "lsp"),
                               ("callers", "make_widget", "graph")):
        env_out = _query(repo, env, op=op, target=target, engine=engine, timeout=120)
        assert env_out["ok"] is True, (op, env_out)
        result = env_out.get("result")
        if result is None:
            # Honest: it could not ask, and said so.
            assert env_out.get("reason") not in _ASKED_AND_FOUND_NOTHING, (op, env_out)
            continue
        # It produced a body under a budget nothing could meet. That body may not assert absence
        # while also claiming completeness.
        claimed_empty = any(m in result for m in _EMPTINESS_MARKERS)
        assert not (claimed_empty and env_out.get("confidence") == "complete"), (
            f"{op}: rendered an emptiness claim with confidence=complete under a 1ms budget — "
            f"this is B1's exact shape: {result[:300]}"
        )
