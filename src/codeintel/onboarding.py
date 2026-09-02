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
            # codebase-memory-mcp IS pip-installable — confirmed against the PyPI JSON API: the
            # wheel is a thin (~5KB) wrapper whose own README documents `pip install
            # codebase-memory-mcp`, which then fetches the native runtime from GitHub Releases on
            # first run. A standalone native binary also exists for people who don't want a Python
            # launcher, but pip is not a fiction here — it is upstream's own primary install path,
            # the same shape as `pip install uv`/`fastembed` above. Frame it as the OPTIONAL add-on
            # it is: the tool is fully usable on semantic + LSP without it.
            return ("OPTIONAL (adds who-calls / impact / hotspots / changed): "
                    "pip install 'codebase-memory-mcp==0.9.*' (0.10.x is not yet supported — see "
                    "docs/graph.md), then re-run setup — codeintel works without it")
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
            db_path = default_db_path(str(cfg.get("model") or ""))
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db = SemanticDb(db_path)
            try:
                db.init()
                indexer = Indexer(
                    db, model_name=str(cfg.get("model") or "BAAI/bge-small-en-v1.5"),
                    window=int(cfg.get("window", 20)), stride=int(cfg.get("stride", 10)),
                    max_chunks=int(cfg.get("max_chunks", 500)),
                    max_total_chunks=int(cfg.get("max_total_chunks", 100000)),
                    chunk_strategy=str(cfg.get("chunk_strategy", "syntax")),
                )
                outcome["count"] = indexer.index(project_root)
                if indexer.last_error:
                    outcome["reason"] = indexer.last_error
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
        # Name the cause here. "an unrecoverable failure" told the user only that something went
        # wrong, while the actual reason — a blocked model download, an unwritable cache directory
        # — sat in a stderr line above, unlinked to this row and easy to scroll past.
        reason = str(outcome.get("reason") or "").strip()
        return {"status": "fail", "chunks": 0,
                "detail": (f"indexing failed — {reason}" if reason
                           else "indexer reported an unrecoverable failure (run `codeintel index "
                                "<repo>` to see the error)")}
    return {"status": "ok", "chunks": count, "detail": f"indexed {count} new chunk(s)"}


def run_setup(
    project_root: str,
    *,
    install_uv: bool = False,
    install_deps: bool = False,
    do_index: bool = False,
    warm_lsp: bool = False,
    fix_languages: bool = False,
    index_timeout_s: float = 900.0,
    lsp_warm_timeout_s: float = 90.0,
    out=sys.stderr,
) -> dict:
    """Diagnose + (opt-in) fix a repo's codeintel setup. Never raises; each flag IS consent."""
    steps: list[dict] = []

    def _step(name: str, status: str, detail: str = "", *, action: bool | None = None) -> None:
        # `action` records whether this step actually CHANGED something. Every other step here only
        # exists when its flag was passed, so its mere presence implies an action and the renderer can
        # match on the name. The languages step is the exception: it runs unconditionally so a dry run
        # can report the gap, so presence proves nothing and it must say so explicitly. Without this,
        # a bare `codeintel setup` would suppress the "(diagnose only — run --all)" hint that tells a
        # new user how to fix anything.
        steps.append({"name": name, "status": status, "detail": detail, "action": action})

    def _empty_doctor() -> dict:
        return {"ok": False, "project_root": root, "engines": {}, "summary": {"ready": 0, "total": 3, "healthy": False}}

    try:
        root = os.path.abspath(str(project_root)) if project_root else os.getcwd()
    except Exception:
        root = str(project_root or "")

    try:
        try:
            # Shallow diagnose only — the warm/deep serena boot happens AFTER the install loop (below)
            # so a freshly-installed uv is visible to it (see the warm_lsp block).
            report0 = doctor.run_doctor(root, deep=False)
            engines0 = report0.get("engines", {}) if isinstance(report0, dict) else {}
        except Exception as exc:
            engines0 = {}
            _step("preflight", "fail", f"doctor check failed ({type(exc).__name__})")
        for name in _ENGINES:
            probe = engines0.get(name) or {}
            if probe.get("installed") is False:
                _step(f"{name}: preflight", probe.get("status", "fail"), _guidance_for(name, probe))

        # Idempotent installs: skip when the engine already reports installed, so `--all` and re-runs
        # don't reinstall (lsp installed ⇒ uv/uvx present; semantic installed ⇒ deps present).
        lsp_installed = (engines0.get("lsp") or {}).get("installed") is True
        semantic_installed = (engines0.get("semantic") or {}).get("installed") is True
        for flag, pkg_args, step_name, already in (
            (install_uv, ["uv"], "install uv", lsp_installed),
            # NOT `["-e", "."]`: that editable-installs whatever project the user happens to be
            # standing in when they run `codeintel setup --all` — with no `cwd=` on the subprocess,
            # `_pip_install` inherits wherever the shell was, which in a random repo means
            # editable-installing THAT repo, not codeintel. Install the actual semantic deps by
            # name instead, same as the `pip install fastembed sqlite-vec` guidance above.
            (install_deps, ["fastembed", "sqlite-vec"], "install deps (fastembed + sqlite-vec)",
             semantic_installed),
        ):
            if flag:
                if already:
                    _step(step_name, "ok", "already satisfied — skipped")
                    continue
                r = _pip_install(pkg_args, out=out)
                _step(step_name, "ok" if r["ok"] else "fail", r["detail"])

        if do_index:
            print("  first index downloads the embedding model (~50MB, one-time); this may take a minute…", file=out)
            idx = _bounded_index(root, timeout_s=index_timeout_s, out=out)
            idx_status = {"ok": "ok", "timeout": "warn"}.get(str(idx.get("status") or ""), "fail")
            _step("index: semantic", idx_status, idx.get("detail", ""))
            try:
                from codeintel.reindexer import Reindexer
                Reindexer()._graph_reindex(root)
                _step("index: graph", "ok", "best-effort graph reindex attempted")
            except Exception as exc:
                _step("index: graph", "warn", f"graph reindex skipped ({type(exc).__name__})")

        if warm_lsp:
            # Warm with a FRESH deep probe AFTER the install loop: on a fresh machine uv was just
            # installed above, so the pre-install preflight would report lsp missing — emitting a
            # stale, self-contradictory "warm lsp: fail" under "install uv: ok". Re-probe so the boot
            # actually runs against the now-present uvx.
            print("  first serena launch fetches it via uvx; this can be slow the first time…", file=out)
            try:
                from codeintel.providers.lsp import LspProvider
                wl = LspProvider().probe(root, deep=True, timeout_s=lsp_warm_timeout_s)
                _step("warm lsp", doctor._status_for({**wl, "engine": "lsp"}), wl.get("detail", ""))
            except Exception as exc:
                _step("warm lsp", "warn", f"warm attempt failed ({type(exc).__name__})")

        # BEFORE the final doctor, so the report reflects the repaired config rather than the one the
        # run started with — and AFTER the warm step, which is what creates `.serena/project.yml` on a
        # repo serena has never seen. Ordered that way deliberately: on a fresh machine
        # `--all` would otherwise plan against a file that did not exist yet and report
        # `no-serena-config` for a config it was about to create.
        #
        # The step runs even without consent, because a dry run is information and silence is not: it
        # says what it WOULD change and names the flag. Only the write is gated.
        try:
            from codeintel import lang_config

            plan = lang_config.apply_plan(root, apply=bool(fix_languages))
            if plan.get("problem"):
                status = "warn"
            elif plan.get("applied"):
                status = "ok" if plan.get("verified") is not False else "warn"
            elif plan.get("additions"):
                status = "warn"          # a real gap, unrepaired — do not report it as fine
            else:
                status = "ok"
            _step("languages: serena language_servers", status, lang_config.describe(plan),
                  action=bool(plan.get("applied")))
        except Exception as exc:
            _step("languages: serena language_servers", "warn",
                  f"could not check language coverage ({type(exc).__name__})", action=False)

        try:
            final_doctor = doctor.run_doctor(root)
        except Exception:
            final_doctor = _empty_doctor()
        return {"ok": True, "project_root": root, "steps": steps, "doctor": final_doctor}
    except Exception as exc:
        return {"ok": False, "project_root": root, "steps": steps, "doctor": _empty_doctor(),
                "detail": f"setup failed ({type(exc).__name__})"}


def _next_steps(doctor_report: dict, root: str) -> list[str]:
    """The crisp 'what's left' list after a setup pass, computed from the final doctor state —
    so a user knows the ONE remaining action instead of parsing the engine table."""
    try:
        out: list[str] = []
        engines = doctor_report.get("engines") if isinstance(doctor_report, dict) else None
        engines = engines if isinstance(engines, dict) else {}
        def _get(name: str) -> dict:
            entry = engines.get(name)
            return entry if isinstance(entry, dict) else {}

        sem, lsp, graph = _get("semantic"), _get("lsp"), _get("graph")
        if sem.get("installed") is not True:
            out.append("Semantic engine deps missing — pip install fastembed sqlite-vec")
        elif sem.get("repo_indexed") is False:
            out.append(f"Index this repo — codeintel index {root}")
        if lsp.get("installed") is not True:
            out.append("LSP engine — pip install uv  (serena auto-fetched on first use)")
        if graph.get("installed") is not True:
            out.append("Graph engine (OPTIONAL — who-calls/impact/hotspots/changed) — "
                       "pip install 'codebase-memory-mcp==0.9.*'")
        # Always the last mile: an installed+indexed tool does nothing until the agent knows about it.
        out.append("Make your AI agent use it — codeintel install")
        return out
    except Exception:
        return []


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
        _ACTION_STEPS = {"install uv", "install deps (fastembed + sqlite-vec)", "index: semantic",
                        "index: graph", "warm lsp"}

        def _did_something(step: dict) -> bool:
            explicit = step.get("action")
            return bool(explicit) if explicit is not None else step.get("name") in _ACTION_STEPS

        if not any(_did_something(s) for s in steps):
            lines.append(c.dim("  (diagnose only — run `codeintel setup --all` to install + index "
                               "everything automatically)"))
        lines.append("")
        try:
            from codeintel.doctor import render_doctor_text
            lines.append(render_doctor_text(report.get("doctor") or {}))
        except Exception:
            lines.append(c.dim("(doctor report unavailable)"))

        nexts = _next_steps(report.get("doctor") or {}, root)
        if nexts:
            lines.append("")
            lines.append(c.bold("Next:"))
            lines.extend("  " + c.cyan("→") + " " + step for step in nexts)
            lines.append("  " + c.dim("or: `codeintel prompt` prints these as a paste-to-your-agent "
                                      "block (Claude Code / Codex / …)"))

        lines.append("")
        lines.append(c.green("setup finished") if report.get("ok") else c.red("setup did not complete cleanly"))
        return "\n".join(lines)
    except Exception:
        return "codeintel setup — (error rendering report)"
