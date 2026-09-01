"""Agent-cost benchmark: tokens and tool calls to answer a real question, per arm.

    python bench/agent_bench.py --dry-run                       # no API calls; loop + scoring + tools
    python bench/agent_bench.py --dry-run --repo-key pathly-adapters
    python bench/agent_bench.py --repo-key pathly-adapters      # the real thing; costs money
    python bench/agent_bench.py --questions q_pa_collision --arms codeintel

`--dry-run` is not just a smoke test: it runs the real loop against the real tools, and answers each
question from its `canned_answer`, which makes it a POSITIVE CONTROL over the ground truth. An
unsatisfiable `must_include` regex would otherwise mark every arm wrong on that question forever and
read as a product finding. Run it after touching `questions.py`.

WHY THIS EXISTS. `README.md` claims codeintel produces "fewer, sharper tool calls, less
re-reading". That is a statement about the world made by a project whose own recurring defect class
is *a fact asserted about the world by code that never checked it*, and it is the number this market
compares on. `bench/run.py` measures call-edge ACCURACY, which is a different axis entirely and
cannot support the claim.

WHAT MAKES THE NUMBER MEAN SOMETHING. Two design choices carry the whole benchmark:

1. **Correctness is scored jointly with cost, and a wrong answer is charged.** A token count alone
   is trivially won by giving up early, and "57% fewer tokens" with no accuracy column is the
   standard way this measurement gets faked. The headline figure here is therefore **cost per
   CORRECT answer**, not cost per question. An arm that answers cheaply and wrongly scores worse
   than one that answers expensively and rightly, which is the actual preference an agent host has.
2. **Every arm keeps grep and read_file** (see `agent_tools.py`). The arms differ only by the
   structural tools ADDED. This biases against codeintel and is the honest direction.

WHAT IT DOES NOT MEASURE. One model, five questions per repository, ONE RUN EACH — there is no
variance estimate, so a small spread between arms is not a result and should not be quoted as one.
Prompt caching is left OFF so the token column is the raw thing the claim is about; enabling it
would cut cost unevenly across arms because their tool blocks differ in size. Questions are
structural by construction, so this measures the population where an index should help, not an
average day. And two repositories is two, not a sample: `--repo-key codeintel` is this project's own
tree, where the author's familiarity shaped the questions, and `pathly-adapters` is the counterweight
(3,284 files, polyglot, not written for this benchmark). Report both or neither — a number from the
self-referential set alone is the weaker half.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_tools import ARMS, ToolRunner, schemas_for
from questions import REPOS, Question, by_key

MODEL = "claude-opus-5"
MAX_TOKENS = 32_000
MAX_TURNS = 25

# Claude Opus 5 list pricing, USD per 1M tokens.
_IN_PER_M = 5.00
_OUT_PER_M = 25.00
_CACHE_READ_PER_M = 0.50    # ~0.1x input
_CACHE_WRITE_PER_M = 6.25   # ~1.25x input

SYSTEM = (
    "You are answering a factual question about a software repository you have tool access to.\n\n"
    "Work efficiently: use the fewest tool calls that let you answer with confidence, and prefer "
    "reading a targeted slice of a file over reading the whole file.\n\n"
    "When you have the answer, state it directly and completely in your final message — include "
    "the specific file paths, line numbers, names and values you were asked for. Your final "
    "message is the answer that gets graded; do not end your turn with a plan or a summary of what "
    "you would do next.\n\n"
    "If you cannot establish something, say so plainly rather than guessing. A confident wrong "
    "answer is scored worse than an admission that you could not determine it."
)


@dataclass
class RunResult:
    arm: str
    question: str
    correct: bool
    missing: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    turns: int = 0
    wall_seconds: float = 0.0
    stop_reason: str = ""
    answer: str = ""
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens * _IN_PER_M
                + self.output_tokens * _OUT_PER_M
                + self.cache_read_tokens * _CACHE_READ_PER_M
                + self.cache_write_tokens * _CACHE_WRITE_PER_M) / 1_000_000


def score(question: Question, answer: str) -> tuple[bool, list[str], list[str]]:
    """An answer is correct when every `must_include` matches and no `must_forbid` does.

    Deliberately not an LLM judge. A judge would add a second model's opinion to a measurement whose
    entire purpose is to replace an unverified assertion, and it would make the result unreproducible
    without spending money. The cost is that these regexes reward an answer that names the right
    file for the wrong reason — which is why `must_forbid` exists and why the full answer text is
    kept in the JSON output for a human to read.
    """
    missing = [p for p in question.must_include
               if not re.search(p, answer, re.IGNORECASE)]
    hits = [p for p in question.must_forbid
            if re.search(p, answer, re.IGNORECASE)]
    return (not missing and not hits), missing, hits


def _blocks_to_text(content) -> str:
    out = []
    for block in content:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype == "text":
            out.append(getattr(block, "text", None) or block.get("text", ""))
    return "\n".join(out)


def run_one(send, runner: ToolRunner, question: Question, arm: str,
            max_turns: int = MAX_TURNS) -> RunResult:
    """One agent loop. `send(messages)` returns an object with .content/.stop_reason/.usage.

    The loop is written manually rather than with the SDK tool runner so that every token and every
    tool call is counted at the point it happens — the two numbers this benchmark exists to produce
    should not depend on a helper's internal behaviour.
    """
    res = RunResult(arm=arm, question=question.key, correct=False)
    messages: list[dict] = [{"role": "user", "content": question.prompt}]
    started = time.monotonic()

    for _ in range(max_turns):
        res.turns += 1
        try:
            response = send(messages)
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"
            break

        usage = getattr(response, "usage", None)
        if usage is not None:
            res.input_tokens += getattr(usage, "input_tokens", 0) or 0
            res.output_tokens += getattr(usage, "output_tokens", 0) or 0
            res.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            res.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        res.stop_reason = getattr(response, "stop_reason", "") or ""

        # Check the stop reason before reading content: a refusal carries no usable answer, and
        # treating one as an empty answer would silently score it as a wrong answer instead of a
        # run that never happened.
        if res.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            res.error = f"refusal: {getattr(details, 'category', None)}"
            break

        # Append the assistant turn wholesale. Thinking blocks must be echoed back unchanged, so
        # extracting text and appending that would break the next request on a thinking model.
        messages.append({"role": "assistant", "content": response.content})

        if res.stop_reason != "tool_use":
            res.answer = _blocks_to_text(response.content)
            break

        # Every tool_result for this turn goes back in ONE user message. Splitting them teaches the
        # model to stop issuing parallel calls, which would change the tool-call count being measured.
        results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            out = runner.call(block.name, block.input or {})
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": out,
            })
        if not results:
            res.answer = _blocks_to_text(response.content)
            break
        messages.append({"role": "user", "content": results})
    else:
        res.error = f"hit max_turns={max_turns} without a final answer"

    res.wall_seconds = round(time.monotonic() - started, 2)
    res.tool_calls = runner.calls
    res.correct, res.missing, res.forbidden_hits = score(question, res.answer)
    return res


# -------------------------------------------------------------------------------------------------
# Senders
# -------------------------------------------------------------------------------------------------

def make_live_sender(arm: str, effort: str):
    import anthropic

    client = anthropic.Anthropic()
    tools = schemas_for(arm)

    def send(messages):
        # Streaming, because an agentic turn with adaptive thinking and a 32k ceiling can outrun the
        # non-streaming HTTP timeout. get_final_message() gives the same Message object back.
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=messages,
        ) as stream:
            return stream.get_final_message()

    return send


class _StubBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _StubUsage:
    def __init__(self):
        self.input_tokens = 1200
        self.output_tokens = 300
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _StubResponse:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _StubUsage()
        self.stop_details = None


def make_stub_sender(arm: str, question: Question):
    """A scripted model, so `--dry-run` exercises the real loop, the real tools and the real scorer.

    It answers correctly on purpose: the dry run is checking that the plumbing produces a scored
    result, not that a model can answer. It really does invoke a tool, so a broken tool surface
    (a bad backend argument, a missing CLI) fails here rather than during a paid run.
    """
    probe = ({"name": "code_query", "input": {"op": "callers", "target": "Gateway.query"}}
             if arm == "codeintel" else
             {"name": "search_graph", "input": {"name_pattern": "Gateway"}}
             if arm == "raw_backend" else
             {"name": "grep", "input": {"pattern": "Gateway", "glob": "*.py"}})
    state = {"turn": 0}

    def send(messages):
        state["turn"] += 1
        if state["turn"] == 1:
            return _StubResponse(
                [_StubBlock(type="tool_use", id="stub_1",
                            name=probe["name"], input=probe["input"])],
                "tool_use",
            )
        # The question's own canned correct answer, so the dry run is a POSITIVE CONTROL: it proves
        # every `must_include` is satisfiable by prose a model could plausibly write. An unsatisfiable
        # regex — a stray `\b`, an unescaped `[./]` — marks every arm wrong on that question forever
        # and reads as a product finding rather than a typo. Deriving the stub answer from the
        # regexes themselves (the earlier approach) could not catch that, because it tested the
        # patterns against a string built out of the patterns.
        answer = question.canned_answer or (
            "[no canned_answer for this question — dry run cannot verify its scoring]")
        return _StubResponse([_StubBlock(type="text", text=answer)], "end_turn")

    return send


# -------------------------------------------------------------------------------------------------
# Reporting
# -------------------------------------------------------------------------------------------------

def report(results: list[RunResult], n_questions: int) -> str:
    lines: list[str] = []
    by_arm: dict[str, list[RunResult]] = {}
    for r in results:
        by_arm.setdefault(r.arm, []).append(r)

    lines.append("")
    lines.append(f"{'arm':<13} {'correct':>9} {'tok/q':>9} {'calls/q':>8} "
                 f"{'sec/q':>7} {'$ total':>9} {'$/correct':>10}")
    lines.append("-" * 70)
    for arm in ARMS:
        rs = by_arm.get(arm)
        if not rs:
            continue
        n = len(rs)
        ncorrect = sum(1 for r in rs if r.correct)
        cost = sum(r.cost_usd for r in rs)
        per_correct = (cost / ncorrect) if ncorrect else float("nan")
        lines.append(
            f"{arm:<13} {f'{ncorrect}/{n}':>9} {sum(r.total_tokens for r in rs) // n:>9,} "
            f"{sum(r.tool_calls for r in rs) / n:>8.1f} "
            f"{sum(r.wall_seconds for r in rs) / n:>7.1f} "
            f"{cost:>9.4f} {per_correct:>10.4f}"
        )

    lines.append("")
    lines.append("Per question (tokens / tool calls / correct):")
    qkeys = sorted({r.question for r in results})
    header = f"  {'question':<22}" + "".join(f"{a:>22}" for a in ARMS if by_arm.get(a))
    lines.append(header)
    for qk in qkeys:
        row = f"  {qk:<22}"
        for arm in ARMS:
            if not by_arm.get(arm):
                continue
            match = next((r for r in by_arm[arm] if r.question == qk), None)
            if match is None:
                row += f"{'-':>22}"
            else:
                mark = "ok" if match.correct else "WRONG"
                row += f"{f'{match.total_tokens:,}/{match.tool_calls}/{mark}':>22}"
        lines.append(row)

    failures = [r for r in results if not r.correct]
    if failures:
        lines.append("")
        lines.append("Not correct — why:")
        for r in failures:
            why = r.error or ("forbidden: " + ", ".join(r.forbidden_hits) if r.forbidden_hits
                              else "missing: " + ", ".join(r.missing))
            lines.append(f"  [{r.arm}] {r.question}: {why}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-key", default="codeintel", choices=sorted(REPOS),
                    help="Which question set to run, and the repository it was verified against.")
    ap.add_argument("--repo", default="",
                    help="Override the repository path for --repo-key (rarely needed).")
    ap.add_argument("--project", default="",
                    help="Override the indexed project name used by the raw_backend arm.")
    ap.add_argument("--arms", default=",".join(ARMS),
                    help=f"Comma-separated subset of: {', '.join(ARMS)}")
    ap.add_argument("--questions", default="",
                    help="Comma-separated question keys (default: all).")
    ap.add_argument("--effort", default="high",
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Scripted model — no API calls, no spend. Verifies loop, tools and scoring.")
    ap.add_argument("--out", default="", help="Write the full JSON result here.")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for arm in arms:
        if arm not in ARMS:
            raise SystemExit(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")
    target = REPOS[args.repo_key]
    repo_path = args.repo or target.path
    project = args.project or target.project
    questions = by_key(args.repo_key,
                       [q.strip() for q in args.questions.split(",") if q.strip()])

    # A question set is only meaningful against the tree its ground truth was verified on. A missing
    # clone would otherwise score every arm zero and read as a product finding rather than a setup
    # problem — the same trap `bench/run.py` fixed by refusing to score against an empty tree.
    if not os.path.isdir(repo_path):
        print(f"repository for --repo-key {args.repo_key} not found at {repo_path}\n"
              f"Clone it, or point at it with --repo / the environment variable in questions.py.",
              file=sys.stderr)
        return 2

    if not args.dry_run and not (os.environ.get("ANTHROPIC_API_KEY")
                                or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("No ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment.\n"
              "Export one, or run with --dry-run to verify the harness without spending anything.",
              file=sys.stderr)
        return 2

    print(f"repo-key={args.repo_key}  repo={repo_path}")
    print(f"arms={','.join(arms)}  questions={len(questions)}  "
          f"model={MODEL}  effort={args.effort}"
          f"{'  [DRY RUN — scripted model, no spend]' if args.dry_run else ''}")

    results: list[RunResult] = []
    for arm in arms:
        for q in questions:
            runner = ToolRunner(repo_path, arm, project_name=project)
            sender = (make_stub_sender(arm, q) if args.dry_run
                      else make_live_sender(arm, args.effort))
            res = run_one(sender, runner, q, arm)
            results.append(res)
            flag = "ok   " if res.correct else "WRONG"
            print(f"  [{flag}] {arm:<12} {q.key:<22} "
                  f"{res.total_tokens:>7,} tok  {res.tool_calls:>2} calls  "
                  f"{res.wall_seconds:>6.1f}s  ${res.cost_usd:.4f}"
                  + (f"  ({res.error})" if res.error else ""))

    print(report(results, len(questions)))

    if args.out:
        payload = {
            "model": MODEL, "effort": args.effort,
            "repo_key": args.repo_key, "repo": repo_path, "project": project,
            "dry_run": args.dry_run,
            "results": [asdict(r) | {"total_tokens": r.total_tokens,
                                     "cost_usd": round(r.cost_usd, 6)}
                        for r in results],
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
