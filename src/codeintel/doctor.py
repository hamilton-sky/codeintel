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
    """Human-readable CLI rendering: a per-engine ✓/✗/⚠ table + remediation lines."""
    root = report.get("project_root", "")
    engines = report.get("engines", {})
    out = [f"codeintel doctor  —  {root}", ""]
    out.append("  {:<10} {:^11} {:^9} {:^13}".format("engine", "installed", "runnable", "repo-indexed"))
    out.append("  " + "─" * 10 + " " + "─" * 11 + " " + "─" * 9 + " " + "─" * 13)

    notes: list[str] = []
    for name in _ENGINES:
        e = engines.get(name, {})
        inst = "✓" if e.get("installed") else "✗"
        run = {True: "✓", False: "✗", None: "⚠"}.get(e.get("runnable"), "?")
        ri = e.get("repo_indexed")
        ri_s = "n/a" if ri is None else ("✓" if ri else "✗")
        out.append("  {:<10} {:^11} {:^9} {:^13}".format(name, inst, run, ri_s))
        if e.get("status") != "ok":
            note = f"    └─ {name}: {e.get('detail', '')}"
            rem = e.get("remediation")
            if rem:
                note += f"  →  {rem}"
            notes.append(note)

    if notes:
        out.append("")
        out.extend(notes)

    summ = report.get("summary", {})
    out.append("")
    tail = "" if report.get("deep") else "  (run with --deep to boot-check serena)"
    out.append(f"  {summ.get('ready', '?')} / {summ.get('total', '?')} engines ready for this repo.{tail}")
    return "\n".join(out)
