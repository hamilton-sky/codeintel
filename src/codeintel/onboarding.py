"""First-run setup — turns doctor's diagnosis into optional, consent-gated action.

Every side effect (install uv, install deps, index, warm lsp) is gated by an explicit flag —
the flag IS the consent, no interactive prompt. Never-raise and bounded: pip installs and
indexing carry timeouts; warming lsp reuses the one deep `doctor.run_doctor` boot rather than
booting serena twice. Stdout stays clean for a future --json; progress goes to `out` (stderr).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

from codeintel import doctor

_ENGINES = ("graph", "lsp", "semantic")


def _guidance_for(engine: str, probe: dict) -> str:
    """One-line install instructions for an engine that is not installed."""
    try:
        if engine == "graph":
            return ("install codebase-memory-mcp (a platform-specific binary) and ensure "
                    "it's on PATH — see its project; then `codeintel index`")
        if engine == "lsp":
            return "install uv (provides uvx): pip install uv — serena is fetched on first use"
        if engine == "semantic":
            return "pip install fastembed sqlite-vec  (or: pip install -e .)"
        return str(probe.get("remediation") or "engine unavailable")
    except Exception:
        return "engine unavailable"


def _pip_install(pkg_args: list[str], *, timeout_s: float = 300.0, out=sys.stderr) -> dict:
    """Run ``pip install <pkg_args>`` against THIS interpreter. Never raises; bounded."""
    try:
        print(f"  installing: pip install {' '.join(pkg_args)}", file=out)
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", *pkg_args],
            capture_output=True, timeout=timeout_s, text=True,
        )
        if proc.returncode == 0:
            return {"ok": True, "detail": f"installed: {' '.join(pkg_args)}"}
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"ok": False, "detail": tail[-1] if tail else f"pip exited {proc.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"pip install timed out after {timeout_s:.0f}s"}
    except Exception as exc:
        return {"ok": False, "detail": f"pip install failed ({type(exc).__name__})"}


def _bounded_index(project_root: str, *, timeout_s: float, out) -> dict:
    """Semantic-index on a joined daemon thread — never blocks past ``timeout_s``."""
    try:
        from codeintel.config import load_config
        from codeintel.indexer import Indexer
        from codeintel.semantic_db import SemanticDb, default_db_path
    except Exception as exc:
        return {"status": "fail", "chunks": 0, "detail": f"semantic deps unavailable ({type(exc).__name__})"}
    outcome: dict = {}

    def _work() -> None:
        try:
            cfg = load_config(project_root)
            db_path = default_db_path()
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db = SemanticDb(db_path)
            try:
                db.init()
                outcome["count"] = Indexer(
                    db, model_name=str(cfg.get("model") or "BAAI/bge-small-en-v1.5"),
                    window=int(cfg.get("window", 20)), stride=int(cfg.get("stride", 10)),
                    max_chunks=int(cfg.get("max_chunks", 500)),
                ).index(project_root)
            finally:
                db.close()
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        # The worker is a daemon thread — it is abandoned when this one-shot process exits, so
        # it is NOT durably "still running". Point the user at `codeintel index` (no timeout).
        return {"status": "timeout", "chunks": 0,
                "detail": f"indexing exceeded {timeout_s:.0f}s and was abandoned — run "
                          f"`codeintel index` directly for a large repo (no timeout)"}
    if "error" in outcome:
        return {"status": "fail", "chunks": 0, "detail": outcome["error"]}
    count = outcome.get("count", 0)
    if count < 0:
        return {"status": "fail", "chunks": 0, "detail": "indexer reported an unrecoverable failure"}
    return {"status": "ok", "chunks": count, "detail": f"indexed {count} new chunk(s)"}


def run_setup(
    project_root: str,
    *,
    install_uv: bool = False,
    install_deps: bool = False,
    do_index: bool = False,
    warm_lsp: bool = False,
    index_timeout_s: float = 900.0,
    lsp_warm_timeout_s: float = 90.0,
    out=sys.stderr,
) -> dict:
    """Diagnose + (opt-in) fix a repo's codeintel setup. Never raises; each flag IS consent."""
    steps: list[dict] = []

    def _step(name: str, status: str, detail: str = "") -> None:
        steps.append({"name": name, "status": status, "detail": detail})

    def _empty_doctor() -> dict:
        return {"ok": False, "project_root": root, "engines": {}, "summary": {"ready": 0, "total": 3, "healthy": False}}

    try:
        root = os.path.abspath(str(project_root)) if project_root else os.getcwd()
    except Exception:
        root = str(project_root or "")

    try:
        if warm_lsp:
            print("  first serena launch fetches it via uvx; this can be slow the first time…", file=out)
        try:
            report0 = doctor.run_doctor(root, deep=warm_lsp, lsp_deep_timeout_s=lsp_warm_timeout_s)
            engines0 = report0.get("engines", {}) if isinstance(report0, dict) else {}
        except Exception as exc:
            engines0 = {}
            _step("preflight", "fail", f"doctor check failed ({type(exc).__name__})")
        for name in _ENGINES:
            probe = engines0.get(name) or {}
            if probe.get("installed") is False:
                _step(f"{name}: preflight", probe.get("status", "fail"), _guidance_for(name, probe))

        for flag, pkg_args, step_name in (
            (install_uv, ["uv"], "install uv"),
            (install_deps, ["-e", "."], "install deps (-e .)"),
        ):
            if flag:
                r = _pip_install(pkg_args, out=out)
                _step(step_name, "ok" if r["ok"] else "fail", r["detail"])

        if do_index:
            print("  first index downloads the embedding model (~50MB, one-time); this may take a minute…", file=out)
            idx = _bounded_index(root, timeout_s=index_timeout_s, out=out)
            idx_status = {"ok": "ok", "timeout": "warn"}.get(idx.get("status"), "fail")
            _step("index: semantic", idx_status, idx.get("detail", ""))
            try:
                from codeintel.reindexer import Reindexer
                Reindexer()._graph_reindex(root)
                _step("index: graph", "ok", "best-effort graph reindex attempted")
            except Exception as exc:
                _step("index: graph", "warn", f"graph reindex skipped ({type(exc).__name__})")

        if warm_lsp:
            lsp = engines0.get("lsp") or {}
            _step("warm lsp", lsp.get("status", "warn"), lsp.get("detail", ""))

        try:
            final_doctor = doctor.run_doctor(root)
        except Exception:
            final_doctor = _empty_doctor()
        return {"ok": True, "project_root": root, "steps": steps, "doctor": final_doctor}
    except Exception as exc:
        return {"ok": False, "project_root": root, "steps": steps, "doctor": _empty_doctor(),
                "detail": f"setup failed ({type(exc).__name__})"}


def render_setup_text(report: dict) -> str:
    """Human CLI view: a ``[n/N]`` step list, the doctor table, then the overall summary."""
    from codeintel.term import c  # imported at call time so the CLI's term.configure() is honored

    try:
        root = report.get("project_root", "")
        steps = report.get("steps") or []
        n = len(steps)
        lines = [c.header("setup", root), ""]
        for i, step in enumerate(steps, start=1):
            detail = step.get("detail")
            tail = f"  {c.dim(str(detail))}" if detail else ""
            name = c.bold(str(step.get("name", "")))
            lines.append(f"  [{i}/{n}] {c.glyph(step.get('status', 'na'))} {name}{tail}")
        if not steps:
            lines.append(c.dim("  (nothing to do — no opt-in flags set)"))
        lines.append("")
        try:
            from codeintel.doctor import render_doctor_text
            lines.append(render_doctor_text(report.get("doctor") or {}))
        except Exception:
            lines.append(c.dim("(doctor report unavailable)"))
        lines.append("")
        lines.append(c.green("setup finished") if report.get("ok") else c.red("setup did not complete cleanly"))
        return "\n".join(lines)
    except Exception:
        return "codeintel setup — (error rendering report)"
