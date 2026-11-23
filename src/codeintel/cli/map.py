"""`codeintel map` — write CODE_INTEL.md, a committable architecture overview."""

from typing import Any

from codeintel.cli._common import never_raise, require_dir, resolve_root


# Never-raise parity with the MCP code.map handler — degrade, don't crash.
# code=1: this command's job is to WRITE A FILE. Exiting 0 after failing to write it
# reports success to any `make`/CI step gating on $? while nothing was produced.
@never_raise("map failed: {exc}", code=1)
def run(args: Any) -> int:
    from codeintel.engines.graph.provider import GraphProvider
    from codeintel.mapping.mapper import MapGenerator
    from codeintel.setup.injector import Injector

    project_root = resolve_root(args)
    problem = require_dir(project_root, "map")
    if problem:
        print(problem)
        return 1
    try:
        provider = GraphProvider()
    except Exception:
        provider = None
    gen = MapGenerator(provider)

    content = gen.generate(project_root, budget_bytes=args.budget)
    path, wrote = gen.write(project_root, content)
    # getattr: a refusal (symlinked CODE_INTEL.md) and a kept stub are both `wrote=False`.
    refusal = getattr(gen, "last_refusal", None)
    if wrote:
        print(f"Wrote {path} ({len(content.encode())} bytes)")
    elif refusal:
        print(refusal)
    else:
        print(f"Kept existing {path} — new map was a stub (graph empty/unindexed; "
              f"run `codeintel index` first)")

    inject_refused: list[str] = []
    if args.inject:
        # create=True: the user asked for the injection by name, so a missing AGENTS.md is created
        # (the advice `codeintel install` prints relies on it).
        injector = Injector()
        inj_path, inj_action = injector.inject(project_root, create=True)
        if inj_path:
            print(f"Inject: {inj_action} block in {inj_path}")
        else:
            print(f"Inject: {inj_action}")
        inject_refused = list(getattr(injector, "refusals", None) or [])
        for line in inject_refused:
            print(line)
    return 1 if (refusal or inject_refused) else 0
