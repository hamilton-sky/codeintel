import argparse
import difflib
import sys
from importlib import import_module

from codeintel import __version__

# Commands grouped by what you are trying to DO. argparse lists them in declaration order with no
# grouping, which turns "what can this thing do?" into reading twelve lines to find the one verb you
# wanted. Each entry is (command, one-line description).
_COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Understand your code", [
        ("query", "Ask one question — changed, impact, callers, chain, search"),
        ("map", "Write CODE_INTEL.md — a committable architecture overview"),
        ("graph", "Call graph of functions — self-contained HTML, no install"),
        ("c4", "Architecture map of files/folders — LikeC4 source, needs Node"),
    ]),
    ("Set up", [
        ("setup", "Prepare backends + index this repo (--all does it all)"),
        ("index", "Build the index every other command reads (semantic + graph)"),
        ("install", "Register codeintel with the AI agents on this machine"),
        ("prompt", "Print a paste-to-your-agent setup prompt for this machine"),
    ]),
    ("Check health", [
        ("doctor", "Per-engine health + index status, with the fix for each gap"),
        ("status", "Engine readiness and index age at a glance"),
        ("reset", "Clear the semantic index (recover from a corrupt or stale DB)"),
    ]),
    ("Run as a server", [
        ("serve", "Start the MCP server over stdio — what an agent host launches"),
        ("serve-http", "Start the HTTP transport (loopback only unless --allow-remote)"),
        ("gen-token", "Print a secure random bearer token for serve-http / RBAC"),
    ]),
]

_COMMANDS = [name for _group, items in _COMMAND_GROUPS for name, _desc in items]

# Each command's body lives in codeintel.commands.<module> as `run(args) -> int`. The mapping is
# spelled out rather than derived from the command name so the target of any command is greppable,
# and the import happens at dispatch time so `codeintel serve` never pays for the semantic
# engine's imports (nor serve-http for the graph's).
_MODULES = {
    "query": "query",
    "map": "map",
    "graph": "graph",
    "c4": "c4",
    "setup": "setup",
    "index": "index",
    "install": "install",
    "prompt": "prompt",
    "doctor": "doctor",
    "status": "status",
    "reset": "reset",
    "serve": "serve",
    "serve-http": "serve_http",
    "gen-token": "gen_token",
}

# Deliberately NOT a second setup path. `_START_HERE` owns onboarding; these show what the tool is
# FOR once it works. The two blocks used to disagree — one said `index` first, the other
# `setup --all` — and neither of the numbered steps mentioned `install`, so following the "New
# here?" list end to end produced a working CLI and an agent that still greps. README's quickstart
# is `setup --all` -> `install` -> `query`; `_START_HERE` now matches it.
_EXAMPLES = [
    ('codeintel query --op changed --target ""', "what do my edits break?"),
    ("codeintel query --op callers --target foo", "who calls it?"),
    ("codeintel graph . --html", "open the call graph"),
    ("codeintel doctor", "why is a query empty?"),
]

# The ordered path out of an empty state. A first-time user's problem is not "which of 15 commands"
# but "what do I run first" — the command list answers the former and silently assumes the latter.
_START_HERE = [
    ("codeintel setup --all .", "backends + index this repo"),
    ("codeintel install", "connect it to your AI agent"),
    ("codeintel doctor", "confirm it works"),
]


HELP_WIDTH = 78              # the width every rendered help line must fit inside
HELP_GUTTER_CAP = 44         # ceiling on the derived comment column (see `gutter` below)


def render_help() -> str:
    """The `codeintel` / `codeintel help` screen: grouped, colored, with real examples.

    Color comes from codeintel.term, so it auto-degrades on a pipe, under NO_COLOR, and on a dumb
    terminal — same as every other human-facing command."""
    from codeintel.term import c

    width = max(len(name) for name in _COMMANDS)
    out = [
        c.bold("codeintel") + c.dim(f" {__version__}")
        + c.dim("  —  code intelligence for AI agents: graph, LSP, semantic"),
        "",
        c.dim("usage: ") + "codeintel <command> [options]",
    ]

    # ONE comment column across both blocks, derived from the widest entry in either. Two blocks
    # each self-aligning would ragged the screen into two gutters; the numbered prefix ("1. ") is
    # part of the measured width so the two blocks' comments still line up.
    # Capped. Derived-from-content is right, but an unbounded max lets ONE long invocation set the
    # column for every short row: `codeintel doctor` was paying 38 blank columns and the line
    # reached 96 chars, so an 80-column terminal wrapped the comment onto its own ragged row —
    # losing the alignment the gutter exists to create. Past the cap a row keeps its comment one
    # space away rather than aligned; a ragged row beats a wrapped screen.
    gutter = min(max(max(len(cmd) + 3 for cmd, _why in _START_HERE),
                     max(len(cmd) for cmd, _why in _EXAMPLES)) + 2, HELP_GUTTER_CAP)
    out.append("")
    # A rule, not a new colour: cyan already means "this is a command name" on this screen, and
    # diluting that costs more than it buys. The block needed separating from the four command
    # groups below it — it was bold at the same weight and indent as every group heading, so the
    # one block a first-time user needs looked like part of the reference list.
    out.append("  " + c.rule(40))
    out.append("  " + c.bold("New here?"))
    for i, (cmd, why) in enumerate(_START_HERE, 1):
        out.append("    " + f"{i}. {cmd}".ljust(gutter) + c.dim("# " + why))

    for group, items in _COMMAND_GROUPS:
        out.append("")
        out.append("  " + c.bold(group))
        for name, desc in items:
            out.append("    " + c.cyan(name.ljust(width)) + "  " + desc)

    out.append("")
    out.append("  " + c.bold("Examples"))
    # Same `gutter` as the New here? block above — width from the content, not a guess. A hardcoded
    # column silently loses its gutter the moment one example grows past it, and ljust() will not
    # pad below the string's own length.
    for cmd, why in _EXAMPLES:
        out.append("    " + cmd.ljust(gutter) + c.dim("# " + why))

    out.append("")
    out.append("  " + c.dim("codeintel <command> --help") + "   full options for one command")
    out.append("  " + c.dim("docs: https://github.com/hamilton-sky/codeintel"))
    return "\n".join(out)


def _suggest(unknown: str) -> list[str]:
    """Commands a typo probably meant. Close matches first, then prefix matches — `gragh` should
    land on `graph`, and a bare `serv` on both `serve` and `serve-http`."""
    close = difflib.get_close_matches(unknown, _COMMANDS, n=3, cutoff=0.5)
    prefix = [cmd for cmd in _COMMANDS if cmd.startswith(unknown) and cmd not in close]
    return (close + prefix)[:3]


def _unknown_command(name: str) -> int:
    """Report an unrecognized command with a way forward. argparse's own error dumps the full list
    of choices and stops there, which is a dead end for a one-character typo."""
    from codeintel.term import c_err as e

    print(e.red(f"unknown command: {name!r}"), file=sys.stderr)
    matches = _suggest(name)
    if matches:
        joined = " or ".join(e.cyan(m) for m in matches)
        print(f"\n  did you mean {joined}?", file=sys.stderr)
    print("\n  " + e.dim("run `codeintel help` to see every command"), file=sys.stderr)
    return 2


# `epilog` text for each subcommand: WHEN to reach for it, and one real invocation. The top-level
# screen (`render_help`) has grouped output, colour and examples; every `codeintel <cmd> --help`
# one level down was still stock argparse — flags listed, purpose unexplained, no example. A flag
# list answers "what can I pass"; it never answers "should I be running this at all".
#
# Kept as a table rather than inline `epilog=` kwargs so the wording of every screen is reviewable
# in one place, and so a command added without an entry degrades to today's behaviour (no epilog)
# rather than breaking.
_EPILOGS: dict[str, str] = {
    "index": """examples:
  codeintel index .                     index this repo (semantic + graph)
  codeintel index ~/src/other-repo      index a repo elsewhere

Run this first, and again after big changes. `codeintel status` shows index
age.""",
    "query": """examples:
  codeintel query --op changed
  codeintel query --op search
  codeintel query --op impact
  codeintel query --op callers
  codeintel query --op chain
  codeintel query --op overview --target ""

`overview`, `changed` and `hotspots` ignore --target. Prefer this over grep:
results are ranked by graph importance, not by match order.""",
    "map": """examples:
  codeintel map .                       write CODE_INTEL.md
  codeintel map . --inject              also point CLAUDE.md / AGENTS.md at it

A committable, readable architecture overview — for agents that do not speak
MCP, and for reading before grepping. Re-run after `codeintel index`.""",
    "graph": """examples:
  codeintel graph . --html
  codeintel graph . > graph.json        the graph as {nodes,edges} JSON

Function-level "what calls what", with nothing to install. For file-level
architecture instead, see `codeintel c4`.""",
    "c4": """examples:
  codeintel c4 .                        write codeintel-c4/model.c4
  codeintel c4 . --scope src            model only src/
  codeintel c4 . --depth 2              coarser: fewer, larger boxes
  codeintel c4 . --json                 inspect the payload, write nothing

File/directory-level "what depends on what". Emits LikeC4 source you can
commit, diff and hand-edit — viewing it needs Node (`npx likec4 start
codeintel-c4`). For function-level calls, or if you have no Node, use
`codeintel graph --html` instead. Indexes the repo first if needed.""",
    "status": """examples:
  codeintel status

Shows which of the three engines are ready. If one is not, `codeintel doctor`
says how to fix it.""",
    "doctor": """examples:
  codeintel doctor
  codeintel doctor --deep

Run this when a query comes back empty — it separates "not indexed" from
"engine missing" from "nothing to find".""",
    "setup": """examples:
  codeintel setup --all
  codeintel setup

The one-shot path on a new machine.""",
    "install": """examples:
  codeintel install
  codeintel install --dry-run           show what would change, write nothing

Writes the MCP server config so an agent can call codeintel. Then restart the
agent (or start a new session) — a running host does not reload its MCP
config. `codeintel doctor` lists what got registered where.""",
    "prompt": """examples:
  codeintel prompt

For agents that cannot read an MCP config: paste the output into the
conversation.""",
    "reset": """examples:
  codeintel reset .
  codeintel reset --all --yes           wipe every repo, no prompt

Recovers from a corrupt or stale index. Re-index afterwards.""",
    "serve": """examples:
  codeintel serve                       start the MCP server on stdio

This is what an AI agent launches; you rarely run it by hand. `codeintel
install` wires it up.""",
    "serve-http": """examples:
  codeintel serve-http                  loopback only, no auth
  codeintel serve-http --token "$(codeintel gen-token)"

Loopback-only unless --allow-remote. Use --token whenever the port is
reachable by anything else.""",
    "gen-token": """examples:
  codeintel gen-token                   print a random bearer token

For `serve-http --token`, or an RBAC auth.toml.""",
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argparse surface.

    Split out of `main()` so the registered subcommands can be introspected directly. `--help` is
    routed to `render_help()`, so argparse's own listing is no longer reachable from the CLI — and
    that listing was how the help-honesty test proved every advertised command is really wired up.
    Reading the parser is a stronger source of truth than scraping either screen's text."""
    parser = argparse.ArgumentParser(prog="codeintel")
    parser.add_argument("--version", action="version", version=f"codeintel {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # Shared flags for the human-facing (styled) commands.
    color_parent = argparse.ArgumentParser(add_help=False)
    color_parent.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    color_parent.add_argument("--ascii", action="store_true", help="Use ASCII-only glyphs")

    subparsers.add_parser("serve", help="Start the MCP server")

    # index subcommand
    index_parser = subparsers.add_parser(
        "index", parents=[color_parent],
        help="Build the index every other command reads (semantic + graph)")
    index_parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        help="Project root directory (default: cwd)",
    )
    index_parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress the live progress display and header; keep only the result line",
    )

    # query subcommand
    query_parser = subparsers.add_parser("query", help="Query the code intelligence engine")
    query_parser.add_argument("--op", required=True, help="Query operation (e.g. search, symbol)")
    query_parser.add_argument("--target", required=True, help="Query target")
    query_parser.add_argument("--engine", default="auto", help="Engine to use (default: auto)")
    query_parser.add_argument(
        "--project-root",
        default=None,
        help="Project root directory (default: cwd)",
    )
    query_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full result envelope (engine, cached, reason, hint, reindexing) — what a "
             "bug report needs, and what an agent host sees",
    )

    # status subcommand
    status_parser = subparsers.add_parser("status", help="Show code intelligence engine status")
    status_parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        help="Project root directory (default: cwd)",
    )

    # serve-http subcommand
    http_parser = subparsers.add_parser("serve-http", help="Start the HTTP transport server")
    http_parser.add_argument("--port", type=int, default=8766, help="Port to listen on (default: 8766)")
    http_parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    http_parser.add_argument("--allow-remote", action="store_true",
                             help="Permit binding a non-loopback host (use with --token, or the "
                                  "endpoint is UNAUTHENTICATED)")
    http_parser.add_argument("--token", default=None,
                             help="Require this bearer token on every request (or set "
                                  "CODEINTEL_HTTP_TOKEN). Strongly recommended with --allow-remote.")

    # install subcommand
    install_parser = subparsers.add_parser("install", help="Register codeintel with AI agents")
    install_parser.add_argument(
        "--agent",
        choices=["auto", "claude", "codex", "gemini", "zed", "all"],
        default="auto",
        help="Agent to register with (default: auto — only agents installed on this machine; "
             "`all` forces every supported agent)",
    )
    install_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-registration MCP handshake (verification is on by default)",
    )
    install_parser.add_argument(
        "--relative-command",
        action="store_true",
        help="Register the bare `codeintel` name instead of its absolute path (the absolute path "
             "is the default because a GUI-launched agent does not inherit your shell's PATH)",
    )

    # map subcommand
    map_parser = subparsers.add_parser("map", help="Generate CODE_INTEL.md orientation file")
    map_parser.add_argument("project_root", nargs="?", default=None,
                            help="Project root (default: cwd)")
    map_parser.add_argument("--inject", action="store_true", help="Inject reference block into CLAUDE.md/AGENTS.md")
    map_parser.add_argument("--budget", type=int, default=32768, help="Byte budget for CODE_INTEL.md (default: 32768)")

    # graph subcommand — interactive call-graph view (HTML) or the raw {nodes,edges} JSON
    graph_parser = subparsers.add_parser(
        "graph", help="Build an interactive call-graph view (--html) or emit the graph as JSON — "
                      "works on any indexed repo")
    graph_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    graph_parser.add_argument("--html", action="store_true",
                              help="Write a self-contained interactive HTML viewer (default: print JSON)")
    graph_parser.add_argument("--out", default=None, help="Output path for --html (default: codeintel-graph.html)")
    graph_parser.add_argument("--limit", type=int, default=220, help="Max call edges to include (default: 220)")

    # c4 subcommand — a LikeC4 model of the Folder/File + IMPORTS slice
    c4_parser = subparsers.add_parser(
        "c4", help="Generate a LikeC4 architecture model (.c4) from the import graph")
    c4_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    c4_parser.add_argument("--out", default=None,
                           help="Output DIRECTORY (default: codeintel-c4/). LikeC4 merges every "
                                ".c4 in a directory into one project, so the model gets its own.")
    c4_parser.add_argument("--depth", type=int, default=None,
                           help="Directory roll-up depth for elements (default: auto-fit to the "
                                "100-element view cap; the chosen depth is always reported)")
    c4_parser.add_argument("--scope", action="append", default=None,
                           help="Limit the model to this path prefix; repeatable. A scope matching "
                                "no indexed file is an error, not a silent empty model.")
    c4_parser.add_argument("--include-tests", action="store_true",
                           help="Model test directories too (excluded by default — they are not "
                                "architecture and outnumber source in some repos)")
    c4_parser.add_argument("--no-index", action="store_true",
                           help="Fail instead of indexing an un-indexed repo (default: index it "
                                "first, so one command always produces a model)")
    c4_parser.add_argument("--json", action="store_true", help="Print the payload; write nothing")

    # doctor subcommand
    doctor_parser = subparsers.add_parser("doctor", parents=[color_parent],
                                          help="Diagnose engine health + index status for a repo")
    doctor_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    doctor_parser.add_argument("--deep", action="store_true",
                               help="Also boot-check serena (slower; first boot pulls it via uvx)")
    doctor_parser.add_argument("--json", action="store_true",
                               help="Emit the structured JSON report instead of the table")

    # setup subcommand
    setup_parser = subparsers.add_parser("setup", parents=[color_parent],
                                         help="Prepare backends and optionally index this repo")
    setup_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    setup_parser.add_argument("--all", action="store_true", dest="all_steps",
                              help="One-command setup: do everything automatable (uv + deps + index + "
                                   "warm serena). Idempotent — skips what's already installed.")
    setup_parser.add_argument("--install-uv", action="store_true",
                              help="Run `pip install uv` (provides uvx for the LSP engine)")
    setup_parser.add_argument("--install-deps", action="store_true",
                              help="Run `pip install -e .` (semantic engine deps)")
    setup_parser.add_argument("--index", action="store_true",
                              help="Index this repo now (first run downloads the ~50MB model)")
    setup_parser.add_argument("--warm", action="store_true", help="Boot serena now (first run pulls it via uvx; slow)")
    setup_parser.add_argument("--json", action="store_true", help="Emit the structured JSON report")

    # prompt subcommand
    prompt_parser = subparsers.add_parser("prompt", parents=[color_parent],
                                          help="Print a paste-to-your-agent setup prompt for this repo")
    prompt_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    prompt_parser.add_argument("--agent", default="auto",
                               help="Agent the prompt targets: claude|codex|gemini|zed|auto (default: auto)")
    prompt_parser.add_argument("--fresh", action="store_true",
                               help="Emit the full sequence from `pip install`, ignoring local state "
                                    "(a template to paste to a friend on a clean machine)")
    prompt_parser.add_argument("--deep", action="store_true",
                               help="Boot-check serena while probing (slower; sharper 'already set up' result)")

    # reset subcommand
    reset_parser = subparsers.add_parser("reset", parents=[color_parent],
                                         help="Clear the semantic index (recover from a corrupt/stale DB)")
    reset_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    reset_parser.add_argument("--all", action="store_true",
                              help="Clear the ENTIRE index (all projects), not just this repo")
    reset_parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    reset_parser.add_argument("--json", action="store_true", help="Emit the structured JSON report")

    subparsers.add_parser("gen-token", help="Print a secure random bearer token (for serve-http / RBAC auth.toml)")
    subparsers.add_parser("help", help="Show every command, grouped, with examples")

    # Applied in one pass over the built parser rather than at each `add_parser` call site: an
    # epilog is presentation, and threading two extra kwargs through fourteen construction sites
    # would bury the flags that actually define each command. RawDescriptionHelpFormatter goes with
    # it — argparse otherwise re-wraps the epilog and destroys the aligned example columns.
    for _name, _sub in (subparsers.choices or {}).items():
        _epilog = _EPILOGS.get(_name)
        if _epilog:
            _sub.epilog = _epilog
            _sub.formatter_class = argparse.RawDescriptionHelpFormatter

    return parser


def main() -> None:
    parser = build_parser()

    # Intercept an unrecognized command BEFORE argparse, whose error prints the full choice list and
    # stops — a dead end for a one-character typo. Only a bare word is claimed here; anything
    # starting with `-` (--version) still goes to argparse.
    argv = sys.argv[1:]
    if argv and not argv[0].startswith("-") and argv[0] not in [*_COMMANDS, "help"]:
        sys.exit(_unknown_command(argv[0]))
    # `-h`/`--help` only counts as top-level when it is the FIRST token: `codeintel query --help`
    # has argv[0] == "query", so it still falls through to argparse and gets that command's own
    # screen. Without this, the command a new user actually types was the ONE path that missed
    # render_help() and fell back to stock argparse.
    if not argv or argv[0] in ("help", "-h", "--help"):
        from codeintel import term
        term.configure(no_color=False, ascii_mode=None)
        print(render_help())
        sys.exit(0)

    args = parser.parse_args()

    from codeintel import term
    term.configure(
        no_color=getattr(args, "no_color", False),
        ascii_mode=(True if getattr(args, "ascii", False) else None),
    )

    module = _MODULES.get(args.command)
    if module is None:          # unreachable via argparse; a bare `codeintel` is handled above
        print(render_help())
        sys.exit(0)
    sys.exit(import_module(f"codeintel.commands.{module}").run(args))


if __name__ == "__main__":
    main()
