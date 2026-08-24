"""`codeintel prompt` — a paste-to-your-agent setup prompt, tailored to this machine."""

import sys
from typing import Any

from codeintel.commands._common import never_raise, require_dir, resolve_root


@never_raise("prompt unavailable: {exc}")
def run(args: Any) -> int:
    from codeintel import agent_prompt

    fresh = getattr(args, "fresh", False)
    if fresh:
        # A portable template: keep the path the user typed, defaulting to `.` — never this machine's
        # absolute cwd, which a friend on another machine could not use.
        root = getattr(args, "project_root", None) or "."
    else:
        root = resolve_root(args)       # the default reflects THIS repo, so it must actually exist
        err = require_dir(root, "prompt")
        if err:
            print(err, file=sys.stderr)
            return 1

    text = agent_prompt.run_prompt(
        root, agent=args.agent, fresh=fresh, deep=getattr(args, "deep", False)
    )
    # The prompt goes to stdout so `codeintel prompt | pbcopy` grabs exactly it; the how-to line goes
    # to stderr so it is visible in a terminal but never ends up in the copied text.
    print("Copy the block below and paste it to your coding agent (Claude Code / Codex / …):\n",
          file=sys.stderr)
    print(text)
    return 0
