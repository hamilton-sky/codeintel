"""Preflight diagnostics — turn the tool's silent safe-null degradation into a clear signal.

`run_doctor` asks each engine three questions — installed? runnable? is THIS repo indexed? —
with a one-line remediation per gap. It is never-raise and bounded: no engine check may hang,
crash, load the embedding model, mutate state, or go through the gateway (no reindex side
effects). The same report drives the CLI `doctor` command, the `code.doctor` MCP tool, and HTTP.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from typing import Any

_ENGINES = ("graph", "lsp", "semantic")
_VERSION_TIMEOUT_S = 2.0
# Graph needs an external native binary (codebase-memory-mcp) codeintel can't auto-install, so a
# missing graph engine does NOT make a repo "unhealthy" — the tool is fully usable on semantic + lsp.
_OPTIONAL_ENGINES = frozenset({"graph"})


def _status_for(report: dict) -> str:
    """Roll a probe dict up to ok / warn / fail.

    fail = not installed, not runnable, or (graph/semantic) repo not indexed — all actionable.
    warn = installed but readiness unknown (lsp not-yet-warmed, or a deep boot that timed out).
    ok   = installed, runnable, and (where applicable) this repo is indexed."""
    if not report.get("installed"):
        return "fail"
    runnable = report.get("runnable")
    if runnable is False:
        return "fail"
    if report.get("repo_indexed") is False:
        return "fail"
    if runnable is None:
        return "warn"
    return "ok"


def _probe_engine(
    engine: str,
    provider: Any,
    build: Callable[[], Any],
    call: Callable[[Any], dict],
    on_provider: Callable[[str, Any], Any] | None = None,
) -> dict:
    """Run one engine's probe, never raising. Uses the passed-in (live) provider when given,
    else builds an ephemeral one.

    ``on_provider(engine, provider)`` is invoked with the instance actually probed, but only once
    that probe reported ``installed``. It exists so a caller holding a long-lived gateway can
    ADOPT an engine this function just proved is present — otherwise the report says "graph: ok"
    (from a fresh ephemeral provider) while the caller's own gateway still has no graph engine at
    all. Never raises, and never affects the report."""
    try:
        p = provider if provider is not None else build()
    except Exception as exc:
        r = {"installed": False, "runnable": False, "repo_indexed": None,
             "detail": f"could not construct provider ({type(exc).__name__})", "remediation": None}
        return {"engine": engine, "status": "fail", **r}
    try:
        r = dict(call(p) or {})
    except Exception as exc:
        r = {"installed": None, "runnable": False, "repo_indexed": None,
             "detail": f"probe raised ({type(exc).__name__})", "remediation": None}
    if on_provider is not None and r.get("installed") is True:
        try:
            on_provider(engine, p)
        except Exception:
            pass
    r["engine"] = engine
    r["status"] = _status_for(r)
    return r


def _cmd_version(argv: list) -> str | None:
    """Version number from ``<cmd> --version``. Never raises; hard-bounded; None if absent.

    Extracts just the numeric token — these tools each print their own banner
    (``codebase-memory-mcp 0.9.0``, ``uvx 0.12.1 (Homebrew …)``), and echoing that verbatim
    into a report that already labels the tool reads as noise."""
    try:
        import re
        import shutil
        import subprocess
        if not shutil.which(argv[0]):
            return None
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S)
        for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(r"\d+(?:\.\d+)+(?:[\w.\-+]*)", line)
            return m.group(0)[:32] if m else line[:32]
    except Exception:
        return None
    return None


def _dist_version(name: str) -> str | None:
    """Installed distribution version — metadata only, never imports the package."""
    try:
        import importlib.metadata as md
        return md.version(name)
    except Exception:
        return None


def collect_versions(engines: dict) -> dict:
    """Versions of the external backends each engine depends on.

    Integration failures across a version boundary are the dominant support cost for a tool that
    shells out to third-party binaries — "works on my machine" is unanswerable without knowing
    which serena or which graph backend the user has. Probed CONCURRENTLY and only for engines
    that reported installed, so the shallow doctor stays ~1 subprocess-round deep."""
    versions: dict[str, str | None] = {}
    try:
        from codeintel import __version__
        versions["codeintel"] = __version__
    except Exception:
        versions["codeintel"] = None

    def _installed(name: str) -> bool:
        e = engines.get(name)
        return isinstance(e, dict) and e.get("installed") is True

    jobs: dict[str, Callable[[], str | None]] = {}
    if _installed("graph"):
        jobs["codebase-memory-mcp"] = lambda: _cmd_version(["codebase-memory-mcp", "--version"])
    if _installed("lsp"):
        jobs["uv"] = lambda: _cmd_version(["uvx", "--version"])
        jobs["serena"] = lambda: _cmd_version(["serena", "--version"])
    if _installed("semantic"):
        jobs["fastembed"] = lambda: _dist_version("fastembed")
        jobs["sqlite-vec"] = lambda: _dist_version("sqlite-vec")

    if jobs:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = {name: pool.submit(fn) for name, fn in jobs.items()}
                for name, fut in futures.items():
                    try:
                        versions[name] = fut.result(timeout=_VERSION_TIMEOUT_S + 1.0)
                    except Exception:
                        versions[name] = None
        except Exception:
            pass
    return versions


def collect_registrations() -> list[dict]:
    """Which agents codeintel is registered with, and whether their launch line still works.

    Registering an ABSOLUTE path makes a GUI-launched host able to find the server, at the cost of
    one new failure mode: an upgrade or a rebuilt venv moves the binary and the config silently
    points at nothing. That failure is invisible from inside the agent — the host just reports a
    server that won't start — so doctor has to name it, with the one command that repairs it.

    Read-only and never-raise: stats a path, never launches anything."""
    out: list[dict] = []
    try:
        from codeintel.installer import _AGENTS, _CONFIG, registered_command
    except Exception:
        return out
    for agent in _AGENTS:
        try:
            spec = _CONFIG.get(agent) or {}
            path, command = registered_command(spec)
            if command is None:
                continue  # not registered with this agent — nothing to report
            runnable = bool(command) and os.path.isfile(command) and os.access(command, os.X_OK)
            if not os.path.isabs(command):
                # A bare name is resolved by the host's PATH, which we cannot see from here.
                runnable = shutil.which(command) is not None
            out.append({
                "agent": agent,
                "config": path,
                "command": command,
                "runnable": runnable,
                "remediation": None if runnable else (
                    f"`{command}` no longer exists — re-run `codeintel install --agent {agent}`"
                ),
            })
        except Exception:
            continue
    return out


# Which version keys belong to which engine, for the per-engine `versions` rollup.
_ENGINE_BACKENDS = {
    "graph": ("codebase-memory-mcp",),
    "lsp": ("serena", "uv"),
    "semantic": ("fastembed", "sqlite-vec"),
}


def run_doctor(
    project_root: Any,
    *,
    deep: bool = False,
    graph: Any = None,
    lsp: Any = None,
    semantic: Any = None,
    lsp_deep_timeout_s: float = 20.0,
    on_provider: Callable[[str, Any], Any] | None = None,
) -> dict:
    """Diagnose all three engines for ``project_root``. Never raises; bounded (~3s shallow).

    Pass live providers (e.g. the singleton gateway's) to reflect real warmed state; omit them
    for a hermetic check that builds fresh providers. When a slot is omitted this builds an
    ephemeral provider, so pass ``on_provider`` (e.g. ``gateway.adopt_provider``) to hand that
    instance back — without it, a caller's gateway keeps reporting an engine as present in the
    report while its own queries still cannot route to it."""
    try:
        root = os.path.abspath(str(project_root)) if project_root else os.getcwd()
    except Exception:
        root = str(project_root or "")

    engines: dict[str, dict] = {}
    try:
        from codeintel.providers.graph import GraphProvider
        engines["graph"] = _probe_engine(
            "graph", graph, GraphProvider, lambda p: p.probe(root), on_provider
        )
    except Exception:
        engines["graph"] = {"engine": "graph", "status": "fail", "installed": False,
                            "runnable": False, "repo_indexed": None,
                            "detail": "graph provider unavailable", "remediation": None}
    try:
        from codeintel.providers.lsp import LspProvider
        engines["lsp"] = _probe_engine(
            "lsp", lsp, LspProvider,
            lambda p: p.probe(root, deep=deep, timeout_s=lsp_deep_timeout_s), on_provider
        )
    except Exception:
        engines["lsp"] = {"engine": "lsp", "status": "fail", "installed": False,
                         "runnable": False, "repo_indexed": None,
                         "detail": "lsp provider unavailable", "remediation": None}
    try:
        from codeintel.providers.semantic import SemanticProvider
        engines["semantic"] = _probe_engine(
            "semantic", semantic, SemanticProvider, lambda p: p.probe(root), on_provider
        )
    except Exception:
        engines["semantic"] = {"engine": "semantic", "status": "fail", "installed": False,
                              "runnable": False, "repo_indexed": None,
                              "detail": "semantic provider unavailable", "remediation": None}

    # Is def-aligned (tree-sitter) chunking active for non-Python files? The semantic engine works
    # without it (line-window fallback) but SILENTLY — surface it so a broken/missing grammar pack
    # doesn't degrade chunking invisibly. find_spec (not import) keeps this cheap + never-load.
    try:
        import importlib.util
        treesitter = importlib.util.find_spec("tree_sitter_language_pack") is not None
    except Exception:
        treesitter = False

    versions = collect_versions(engines)
    # Roll each engine's backend versions onto its own probe so a caller reading a single engine
    # (code.status, the HTTP payload) sees what it is actually talking to.
    for name, keys in _ENGINE_BACKENDS.items():
        probe = engines.get(name)
        if isinstance(probe, dict):
            probe["versions"] = {k: versions.get(k) for k in keys if versions.get(k)}

    ready = sum(1 for e in engines.values() if e.get("status") != "fail")
    # "healthy" ignores OPTIONAL engines (graph): a repo with semantic + lsp ready is healthy even
    # without the external graph binary. `ready`/`total` stay literal (all three) for transparency.
    healthy = all(
        e.get("status") != "fail" for n, e in engines.items() if n not in _OPTIONAL_ENGINES
    )
    return {
        "ok": True,
        "project_root": root,
        "deep": bool(deep),
        "treesitter": treesitter,
        "versions": versions,
        "summary": {"ready": ready, "total": len(engines), "healthy": healthy},
        "engines": engines,
        "registrations": collect_registrations(),
    }


def render_doctor_text(report: dict) -> str:
    """Human-readable CLI rendering: a per-engine ✓/✗/▲ table + two-line `fix:` remediation.
    Styled via codeintel.term (color only on a TTY; width-safe glyphs so columns stay aligned)."""
    from codeintel.term import c  # imported at call time to honor the CLI's term.configure()

    _NAME, _INST, _RUN, _REPO = 10, 11, 10, 14
    root = report.get("project_root", "")
    engines = report.get("engines", {})
    out = [c.header("doctor", root), ""]
    out.append("  " + c.bold(
        "engine".ljust(_NAME) + " " + "installed".center(_INST) + " "
        + "runnable".center(_RUN) + " " + "repo-indexed".center(_REPO)
    ))
    out.append("  " + c.rule(_NAME) + " " + c.rule(_INST) + " " + c.rule(_RUN) + " " + c.rule(_REPO))

    def _state(value, na_ok=False):
        if value is True:
            return "ok"
        if value is False:
            return "fail"
        return "na" if na_ok else "warn"

    notes: list[tuple] = []
    for name in _ENGINES:
        e = engines.get(name, {})
        inst = c.status_cell(_state(e.get("installed")), _INST)
        run = c.status_cell(_state(e.get("runnable")), _RUN)
        repo = c.status_cell(_state(e.get("repo_indexed"), na_ok=True), _REPO)
        out.append("  " + name.ljust(_NAME) + " " + inst + " " + run + " " + repo)
        # Print the note whenever there is a REMEDIATION, not only when the row is non-ok. An
        # engine can be installed, runnable and indexed — status "ok", three green cells — and
        # still be answering from the wrong project, which is exactly the ancestor-match case.
        # Keying the note off the status cell meant the renderer computed that warning and then
        # dropped it, so the one command a user runs when confused stayed silent about the one
        # thing most likely to be confusing them.
        if e.get("status") != "ok" or e.get("remediation"):
            notes.append((name, e.get("detail", ""), e.get("remediation")))

    for name, detail, rem in notes:
        out.append("")
        out.append("  " + c.dim("└─") + " " + c.cyan(name) + ": " + detail)
        if rem:
            out.append("     " + c.bold(c.cyan("fix:")) + " " + rem)

    summ = report.get("summary", {})
    ready, total, healthy = summ.get("ready", "?"), summ.get("total", "?"), summ.get("healthy")
    count = c.bold(f"{ready} / {total}")
    count = c.red(count) if healthy is False else (c.green(count) if healthy else count)
    tail = "" if report.get("deep") else c.dim("  (run with --deep to boot-check serena)")
    out.append("")
    out.append(f"  {count} engines ready for this repo.{tail}")
    opt_down = [n for n in _ENGINES
                if n in _OPTIONAL_ENGINES and (engines.get(n) or {}).get("status") == "fail"]
    if opt_down:
        out.append("  " + c.dim(
            f"({', '.join(opt_down)} optional — an external backend; codeintel works without it)"
        ))
    if healthy is False:
        out.append("  " + c.dim(
            "tip: `codeintel setup --all` installs + indexes everything automatable in one command; "
            "each fix: line above has the per-engine command."
        ))
    # Backend versions: what codeintel is actually talking to. Printed as one dim line so an
    # integration bug report carries the version boundary without anyone having to ask for it.
    versions = report.get("versions") or {}
    shown = [f"{k} {v}" for k, v in versions.items() if v]
    if shown:
        out.append("  " + c.dim("backends: " + "  ".join(shown)))
    if report.get("treesitter") is False:
        out.append("  " + c.dim(
            "def-aligned chunking: OFF for non-Python — tree-sitter-language-pack not importable, "
            "so TS/JS/Go/Rust/… fall back to line windows. fix: pip install tree-sitter-language-pack"
        ))

    # Agent registrations. Only BROKEN ones are printed: a working launch line is not news, but a
    # config pointing at a binary that no longer exists is invisible from inside the agent — the
    # host just reports a server that will not start, with no hint that codeintel moved.
    broken = [r for r in (report.get("registrations") or []) if r.get("runnable") is False]
    for reg in broken:
        out.append("")
        out.append("  " + c.dim("└─") + " " + c.cyan(f"{reg['agent']} registration")
                   + f": stale launch command in {reg['config']}")
        out.append("     " + c.bold(c.cyan("fix:")) + " " + (reg.get("remediation") or ""))
    return "\n".join(out)
