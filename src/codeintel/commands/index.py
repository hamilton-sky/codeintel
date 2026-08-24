"""`codeintel index` — build the semantic index, then best-effort refresh the graph and the map."""

import os
from typing import Any

from codeintel.commands._common import resolve_root


def run(args: Any) -> int:
    import sys

    import codeintel.term as term
    from codeintel.config import load_config
    from codeintel.indexer import Indexer
    from codeintel.semantic_db import SemanticDb, default_db_path

    project_root = resolve_root(args)
    # Validate before doing anything. `codeintel index /typo/path` walked nothing, found nothing,
    # and printed "Nothing new to index" at exit 0 — indistinguishable from a correct incremental
    # run, which is the worst possible response to a mistyped path in a script.
    if not os.path.isdir(project_root):
        print(f"index failed: not a directory: {project_root}")
        return 1

    # A console bound to the CURRENT stdout (honouring --no-color/--ascii/NO_COLOR), so progress is
    # captured under test and routed correctly whether or not main() already ran configure(). The
    # LiveCounter auto-goes silent on a non-TTY; --quiet suppresses it (and the header) outright.
    console = term.Console(
        stream=sys.stdout,
        no_color=getattr(args, "no_color", False),
        ascii_mode=(True if getattr(args, "ascii", False) else None),
    )
    quiet = getattr(args, "quiet", False)
    counter = None if quiet else term.LiveCounter(console)

    failed = False
    # Wrap the whole semantic pass so a setup failure (e.g. an unresolvable home dir →
    # Path.home() raising) degrades with a message, like every other subcommand, not a traceback.
    try:
        cfg = load_config(project_root)
        if not quiet:
            # One dim header naming the model + chunk strategy: it says WHAT is running (and that it
            # is all local), and on a cold cache it pre-explains the model-download stall.
            strat = str(cfg.get("chunk_strategy", "syntax"))
            strat_label = "def-aligned" if strat == "syntax" else "line-window"
            model = str(cfg.get("model") or "BAAI/bge-small-en-v1.5")
            sep = "." if console.ascii else "·"
            print(console.header("index", project_root))
            print(f"  {console.dim(f'{model} {sep} {strat_label} chunks')}")
            print()
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
                progress=counter,
            ).index(project_root)
            # Seal the live line before the plain summary: commit the ✓ row on real work, else erase
            # it so "Nothing new" / the failure message stands on its own clean line.
            if counter:
                counter.finish(commit=count > 0)
            if count > 0:
                print(f"Indexed {count} chunks")
            elif count < 0:
                # Indexer.index() returns -1 for an unrecoverable failure. `> 0` sent that into
                # the "Nothing new to index" branch, so a total failure read as a clean no-op.
                print("index failed — the indexer could not complete (see the warnings above)")
                failed = True
            else:
                print("Nothing new to index")
        finally:
            db.close()
    except Exception as exc:
        if counter:
            counter.finish(commit=False)
        print(f"index failed: {exc}")
        failed = True

    # best-effort graph reindex — an opaque subprocess, so a ticking elapsed heartbeat is the honest
    # fidelity (no counts cross the boundary): the number keeps moving so a minute-long reindex never
    # reads as a hang, but we never fabricate a percentage we can't see.
    import shutil

    if shutil.which("codebase-memory-mcp"):
        beat = None if quiet else term.LiveHeartbeat(console, "graph reindex").start()
        try:
            from codeintel.reindexer import Reindexer

            Reindexer()._graph_reindex(project_root)
            if beat:
                beat.stop("ok")
        except Exception:
            if beat:
                beat.stop("warn", "skipped")

    # best-effort map refresh after index (fast, kept silent — not worth a checklist row)
    try:
        from codeintel.mapper import MapGenerator
        from codeintel.providers.graph import GraphProvider

        gen = MapGenerator(GraphProvider())
        gen.write(project_root, gen.generate(project_root))
    except Exception:
        pass

    return 1 if failed else 0
