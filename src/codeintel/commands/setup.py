"""`codeintel setup` — prepare backends and optionally index this repo."""

from typing import Any

from codeintel.commands._common import emit, never_raise, require_dir, resolve_root


@never_raise("setup unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel import onboarding

    project_root = resolve_root(args)
    problem = require_dir(project_root, "setup")
    if problem:
        print(problem)
        return 1
    all_steps = getattr(args, "all_steps", False)  # --all implies every automatable step
    report = onboarding.run_setup(
        project_root,
        install_uv=args.install_uv or all_steps,
        install_deps=args.install_deps or all_steps,
        do_index=args.index or all_steps,
        warm_lsp=args.warm or all_steps,
    )
    emit(report, as_json=args.json, render=onboarding.render_setup_text)
    return 0 if report.get("doctor", {}).get("summary", {}).get("healthy") else 1
