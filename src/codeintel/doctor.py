"""Preflight diagnostics — turn the tool's silent safe-null degradation into a clear signal.

`run_doctor` asks each engine three questions — installed? runnable? is THIS repo indexed? —
with a one-line remediation per gap. It is never-raise and bounded: no engine check may hang,
crash, load the embedding model, mutate state, or go through the gateway (no reindex side
effects). The same report drives the CLI `doctor` command, the `code.doctor` MCP tool, and HTTP.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

_ENGINES = ("graph", "lsp", "semantic")


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
) -> dict:
    """Run one engine's probe, never raising. Uses the passed-in (live) provider when given,
    else builds an ephemeral one."""
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
    r["engine"] = engine
    r["status"] = _status_for(r)
    return r


def run_doctor(
    project_root: Any,
    *,
    deep: bool = False,
    graph: Any = None,
    lsp: Any = None,
    semantic: Any = None,
    lsp_deep_timeout_s: float = 20.0,
) -> dict:
    """Diagnose all three engines for ``project_root``. Never raises; bounded (~3s shallow).

    Pass live providers (e.g. the singleton gateway's) to reflect real warmed state; omit them
    for a hermetic check that builds fresh providers."""
    try:
        root = os.path.abspath(str(project_root)) if project_root else os.getcwd()
    except Exception:
        root = str(project_root or "")

    engines: dict[str, dict] = {}
    try:
        from codeintel.providers.graph import GraphProvider
        engines["graph"] = _probe_engine(
            "graph", graph, GraphProvider, lambda p: p.probe(root)
        )
    except Exception:
        engines["graph"] = {"engine": "graph", "status": "fail", "installed": False,
                            "runnable": False, "repo_indexed": None,
                            "detail": "graph provider unavailable", "remediation": None}
    try:
        from codeintel.providers.lsp import LspProvider
        engines["lsp"] = _probe_engine(
            "lsp", lsp, LspProvider, lambda p: p.probe(root, deep=deep, timeout_s=lsp_deep_timeout_s)
        )
    except Exception:
        engines["lsp"] = {"engine": "lsp", "status": "fail", "installed": False,
                         "runnable": False, "repo_indexed": None,
                         "detail": "lsp provider unavailable", "remediation": None}
    try:
        from codeintel.providers.semantic import SemanticProvider
        engines["semantic"] = _probe_engine(
            "semantic", semantic, SemanticProvider, lambda p: p.probe(root)
        )
    except Exception:
        engines["semantic"] = {"engine": "semantic", "status": "fail", "installed": False,
                              "runnable": False, "repo_indexed": None,
                              "detail": "semantic provider unavailable", "remediation": None}

    ready = sum(1 for e in engines.values() if e.get("status") != "fail")
    return {
        "ok": True,
        "project_root": root,
        "deep": bool(deep),
        "summary": {"ready": ready, "total": len(engines),
                    "healthy": all(e.get("status") != "fail" for e in engines.values())},
        "engines": engines,
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
        if e.get("status") != "ok":
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
    return "\n".join(out)
