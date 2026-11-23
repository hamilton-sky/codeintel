"""`codeintel prompt` — generate a paste-to-your-agent setup prompt, tailored to this machine.

`setup` DOES the bring-up and `install` REGISTERS the MCP server; this hands the same job to your
coding agent instead. It runs a doctor probe, sees which engines are actually missing and whether
this agent is already registered, and prints a natural-language prompt you copy into Claude Code /
Codex / etc. — telling it the exact remaining commands for THIS repo, to verify with `doctor`, and to
have you restart it so the MCP tools load. A static prompt would just repeat the README; reflecting
the live health is what makes it worth a command.

Two modes: the default reflects this machine (it drops steps already satisfied, and when everything
is healthy and registered it reduces to "just restart me"); `--fresh` ignores local state and emits
the full sequence from `pip install`, for pasting to a friend on a clean machine.
"""
from __future__ import annotations

from typing import Any

from codeintel.setup.installer import _AGENTS, detect_agents

# On PyPI the distribution is `codecortex` (the name `codeintel` was taken); the CLI and import stay
# `codeintel`. A pasted prompt has to name the installable, so this is the one place the CLI spells it.
_PYPI_PACKAGE = "codecortex"


def resolve_agent(agent: str) -> str:
    """Which agent the prompt targets. A named agent is honoured; `auto` picks the one installed on
    this machine (the first, if several); `""` when none is detected → the prompt says 'your agent'
    and omits the `--agent` flag rather than guessing a host you do not have."""
    a = (agent or "auto").strip().lower()
    if a in _AGENTS:
        return a
    detected = detect_agents()
    return detected[0] if detected else ""


def build_prompt(project_root: str, agent: str, report: dict[str, Any], *, fresh: bool = False) -> str:
    """The paste-ready prompt, tailored to what *report* (a doctor report) says is still missing.

    Pure and side-effect-free so a test can assert the tailoring without a live backend. With
    ``fresh=True`` the local state is ignored and every step is emitted."""
    engines = {} if fresh else (report.get("engines") or {})

    def _row(name: str) -> dict[str, Any]:
        row = engines.get(name)
        return row if isinstance(row, dict) else {}

    def _ok(name: str) -> bool:
        return _row(name).get("status") == "ok"

    def _installed(name: str) -> bool:
        # Doctor says `installed: True` for a package that is present but failing. A row without
        # the field (an older or partial report) keeps the status-based step.
        return _row(name).get("installed") is True

    graph_ok, lsp_ok, semantic_ok = _ok("graph"), _ok("lsp"), _ok("semantic")
    semantic_row = _row("semantic")
    # The package is installed and the last index could not download the embedding model:
    # reinstalling changes nothing, the network or a pre-seeded cache does.
    model_failed = semantic_row.get("reason") in ("model-load-failed", "index-failed")
    # serena is installed but has not booted yet: unverified, not missing.
    lsp_row = _row("lsp")
    lsp_unverified = (not lsp_ok and _installed("lsp") and lsp_row.get("status") == "warn"
                      and lsp_row.get("runnable") is None)
    healthy = (not fresh) and bool((report.get("summary") or {}).get("healthy"))
    registered = (not fresh) and any(
        r.get("agent") == agent and r.get("runnable")
        for r in (report.get("registrations") or [])
    )
    install_flag = f" --agent {agent}" if agent else ""
    tools = "code.query, code.status, code.doctor, code.map"

    unverified = (
        " The LSP engine is installed but unverified (serena boots on first use); "
        "`doctor --deep` boots it to check." if lsp_unverified else ""
    )

    # Already fully set up: the one thing an agent cannot do for itself is get restarted.
    if healthy and registered:
        return (
            f"codeintel is already installed and healthy for this project ({project_root}) and "
            f"registered as one of your MCP servers — nothing to install. Confirm with "
            f"`codeintel doctor --deep {project_root}` (expect \"ready 3/3, healthy\").{unverified} Then tell me "
            f"to restart you so your codeintel MCP tools ({tools}) load."
        )

    # Emit only the steps that are actually outstanding.
    steps: list[str] = []
    pip_targets: list[str] = []
    semantic_missing = not semantic_ok and not _installed("semantic") and not model_failed
    graph_missing = not graph_ok and not _installed("graph")
    if semantic_missing:
        pip_targets.append(_PYPI_PACKAGE)
    if graph_missing:
        pip_targets.append("'codebase-memory-mcp==0.9.*'")
    if pip_targets:
        adds = " and ".join(
            part for part in (
                "the codeintel CLI + semantic engine" if semantic_missing else "",
                "the graph engine" if graph_missing else "",
            ) if part
        )
        steps.append(f"`pip install {' '.join(pip_targets)}` — installs {adds}")
    if model_failed:
        remediation = str(semantic_row.get("remediation") or "")
        fix = remediation.split("; then:")[0].strip()
        if "FASTEMBED_CACHE_PATH" not in fix or "network" not in fix:
            fix = ("check network/proxy access to huggingface.co, or set FASTEMBED_CACHE_PATH to a "
                   "directory pre-seeded with the embedding model")
        steps.append(
            f"the semantic engine is installed, but the last index could not download its "
            f"embedding model, so do not reinstall it: {fix}"
        )
    # setup --all installs uv, indexes, and warms serena — needed unless all three engines are
    # already ok (in which case the repo is indexed too, and only registration can be outstanding).
    # An unverified LSP row counts as present: `doctor --deep` below boots it.
    if not (graph_ok and (lsp_ok or lsp_unverified) and semantic_ok):
        steps.append(
            f"`codeintel setup --all {project_root}` — installs uv (the LSP engine), indexes this "
            f"repo, and warms serena; idempotent, safe to re-run"
        )
    if not registered:
        steps.append(f"`codeintel install{install_flag}` — registers codeintel as my MCP server")

    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    return (
        f"Set up the codeintel code-intelligence tool for this project ({project_root}) and wire it "
        f"into me. Run these in order, showing me the output of each:\n\n{numbered}\n\n"
        f"Then run `codeintel doctor --deep {project_root}` and show me the result — I expect "
        f"\"ready 3/3, healthy\".{unverified} If any engine is not \"ok\", run the exact remediation `doctor` "
        f"prints for it and re-check. When it is healthy, tell me to restart you so my codeintel MCP "
        f"tools ({tools}) load."
    )


def run_prompt(project_root: str, agent: str = "auto", *, fresh: bool = False, deep: bool = False) -> str:
    """Probe (unless ``fresh``) and render the prompt. Never raises here — the command wrapper also
    guards, but keeping the probe optional means `--fresh` needs no backend at all."""
    report: dict[str, Any] = {}
    if not fresh:
        from codeintel.setup import doctor as _doctor
        report = _doctor.run_doctor(project_root, deep=deep)
    return build_prompt(project_root, resolve_agent(agent), report, fresh=fresh)
