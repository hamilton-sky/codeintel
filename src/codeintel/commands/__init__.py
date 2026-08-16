"""One module per `codeintel` subcommand, each exposing `run(args) -> int`.

Deliberately empty of imports: `__main__` resolves a command to its module lazily, so `codeintel
serve` never pays for the semantic engine's import cost and vice versa. Importing the submodules
here would undo that for every command at once.
"""
