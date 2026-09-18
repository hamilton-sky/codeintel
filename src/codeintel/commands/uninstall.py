"""`codeintel uninstall` — remove codeintel's entry from the agent configs `install` wrote.

The inverse of `install`, and deliberately only that. It does NOT delete the semantic index: that
is `reset`'s job, it is expensive to rebuild (ten minutes on a large repo), and a command whose
name says "uninstall" quietly destroying ten minutes of work is the kind of surprise this project
spends most of its comments avoiding. What the index costs and how to remove it is printed instead,
which is the same rule every other disclosure here follows — name what was NOT done.

There is no confirmation prompt, and that is a considered difference from `reset --all`. This edits
one entry in a config file and `codeintel install` puts it straight back; `reset` destroys an index
that has to be re-embedded. `--dry-run` covers the reader who wants to look first.
"""

from typing import Any

from codeintel.commands._common import never_raise


def _targets(agent: str, installer_mod: Any) -> tuple[list[str], list[str]]:
    """`(agents, untouched)` for the requested `--agent` selection.

    `auto` means "wherever codeintel is actually registered", not "agents installed on this
    machine" — the question `install` asks. They differ in both directions that matter: an agent
    uninstalled from this machine can still hold our entry, and an agent that is present but never
    registered has nothing to remove and should not be reported as though it did.
    """
    if agent == "auto":
        registered = installer_mod.registered_agents()
        return registered, [a for a in installer_mod._AGENTS if a not in registered]
    if agent == "all":
        return list(installer_mod._AGENTS), []
    return [agent], []


def _dry_run_line(installer_mod: Any, agent: str) -> str:
    """What `--dry-run` would do to one agent's config — read-only, through the same lookup
    `codeintel doctor` uses to notice a registration at all."""
    spec = installer_mod._CONFIG[agent]
    path, current = installer_mod.registered_command(spec)
    if current is None:
        return f"~ {agent}: no codeintel entry in {path} (no change)"
    return f"- {agent}: would remove the codeintel entry from {path} (command: {current})"


def _report(results: list) -> tuple[bool, bool, list[str]]:
    """Print one line per agent; return (removed_any, failed_any, legacy_paths)."""
    removed_any = failed_any = False
    legacy_paths: list[str] = []
    for r in results:
        agent, path, action = r["agent"], r["path"], r["action"]
        if action == "removed":
            print(f"v {agent}: removed from {path}")
            removed_any = True
        elif action == "absent":
            print(f"~ {agent}: nothing registered at {path}")
        else:
            print(f"x {agent}: failed — {r['reason']}")
            failed_any = True
        if r.get("legacy"):
            legacy_paths.append(r["legacy"])
    return removed_any, failed_any, legacy_paths


@never_raise("uninstall failed: {exc}", code=1)
def run(args: Any) -> int:
    from codeintel import installer as installer_mod

    agents, untouched = _targets(args.agent, installer_mod)

    if args.agent == "auto" and not agents:
        # Exit 0, not 1. The requested end state — codeintel is not registered with anything —
        # already holds, and reporting "failure" for a goal that is met would make this command
        # unusable in a teardown script that runs whether or not install ever ran.
        print("codeintel is not registered with any agent on this machine — nothing to remove.")
        print(f"  (looked at: {', '.join(untouched)})")
        return 0

    if getattr(args, "dry_run", False):
        for agent in agents:
            print(_dry_run_line(installer_mod, agent))
        print("\nDry run — nothing was changed.")
        return 0

    results = installer_mod.Installer().unregister_many(agents)
    removed_any, failed_any, legacy_paths = _report(results)

    for legacy in dict.fromkeys(legacy_paths):
        print(f"\n! stale entry: {legacy} has an `mcpServers.codeintel` block that this host does "
              f"NOT read (an older codeintel wrote it). It is inert, and safe to delete by hand.")

    if args.agent == "auto" and untouched:
        print(f"\n- untouched (nothing registered): {', '.join(untouched)}")

    if removed_any:
        # A host that is already running keeps the server it launched at session start, so the
        # tools stay on screen until it is restarted — the mirror of the note `install` prints,
        # and the same surprise in the other direction.
        print("\nRestart your agent (or start a new session) — a running host does not reload "
              "its MCP config, so codeintel's tools stay visible until it does.")

    # Named, not done. The index is the expensive artifact and this command does not own it.
    from codeintel.paths import codeintel_home
    print(f"\nThe index is untouched: {codeintel_home()}")
    print("  Remove it with `codeintel reset --all` if you are done with codeintel entirely.")

    return 1 if failed_any else 0
