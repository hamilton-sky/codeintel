"""`codeintel install` — register codeintel with the AI agents installed on this machine."""

from typing import Any

from codeintel.commands._common import never_raise


def _register(installer: Any, agent: str, *, verify: bool, absolute: bool) -> tuple[list, list]:
    """(results, skipped) for the requested agent selection. Only `auto` reports skips — `all` and
    a named agent register unconditionally, so there is nothing to have skipped."""
    if agent == "auto":
        return installer.register_detected(verify=verify, absolute=absolute)
    if agent == "all":
        return installer.register_all(verify=verify, absolute=absolute), []
    return installer.register_many([agent], verify=verify, absolute=absolute), []


def _agents_for(agent: str, installer_mod: Any) -> tuple[list[str], list[str]]:
    """(agents, skipped) for the requested `--agent` selection. Mirrors `_register`'s selection
    logic for `--dry-run`, which never constructs an `Installer` — nothing should be written."""
    if agent == "auto":
        detected = installer_mod.detect_agents()
        return detected, [a for a in installer_mod._AGENTS if a not in detected]
    if agent == "all":
        return list(installer_mod._AGENTS), []
    return [agent], []


def _dry_run_line(installer_mod: Any, agent: str, command: str) -> str:
    """What `--dry-run` would do to one agent's config — read-only, via the same lookup
    `codeintel doctor` already uses to notice a stale registration."""
    spec = installer_mod._CONFIG[agent]
    path, current = installer_mod.registered_command(spec)
    if current == command:
        return f"~ {agent}: already registered at {path} (no change)"
    if current is None:
        return f"+ {agent}: would register at {path}"
    return f"+ {agent}: would update {path} (was: {current})"


def _run_dry(args: Any) -> int:
    from codeintel import installer as installer_mod

    command = installer_mod.resolve_command(absolute=not args.relative_command)
    agents, skipped = _agents_for(args.agent, installer_mod)

    if args.agent == "auto" and not agents:
        print("No supported agent found on this machine "
              f"(looked for: {', '.join(skipped)}).")
        print("  Install one, or force registration with "
              "`codeintel install --agent <name>`.")
        return 1

    for agent in agents:
        print(_dry_run_line(installer_mod, agent, command))

    if skipped:
        print(f"\n- skipped (not installed here): {', '.join(skipped)}"
              f"\n  register anyway with `codeintel install --agent <name>`")

    print("\nDry run — nothing was written.")
    return 0


def _report(results: list) -> tuple[bool, list, Any]:
    """Print one line per agent; return (any_ok, legacy_paths, verdict)."""
    any_ok = False
    legacy_paths: list[str] = []
    verdict = None
    for r in results:
        agent, path, action = r["agent"], r["path"], r["action"]
        if action == "registered":
            print(f"v {agent}: registered at {path}")
            any_ok = True
        elif action == "already":
            print(f"~ {agent}: already registered at {path}")
            any_ok = True
        else:
            print(f"x {agent}: failed — {r['reason']}")
        if r.get("legacy"):
            legacy_paths.append(r["legacy"])
        verdict = r.get("verified") or verdict
    return any_ok, legacy_paths, verdict


@never_raise("install failed: {exc}", code=1)
def run(args: Any) -> int:
    if getattr(args, "dry_run", False):
        return _run_dry(args)

    from codeintel.installer import Installer

    results, skipped = _register(
        Installer(), args.agent, verify=not args.no_verify, absolute=not args.relative_command
    )
    if args.agent == "auto" and not results:
        print("No supported agent found on this machine "
              f"(looked for: {', '.join(skipped)}).")
        print("  Install one, or force registration with "
              "`codeintel install --agent <name>`.")
        return 1

    any_ok, legacy_paths, verdict = _report(results)

    # A written config file proves nothing about whether the host can launch the server — so say
    # what the handshake actually found, and fail loudly when it did not happen.
    if verdict is not None:
        if verdict.get("ok"):
            print(f"\nv verified: {verdict.get('detail', '')}")
        else:
            print(f"\nx NOT verified: {verdict.get('detail', '')}")
            print("  The config was written, but your agent will not be able to use it "
                  "until this is resolved.")
            any_ok = False

    for legacy in dict.fromkeys(legacy_paths):
        print(f"\n! stale entry: {legacy} has an `mcpServers.codeintel` block that this host "
              f"does NOT read (an older codeintel wrote it). Safe to delete by hand.")

    # Name what was skipped: silence about an untouched host reads as "unsupported" rather than
    # "you don't have it installed".
    if skipped:
        print(f"\n- skipped (not installed here): {', '.join(skipped)}"
              f"\n  register anyway with `codeintel install --agent <name>`")

    # An installed, indexed, registered server does nothing until the agent actually knows to
    # reach for it — and "Start a new agent session" never mentions that. Only offered when at
    # least one agent came out of this run genuinely usable (registered/already AND, when
    # verification ran, verified) — `any_ok` is False here after a failed verify above, same
    # signal the exit code already uses. `offer_injection` is itself consent-gated: it prompts
    # only on a TTY, and off one it prints the command instead of guessing — never a silent write.
    if any_ok:
        from codeintel.injector import offer_injection
        print()
        offer_injection()

    print("\n  Start a new agent session to pick up the tools.")
    return 0 if any_ok else 1
