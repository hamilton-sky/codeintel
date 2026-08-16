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

    print("\n  Start a new agent session to pick up the tools.")
    return 0 if any_ok else 1
