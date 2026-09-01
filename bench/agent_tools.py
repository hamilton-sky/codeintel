"""Tool surfaces for the three arms of the agent-cost benchmark.

The arms differ ONLY in which structural tools are added. Every arm gets `read_file`, `grep` and
`list_files`, because the claim under test is "fewer, sharper tool calls, less re-reading" — a claim
about what an agent CHOOSES to do when it has a structural index, not about what it can do when the
alternative is confiscated. Taking grep away from the codeintel arm would measure tool deprivation
and produce a number that flatters the tool for the wrong reason.

That biases the result AGAINST codeintel: an agent holding both may simply grep anyway, and every
token it spends doing so is charged to the codeintel arm. This is the same direction of bias
`run.py` takes deliberately, and for the same reason — the point is to settle an argument, not to
win one.

Tool RESULTS are capped (`_MAX_RESULT_CHARS`) because an unbounded `read_file` on a 2,000-line file
is a single tool call that can dominate a whole run's token count, and which files are large differs
by arm only through the agent's own choices. The cap is identical across arms and is reported, so a
truncation is visible in the transcript rather than silently changing what was measured.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

_MAX_RESULT_CHARS = 20_000
_HAVE_RG = shutil.which("rg") is not None
_TOOL_TIMEOUT = 240

# ---------------------------------------------------------------------------------------------
# Shared tools — present in every arm.
# ---------------------------------------------------------------------------------------------

_SHARED_SCHEMAS = [
    {
        "name": "grep",
        "description": (
            "Search file contents with a regular expression, recursively. Returns matching lines "
            "prefixed with file path and line number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {
                    "type": "string",
                    "description": (
                        "Directory or file to search, relative to the repository root. "
                        "Defaults to the whole repo."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": "Optional filename glob to restrict the search, e.g. '*.py'.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob, relative to the repository root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "glob": {"type": "string", "description": "Glob such as 'src/**/*.py'."},
            },
            "required": ["glob"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file's contents with line numbers. Provide start_line/end_line to read a slice; "
            "omit them to read the whole file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the repository root."},
                "start_line": {"type": "integer", "description": "1-indexed first line to read."},
                "end_line": {"type": "integer", "description": "1-indexed last line to read."},
            },
            "required": ["path"],
        },
    },
]

# ---------------------------------------------------------------------------------------------
# Arm-specific structural tools.
# ---------------------------------------------------------------------------------------------

_CODEINTEL_SCHEMAS = [
    {
        "name": "code_query",
        "description": (
            "Ask codeintel one structural question about the repository. Operations: "
            "`callers` (who calls a symbol), `callees` (what a symbol calls), `impact` (what a "
            "change to a symbol ripples into), `chain` (call chain), `symbol` (where a symbol is "
            "defined), `search` (natural-language 'find the code that does Y'), `overview`, "
            "`hotspots`, `context`. Returns a result envelope: a null `result` with a `reason` "
            "means nothing was found, not an error. Check `confidence` — `partial` means a named "
            "part of the answer could not be retrieved and `gaps` says which."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "search", "symbol", "callers", "callees", "impact",
                        "chain", "overview", "context", "hotspots",
                    ],
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Symbol name, or a natural-language query when op=search. "
                        "Omit for overview/hotspots."
                    ),
                },
            },
            "required": ["op"],
        },
    },
]

_BACKEND_SCHEMAS = [
    {
        "name": "search_graph",
        "description": (
            "Search the code knowledge graph for symbols by name pattern. Returns rows of "
            "name/label/lines/in/out fan counts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_pattern": {"type": "string", "description": "Symbol name or pattern to find."},
                "label": {"type": "string", "description": "Optional node label filter, e.g. 'Function'."},
            },
            "required": ["name_pattern"],
        },
    },
    {
        "name": "trace_path",
        "description": "Trace call relationships from a function — callers or callees.",
        "input_schema": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string"},
                "mode": {"type": "string", "enum": ["calls", "callers", "data_flow"]},
            },
            "required": ["function_name"],
        },
    },
    {
        "name": "get_code_snippet",
        "description": "Fetch the exact source of a symbol by its qualified name.",
        "input_schema": {
            "type": "object",
            "properties": {"qualified_name": {"type": "string"}},
            "required": ["qualified_name"],
        },
    },
]

ARMS = ("grep_only", "codeintel", "raw_backend")

_ARM_EXTRA = {
    "grep_only": [],
    "codeintel": _CODEINTEL_SCHEMAS,
    "raw_backend": _BACKEND_SCHEMAS,
}


def schemas_for(arm: str) -> list[dict]:
    """Tool definitions for one arm. Order is fixed so the serialized tool block is stable."""
    if arm not in _ARM_EXTRA:
        raise SystemExit(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")
    return _SHARED_SCHEMAS + _ARM_EXTRA[arm]


def _clip(text: str) -> str:
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + f"\n... [truncated at {_MAX_RESULT_CHARS} chars]"


def _run(argv: list[str], cwd: str | None = None, env: dict | None = None) -> str:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=_TOOL_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        return f"[tool timed out after {_TOOL_TIMEOUT}s]"
    except OSError as exc:
        return f"[could not run {argv[0]}: {exc}]"
    out = (proc.stdout or "") + (proc.stderr or "" if proc.returncode != 0 else "")
    return out.strip() or "[no output]"


class ToolRunner:
    """Executes tool calls against one repository. One instance per benchmark run.

    `calls` counts every tool invocation, which is one of the two headline numbers. It counts
    invocations rather than distinct tools deliberately: an agent that greps eleven times has spent
    eleven round trips whatever the tool surface allowed.
    """

    def __init__(self, repo_root: str, arm: str, project_name: str | None = None) -> None:
        self.repo_root = os.path.abspath(repo_root)
        self.arm = arm
        self.project_name = project_name
        self.calls = 0
        self.log: list[dict] = []

    # -- shared -------------------------------------------------------------------------------

    def _grep(self, pattern: str, path: str | None, glob: str | None) -> str:
        target = os.path.join(self.repo_root, path) if path else self.repo_root
        if _HAVE_RG:
            # ripgrep, not `grep`, because that is what an agent's grep tool actually is — and
            # because BRE is the wrong dialect for a model's output. Under plain `grep`, a pattern
            # as ordinary as `\.query\(` dies with "parentheses not balanced", since BRE reads
            # `\(` as a group. Charging the grep arm for that would have measured the harness's
            # choice of regex flavour and reported it as the cost of not having an index.
            # `-H` is explicit and load-bearing: ripgrep's `-I` is `--no-filename` (it is
            # "ignore binary" only in `grep`), and with the path stripped this arm cannot answer a
            # question that asks which FILE something is in. That would have scored grep_only near
            # zero on most of the question set and handed codeintel a win the tool had not earned —
            # a harness artifact reported as a product difference.
            argv = ["rg", "-n", "-H", "--no-heading", "--no-messages"]
            if glob:
                argv += ["-g", glob]
            argv += ["-e", pattern, target]
        else:
            argv = ["grep", "-rnIE", "--exclude-dir=.git", "--exclude-dir=__pycache__",
                    "--exclude-dir=node_modules", "--exclude-dir=.venv"]
            if glob:
                argv += [f"--include={glob}"]
            argv += ["-e", pattern, target]
        out = _clip(_run(argv))
        # Paths are made repo-relative so the model never sees the absolute layout of this machine,
        # which would differ between runs and is not part of the question.
        return out.replace(self.repo_root + os.sep, "").replace(self.repo_root, ".")

    def _list_files(self, glob: str) -> str:
        out = _run(["sh", "-c", f"cd {self.repo_root!r} && ls -1 {glob} 2>/dev/null | head -300"])
        return out

    def _read_file(self, path: str, start: int | None, end: int | None) -> str:
        full = os.path.join(self.repo_root, path)
        if not os.path.isfile(full):
            return f"[no such file: {path}]"
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return f"[could not read {path}: {exc}]"
        lo = max(1, start or 1)
        hi = min(len(lines), end or len(lines))
        body = "".join(f"{i:6d}\t{lines[i - 1]}" for i in range(lo, hi + 1))
        return _clip(body) or "[empty]"

    # -- codeintel arm ------------------------------------------------------------------------

    def _code_query(self, op: str, target: str | None) -> str:
        argv = ["uv", "run", "codeintel", "query", "--op", op, "--json",
                "--project-root", self.repo_root]
        if target:
            argv += ["--target", target]
        # Run from the codeintel checkout so `uv run` resolves this project's own environment even
        # when the repository under test is a different tree.
        out = _run(argv, cwd=_CODEINTEL_CHECKOUT)
        return _clip(out)

    # -- raw_backend arm ----------------------------------------------------------------------

    def _backend(self, tool: str, args: dict) -> str:
        if not self.project_name:
            return "[no indexed project name was supplied for this repository]"
        args = dict(args)
        args["project"] = self.project_name
        out = _run(["codebase-memory-mcp", "cli", "--json", tool, json.dumps(args)])
        # The backend writes allocator/init lines to stderr and wraps its answer in an MCP content
        # envelope. Unwrap to the text payload so the arm is charged for the ANSWER, not for the
        # backend's logging — charging it for log noise would flatter codeintel, which strips it.
        try:
            payload = json.loads(out)
            blocks = payload.get("content") or []
            text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            return _clip(text or out)
        except (ValueError, AttributeError):
            return _clip(out)

    # -- dispatch -----------------------------------------------------------------------------

    def call(self, name: str, args: dict) -> str:
        self.calls += 1
        if name == "grep":
            result = self._grep(args.get("pattern", ""), args.get("path"), args.get("glob"))
        elif name == "list_files":
            result = self._list_files(args.get("glob", "*"))
        elif name == "read_file":
            result = self._read_file(
                args.get("path", ""), args.get("start_line"), args.get("end_line"))
        elif name == "code_query":
            result = self._code_query(args.get("op", "search"), args.get("target"))
        elif name in ("search_graph", "trace_path", "get_code_snippet"):
            result = self._backend(name, args)
        else:
            result = f"[unknown tool {name!r}]"
        self.log.append({"tool": name, "args": args, "chars": len(result)})
        return result


# Where this checkout lives — needed so the codeintel arm can `uv run` this project's CLI while
# pointed at a different repository.
_CODEINTEL_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
