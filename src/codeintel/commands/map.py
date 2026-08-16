"""`codeintel map` — write CODE_INTEL.md, a committable architecture overview."""

from typing import Any

from codeintel.commands._common import never_raise, resolve_root


# Never-raise parity with the MCP code.map handler — degrade, don't crash.
# code=1: this command's job is to WRITE A FILE. Exiting 0 after failing to write it
# reports success to any `make`/CI step gating on $? while nothing was produced.
@never_raise("map failed: {exc}", code=1)
def run(args: Any) -> int:
    from codeintel.injector import Injector
    from codeintel.mapper import MapGenerator
    from codeintel.providers.graph import GraphProvider

    project_root = resolve_root(args)
    try:
        provider = GraphProvider()
    except Exception:
        provider = None
    gen = MapGenerator(provider)

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
    return 0
