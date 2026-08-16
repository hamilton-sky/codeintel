import argparse
import difflib
import os
import sys

from codeintel import __version__

# Commands grouped by what you are trying to DO. argparse lists them in declaration order with no
# grouping, which turns "what can this thing do?" into reading twelve lines to find the one verb you
# wanted. Each entry is (command, one-line description).
_COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Understand your code", [
        ("query", "Ask one question — search, callers, callees, impact, chain, symbol, hotspots"),
        ("map", "Write CODE_INTEL.md — a committable architecture overview"),
        ("graph", "Interactive call-graph viewer (--html), or the graph as JSON"),
    ]),
    ("Set up", [
        ("setup", "Prepare backends + index this repo (--all does everything automatable)"),
        ("index", "Index a project for semantic search"),
        ("install", "Register codeintel with the AI agents installed on this machine"),
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

_EXAMPLES = [
    ("codeintel setup --all .", "prepare backends and index this repo"),
    ("codeintel install", "register with the agents you have"),
    ("codeintel query --op callers --target my_function", "who calls it?"),
    ("codeintel doctor", "why is a query coming back empty?"),
]


def render_help() -> str:
    """The `codeintel` / `codeintel help` screen: grouped, colored, with real examples.

    Color comes from codeintel.term, so it auto-degrades on a pipe, under NO_COLOR, and on a dumb
    terminal — same as every other human-facing command."""
    from codeintel.term import c

    width = max(len(name) for name in _COMMANDS)
    out = [
        c.bold("codeintel") + c.dim(f" {__version__}")
        + c.dim("  —  code intelligence for AI agents: graph + LSP + semantic search"),
        "",
        c.dim("usage: ") + "codeintel <command> [options]",
    ]
    for group, items in _COMMAND_GROUPS:
        out.append("")
        out.append("  " + c.bold(group))
        for name, desc in items:
            out.append("    " + c.cyan(name.ljust(width)) + "  " + desc)

    out.append("")
    out.append("  " + c.bold("Examples"))
    # Width from the content, not a guess — a hardcoded column silently loses its gutter the moment
    # one example grows past it, and ljust() will not pad below the string's own length.
    cmd_width = max(len(cmd) for cmd, _why in _EXAMPLES) + 2
    for cmd, why in _EXAMPLES:
        out.append("    " + cmd.ljust(cmd_width) + c.dim("# " + why))

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="codeintel")
    parser.add_argument("--version", action="version", version=f"codeintel {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # Shared flags for the human-facing (styled) commands.
    color_parent = argparse.ArgumentParser(add_help=False)
    color_parent.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    color_parent.add_argument("--ascii", action="store_true", help="Use ASCII-only glyphs")

    subparsers.add_parser("serve", help="Start the MCP server")

    # index subcommand
    index_parser = subparsers.add_parser("index", help="Index a project for semantic search")
    index_parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        help="Project root directory (default: cwd)",
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
    http_parser.add_argument("--allow-remote", action="store_true", help="Permit binding a non-loopback host (use with --token, or the endpoint is UNAUTHENTICATED)")
    http_parser.add_argument("--token", default=None, help="Require this bearer token on every request (or set CODEINTEL_HTTP_TOKEN). Strongly recommended with --allow-remote.")

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
    map_parser.add_argument("project_root", nargs="?", default=None)
    map_parser.add_argument("--inject", action="store_true", help="Inject reference block into CLAUDE.md/AGENTS.md")
    map_parser.add_argument("--budget", type=int, default=32768, help="Byte budget for CODE_INTEL.md (default: 32768)")

    # graph subcommand — interactive call-graph view (HTML) or the raw {nodes,edges} JSON
    graph_parser = subparsers.add_parser("graph", help="Build an interactive call-graph view (--html) or emit the graph as JSON — works on any indexed repo")
    graph_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    graph_parser.add_argument("--html", action="store_true", help="Write a self-contained interactive HTML viewer (default: print JSON)")
    graph_parser.add_argument("--out", default=None, help="Output path for --html (default: codeintel-graph.html)")
    graph_parser.add_argument("--limit", type=int, default=220, help="Max call edges to include (default: 220)")

    # doctor subcommand
    doctor_parser = subparsers.add_parser("doctor", parents=[color_parent], help="Diagnose engine health + index status for a repo")
    doctor_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    doctor_parser.add_argument("--deep", action="store_true", help="Also boot-check serena (slower; first boot pulls it via uvx)")
    doctor_parser.add_argument("--json", action="store_true", help="Emit the structured JSON report instead of the table")

    # setup subcommand
    setup_parser = subparsers.add_parser("setup", parents=[color_parent], help="Prepare backends and optionally index this repo")
    setup_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    setup_parser.add_argument("--all", action="store_true", dest="all_steps",
                              help="One-command setup: do everything automatable (uv + deps + index + "
                                   "warm serena). Idempotent — skips what's already installed.")
    setup_parser.add_argument("--install-uv", action="store_true", help="Run `pip install uv` (provides uvx for the LSP engine)")
    setup_parser.add_argument("--install-deps", action="store_true", help="Run `pip install -e .` (semantic engine deps)")
    setup_parser.add_argument("--index", action="store_true", help="Index this repo now (first run downloads the ~50MB model)")
    setup_parser.add_argument("--warm", action="store_true", help="Boot serena now (first run pulls it via uvx; slow)")
    setup_parser.add_argument("--json", action="store_true", help="Emit the structured JSON report")

    # reset subcommand
    reset_parser = subparsers.add_parser("reset", parents=[color_parent], help="Clear the semantic index (recover from a corrupt/stale DB)")
    reset_parser.add_argument("project_root", nargs="?", default=None, help="Project root (default: cwd)")
    reset_parser.add_argument("--all", action="store_true", help="Clear the ENTIRE index (all projects), not just this repo")
    reset_parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    reset_parser.add_argument("--json", action="store_true", help="Emit the structured JSON report")

    subparsers.add_parser("gen-token", help="Print a secure random bearer token (for serve-http / RBAC auth.toml)")
    subparsers.add_parser("help", help="Show every command, grouped, with examples")

    # Intercept an unrecognized command BEFORE argparse, whose error prints the full choice list and
    # stops — a dead end for a one-character typo. Only a bare word is claimed here; anything
    # starting with `-` (--version, --help) still goes to argparse.
    argv = sys.argv[1:]
    if argv and not argv[0].startswith("-") and argv[0] not in _COMMANDS + ["help"]:
        sys.exit(_unknown_command(argv[0]))
    if not argv or argv[0] == "help":
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

    if args.command == "serve":
        from codeintel.server import run
        run()

    elif args.command == "index":
        from codeintel.config import load_config
        from codeintel.indexer import Indexer
        from codeintel.semantic_db import SemanticDb, default_db_path

        project_root = args.project_root or os.getcwd()
        # Wrap the whole semantic pass so a setup failure (e.g. an unresolvable home dir →
        # Path.home() raising) degrades with a message, like every other subcommand, not a traceback.
        try:
            cfg = load_config(project_root)
            db_path = default_db_path(str(cfg.get("model") or ""))
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db = SemanticDb(db_path)
            try:
                db.init()
                count = Indexer(
                    db,
                    model_name=str(cfg.get("model") or "BAAI/bge-small-en-v1.5"),
                    window=int(cfg.get("window", 20)),
                    stride=int(cfg.get("stride", 10)),
                    max_chunks=int(cfg.get("max_chunks", 500)),
                    max_total_chunks=int(cfg.get("max_total_chunks", 100000)),
                    chunk_strategy=str(cfg.get("chunk_strategy", "syntax")),
                ).index(project_root)
                if count > 0:
                    print(f"Indexed {count} chunks")
                else:
                    print("Nothing new to index")
            finally:
                db.close()
        except Exception as exc:
            print(f"index failed: {exc}")

        # best-effort graph reindex
        import shutil
        if shutil.which("codebase-memory-mcp"):
            try:
                from codeintel.reindexer import Reindexer
                Reindexer()._graph_reindex(project_root)
            except Exception:
                pass

        # best-effort map refresh after index
        try:
            from codeintel.providers.graph import GraphProvider
            from codeintel.mapper import MapGenerator
            _provider = GraphProvider()
            _gen = MapGenerator(_provider)
            _content = _gen.generate(project_root)
            _gen.write(project_root, _content)
        except Exception:
            pass

    elif args.command == "map":
        from codeintel.providers.graph import GraphProvider
        from codeintel.mapper import MapGenerator
        from codeintel.injector import Injector

        project_root = args.project_root or os.getcwd()
        try:
            provider = GraphProvider()
        except Exception:
            provider = None
        gen = MapGenerator(provider)
        try:
            content = gen.generate(project_root, budget_bytes=args.budget)
            path, wrote = gen.write(project_root, content)
            if wrote:
                print(f"Wrote {path} ({len(content.encode())} bytes)")
            else:
                print(f"Kept existing {path} — new map was a stub (graph empty/unindexed; "
                      f"run `codeintel index` first)")
            if args.inject:
                inj_path, inj_action = Injector().inject(project_root)
                if inj_path:
                    print(f"Inject: {inj_action} block in {inj_path}")
                else:
                    print(f"Inject: {inj_action}")
        except Exception as exc:
            # Never-raise parity with the MCP code.map handler — degrade, don't crash.
            print(f"map failed: {exc}")
        sys.exit(0)

    elif args.command == "graph":
        from codeintel import grapher
        project_root = args.project_root or os.getcwd()
        try:
            payload = grapher.build_graph_payload(project_root, limit=args.limit)
            n, e = len(payload.get("nodes", [])), len(payload.get("edges", []))
            if args.html:
                out = args.out or "codeintel-graph.html"
                with open(out, "w", encoding="utf-8") as f:
                    f.write(grapher.render_html(payload))
                print(f"Wrote {out}  ({n} nodes, {e} edges) — open it in any browser")
                if not n:
                    reason = payload.get("reason")
                    if reason:
                        print(f"  (graph empty: {reason} — run `codeintel doctor` to check the "
                              f"graph backend + index)")
                    else:
                        print("  (no internal call edges found for this repo)")
            else:
                import json as _json
                print(_json.dumps(payload, indent=2))
        except Exception as exc:
            print(f"graph failed: {exc}")
        sys.exit(0)

    elif args.command == "query":
        try:
            import time

            from codeintel import server
            project_root = args.project_root or os.getcwd()
            engine = args.engine if args.engine != "auto" else None
            gw = server._build_gateway()

            def _run_query():
                return gw.query(
                    op=args.op,
                    target=args.target,
                    engine=engine,
                    role="",
                    project_root=project_root,
                )

            result = _run_query()

            # The LSP engine warms a serena session in a background thread and returns
            # reason:warming on the first call. A one-shot CLI process would otherwise always
            # exit on 'warming' (the subprocess dies with it). Wait — bounded, never-raise —
            # for the session to boot, re-querying the same gateway (session is cached per root).
            if result.get("result") is None and result.get("reason") == "warming":
                print("(lsp warming up — waiting for the language server...)", file=sys.stderr)
                deadline = time.monotonic() + 45.0
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    result = _run_query()
                    if result.get("result") is not None or result.get("reason") != "warming":
                        break

            value = result.get("result")
            if value is not None:
                print(value)
            else:
                reason = result.get("reason", "unknown")
                hint = result.get("hint")
                print(f"No result (reason: {reason})")
                if hint:
                    print(f"  hint: {hint}", file=sys.stderr)
        except Exception as exc:
            print(f"No result (reason: {exc})")
        sys.exit(0)

    elif args.command == "status":
        try:
            from codeintel import server

            project_root = args.project_root or os.getcwd()
            status = server.code_status_handler({"project_root": project_root})

            # "available" alone was the misleading word: it meant "a binary is on PATH", which is
            # not the same as runnable, and not the same as usable on THIS repo. Say which.
            _READY = {"ok": "ready", "warn": "installed (not verified)", "fail": "unavailable"}
            readiness = status.get("readiness") or {}
            print("Engine status:")
            for engine in ["graph", "lsp", "semantic"]:
                entry = readiness.get(engine) or {}
                state = _READY.get(entry.get("status"), "unavailable")
                detail = entry.get("detail") or ""
                print(f"  {engine:<10} {state:<26} {detail}")
            if status.get("healthy") is False:
                print("\n  run `codeintel doctor` for the fix for each gap")

            from codeintel.config import load_config
            from codeintel.semantic_db import default_db_path
            db_path = default_db_path(str(load_config(project_root).get("model") or ""))
            if os.path.exists(db_path):
                import datetime
                mtime = os.path.getmtime(db_path)
                age = datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)
                hours = int(age.total_seconds() // 3600)
                minutes = int((age.total_seconds() % 3600) // 60)
                print(f"\nIndex age: {hours}h {minutes}m  ({db_path})")
            else:
                print(f"\nIndex: not found  ({db_path})")
        except Exception as exc:
            print(f"Status unavailable: {exc}")
        sys.exit(0)

    elif args.command == "doctor":
        try:
            from codeintel import doctor as _doctor

            project_root = args.project_root or os.getcwd()
            report = _doctor.run_doctor(project_root, deep=args.deep)
            if args.json:
                import json as _json
                print(_json.dumps(report, indent=2))
            else:
                print(_doctor.render_doctor_text(report))
            # Exit non-zero when a repo-critical engine is unhealthy, so scripts/CI can gate on it.
            sys.exit(0 if report.get("summary", {}).get("healthy") else 1)
        except Exception as exc:
            print(f"doctor unavailable: {exc}")
            sys.exit(0)

    elif args.command == "setup":
        try:
            from codeintel import onboarding

            project_root = args.project_root or os.getcwd()
            all_steps = getattr(args, "all_steps", False)  # --all implies every automatable step
            report = onboarding.run_setup(
                project_root,
                install_uv=args.install_uv or all_steps,
                install_deps=args.install_deps or all_steps,
                do_index=args.index or all_steps,
                warm_lsp=args.warm or all_steps,
            )
            if args.json:
                import json as _json
                print(_json.dumps(report, indent=2))
            else:
                print(onboarding.render_setup_text(report))
            healthy = report.get("doctor", {}).get("summary", {}).get("healthy")
            sys.exit(0 if healthy else 1)
        except Exception as exc:
            print(f"setup unavailable: {exc}")
            sys.exit(0)

    elif args.command == "reset":
        try:
            from codeintel import reset as _reset

            project_root = args.project_root or os.getcwd()
            preview = _reset.run_reset(project_root, all_projects=args.all, apply=False)
            if not args.yes:
                if not sys.stdin.isatty():
                    print("refusing to reset without --yes in a non-interactive shell")
                    sys.exit(1)
                target = "ALL projects" if args.all else project_root
                count = preview.get("count", 0)
                ans = input(f"Reset semantic index for {target} ({count} entries)? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    print("aborted")
                    sys.exit(0)
            report = _reset.run_reset(project_root, all_projects=args.all, apply=True)
            if args.json:
                import json as _json
                print(_json.dumps(report, indent=2))
            else:
                print(report.get("detail", "reset complete"))
            sys.exit(0)
        except Exception as exc:
            print(f"reset unavailable: {exc}")
            sys.exit(0)

    elif args.command == "serve-http":
        try:
            from codeintel.http_server import run
            token = args.token or os.environ.get("CODEINTEL_HTTP_TOKEN")
            run(host=args.host, port=args.port, allow_remote=args.allow_remote, token=token)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            # A startup failure (e.g. port in use) should print a friendly line, not a traceback.
            # SystemExit from the non-loopback guard is BaseException — it passes through here.
            print(f"serve-http failed: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "install":
        from codeintel.installer import Installer

        installer = Installer()
        do_verify = not args.no_verify
        absolute = not args.relative_command
        skipped: list[str] = []
        if args.agent == "auto":
            results, skipped = installer.register_detected(verify=do_verify, absolute=absolute)
            if not results:
                print("No supported agent found on this machine "
                      f"(looked for: {', '.join(skipped)}).")
                print("  Install one, or force registration with "
                      "`codeintel install --agent <name>`.")
                sys.exit(1)
        elif args.agent == "all":
            results = installer.register_all(verify=do_verify, absolute=absolute)
        else:
            results = installer.register_many([args.agent], verify=do_verify, absolute=absolute)

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

        # A written config file proves nothing about whether the host can launch the server —
        # so say what the handshake actually found, and fail loudly when it did not happen.
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

        # Name what was skipped: silence about an untouched host reads as "unsupported" rather
        # than "you don't have it installed".
        if skipped:
            print(f"\n- skipped (not installed here): {', '.join(skipped)}"
                  f"\n  register anyway with `codeintel install --agent <name>`")

        print("\n  Start a new agent session to pick up the tools.")
        sys.exit(0 if any_ok else 1)

    elif args.command == "gen-token":
        import secrets
        print(secrets.token_urlsafe(32))
        sys.exit(0)

    else:
        print(render_help())
        sys.exit(0)


if __name__ == "__main__":
    main()
