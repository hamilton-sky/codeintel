import argparse
import os
import sys

from codeintel import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="codeintel")
    parser.add_argument("--version", action="version", version=f"codeintel {__version__}")
    subparsers = parser.add_subparsers(dest="command")
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

    # install subcommand
    install_parser = subparsers.add_parser("install", help="Register codeintel with AI agents")
    install_parser.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini", "zed", "all"],
        default="all",
        help="Agent to register with (default: all)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        from codeintel.server import run
        run()

    elif args.command == "index":
        from codeintel.config import load_config
        from codeintel.indexer import Indexer
        from codeintel.semantic_db import SemanticDb

        project_root = args.project_root or os.getcwd()
        load_config(project_root)  # validate/load config; result unused here but exercises the path

        db_dir = os.path.join(project_root, ".codeintel")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "semantic.db")
        db = SemanticDb(db_path)
        try:
            db.init()
            count = Indexer(db).index(project_root)
            if count > 0:
                print(f"Indexed {count} chunks")
            else:
                print("Nothing new to index")
        finally:
            db.close()

        # best-effort graph reindex
        import shutil
        if shutil.which("codebase-memory-mcp"):
            try:
                from codeintel.reindexer import Reindexer
                Reindexer()._graph_reindex(project_root)
            except Exception:
                pass

    elif args.command == "query":
        try:
            from codeintel import server
            project_root = args.project_root or os.getcwd()
            engine = args.engine if args.engine != "auto" else None
            gw = server._build_gateway()
            result = gw.query(
                op=args.op,
                target=args.target,
                engine=engine,
                role="",
                project_root=project_root,
            )
            value = result.get("result")
            if value is not None:
                print(value)
            else:
                reason = result.get("reason", "unknown")
                print(f"No result (reason: {reason})")
        except Exception as exc:
            print(f"No result (reason: {exc})")
        sys.exit(0)

    elif args.command == "status":
        try:
            from codeintel import server

            project_root = args.project_root or os.getcwd()
            status = server.code_status_handler({})

            print("Engine status:")
            for engine in ["graph", "lsp", "semantic"]:
                available = status.get(engine, False)
                state = "available" if available else "unavailable"
                print(f"  {engine:<10} {state}")

            db_path = os.path.join(project_root, ".codeintel", "semantic.db")
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

    elif args.command == "serve-http":
        try:
            from codeintel.http_server import run
            run(host=args.host, port=args.port)
        except KeyboardInterrupt:
            pass

    elif args.command == "install":
        from codeintel.installer import Installer

        installer = Installer()
        if args.agent == "all":
            results = installer.register_all()
        else:
            results = [installer.register(args.agent)]

        any_ok = False
        for r in results:
            agent = r["agent"]
            path = r["path"]
            action = r["action"]
            if action == "registered":
                print(f"v {agent}: registered at {path}")
                any_ok = True
            elif action == "already":
                print(f"~ {agent}: already registered at {path}")
                any_ok = True
            else:
                print(f"x {agent}: failed — {r['reason']}")

        sys.exit(0 if any_ok else 1)

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
