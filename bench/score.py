"""Score each engine's "who calls this" against the labelled truth, per question.

Three arms, because the argument this benchmark exists to settle was about which of them to trust:

    graph            what codeintel reports today, whole stack, as an agent sees it
    lsp_raw          the language server's references, taken as callers — the design I proposed
                     and then measured at 56% precision on one hand-checked symbol
    lsp_classified   the same references, with the syntax at each location deciding whether it is
                     actually a call — "LSP locates, syntax classifies"

`graph` is measured through codeintel's own JSON envelope rather than by querying the backend
directly. That is deliberate: the number that matters is what an agent receives, which includes every
filter, collapse and rename the provider applies on the way out. Measuring the backend would flatter
the tool by skipping its own rendering.

Two questions are scored separately, because they have opposite failure costs and a single F-score
would hide that:

    direct callers   precision-first. A fabricated caller sends an agent to edit unrelated code.
    change impact    recall-first. A missed dependant is how live code gets broken, and this is the
                     question `changed`/`impact` answer.

`safe_to_delete` is reported as its own count: how often an engine returns NOTHING for a symbol that
truth says has callers. It is the single most consequential error an engine of this kind can make,
and averaging it into precision would bury it.

Scoring is restricted to sites the oracle was willing to judge, which now includes the ones it judged
to be NOT the target. That third population is load-bearing. While truth held only positives, a
claimed caller on an unjudged site was dropped rather than charged, so the fabrication failure mode —
matching a bare name across files that never import the symbol — cost an engine exactly nothing.
Every arm scored 100% precision against 32 invented callers, because the symbol left the population
altogether. Proven negatives are what turn that back into a measurement.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field

from oracle_py import CALL, UNDECIDABLE, Truth, label_file, truth_for

# `- pkg.mod.sym [CALLS] [?0.75] (path/to/file.py)` and `- module scope of path/to/file.py`
_ROW = re.compile(r"^- (?P<label>.+?)(?P<badges>(?: \[[^\]]+\])*)(?: \((?P<file>[^)]+)\))?$")
_MODULE_SCOPE = re.compile(r"^- module scope of (?P<file>\S+)")
_LSP_REF = re.compile(r"^- (?P<file>[^\s:]+):(?P<line>\d+)")


@dataclass
class Answer:
    """One engine's reply, reduced to comparable keys."""

    callers: set[tuple[str, str]] = field(default_factory=set)   # claimed as CALLS
    others: set[tuple[str, str]] = field(default_factory=set)    # claimed, but not as a call
    reason: str | None = None
    # The engine could not answer here — as opposed to answering that there is nothing.
    # These are different facts and averaging them produces a lie of exactly the kind this
    # project exists to stop. The first run of this benchmark put the LSP arms at 0% recall
    # on one repository; the cause was not LSP quality but that repo's .serena/project.yml
    # naming only `typescript` while every target was Python — the same false-healthy
    # `codeintel doctor` was taught to catch. Unanswerable symbols are excluded from
    # precision and recall, and counted on their own.
    unavailable: bool = False

    @property
    def everything(self) -> set[tuple[str, str]]:
        return self.callers | self.others


def _run_codeintel(root: str, op: str, target: str, engine: str, exe: str) -> dict:
    try:
        proc = subprocess.run(
            [exe, "query", "--op", op, "--target", target, "--engine", engine,
             "--project-root", root, "--json"],
            capture_output=True, text=True, timeout=300)
        return json.loads(proc.stdout or "{}")
    except Exception as exc:                                # never let one symbol kill a run
        return {"result": None, "reason": f"harness-error: {type(exc).__name__}: {exc}"}


def _enclosing_of(root: str, rel_file: str, line: int) -> str | None:
    """The symbol containing *line*, via the oracle's own descent. None if unreadable."""
    import ast

    from oracle_py import _enclosing_map
    try:
        with open(os.path.join(root, rel_file), encoding="utf-8",
                  errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return None
    return _enclosing_map(tree).get(line, "<module>")


def graph_answer(root: str, target_name: str, exe: str) -> Answer:
    """Parse codeintel's rendered `callers` rows back into keys.

    The rendering is the product under test, so it is what gets parsed. `module scope of <file>` maps
    to `<module>` — the same name the oracle gives a top-level site — which is the whole reason the
    comparison key is (file, enclosing symbol) rather than a qualified name: the two engines spell
    qualified names differently and neither spelling is the fact being measured.
    """
    env = _run_codeintel(root, "callers", target_name, "graph", exe)
    ans = Answer(reason=env.get("reason"))
    if str(env.get("reason") or "") in (
            "engine-unavailable", "backend-incompatible", "project-not-indexed"
    ) or str(env.get("reason") or "").startswith("harness-error"):
        ans.unavailable = True
        return ans
    body = env.get("result") or ""
    for raw in body.splitlines():
        if not raw.startswith("- "):
            continue
        mod = _MODULE_SCOPE.match(raw)
        if mod:
            key = (mod.group("file"), "<module>")
            (ans.callers if "[CALL_REFERENCE]" not in raw else ans.others).add(key)
            continue
        m = _ROW.match(raw)
        if not m or not m.group("file"):
            continue
        file, label = m.group("file"), m.group("label")
        # Strip the module prefix off the qualified name to leave the enclosing symbol, using the
        # file path as the authority for where the module ends.
        stem = os.path.splitext(file)[0].replace("/", ".")
        enclosing = label
        for cut in (stem, stem.split(".", 1)[-1] if "." in stem else stem):
            if label.startswith(cut + "."):
                enclosing = label[len(cut) + 1:]
                break
        else:
            enclosing = label.rpartition(".")[2] or label
        key = (file, enclosing)
        kinds = m.group("badges") or ""
        (ans.others if "CALL_REFERENCE" in kinds or "USAGE" in kinds else ans.callers).add(key)
    return ans


def lsp_answers(root: str, target_name: str, target_qn: str, exe: str) -> tuple[Answer, Answer]:
    """`(lsp_raw, lsp_classified)` — references as callers, and references filtered by syntax."""
    env = _run_codeintel(root, "symbol", target_name, "lsp", exe)
    raw, classified = Answer(reason=env.get("reason")), Answer(reason=env.get("reason"))
    body = env.get("result") or ""
    # Use codeintel's OWN structured gap rather than sniffing the prose. A repository whose
    # .serena/project.yml omits the target's language yields a body that still contains a
    # "## References" heading — "## References — not retrieved" — so a string check passes
    # and the arm scores 0% recall for a reason that has nothing to do with LSP quality.
    # The envelope already says so exactly: a `references` gap of kind `not-asked`.
    gaps = env.get("gaps") or []
    not_asked = any(
        isinstance(g, dict) and g.get("section") == "references"
        and g.get("kind") in ("not-asked", "engine-unavailable", "warming")
        for g in gaps)
    if not body or not_asked or "## References" not in body:
        raw.unavailable = classified.unavailable = True
        why = ("references not-asked: the language server did not resolve this symbol"
               if not_asked else "lsp-served-no-references")
        raw.reason = classified.reason = env.get("reason") or why
        return raw, classified
    in_refs = False
    aliases = None
    for line in body.splitlines():
        if line.startswith("## References"):
            in_refs = True
            continue
        if not in_refs or not line.startswith("- "):
            continue
        m = _LSP_REF.match(line)
        if not m:
            continue
        file, ln = m.group("file"), int(m.group("line"))
        enclosing = _enclosing_of(root, file, ln)
        if enclosing is None:
            continue
        key = (file, enclosing)
        raw.callers.add(key)                          # taken at face value: every ref is a caller
        # …and classified: ask the syntax at that exact line what the reference actually is.
        if aliases is None:
            from oracle_py import _alias_set, _reexport_map, _walk_py
            aliases = _alias_set(target_qn, _reexport_map(root, _walk_py(root)))
        verdict = label_file(os.path.join(root, file), root, target_qn, aliases)
        kinds = {s.kind for s in verdict.sites if s.line == ln}
        if CALL in kinds:
            classified.callers.add(key)
        elif kinds and kinds != {UNDECIDABLE}:
            classified.others.add(key)
    return raw, classified


@dataclass
class Scores:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    said_nothing_wrongly: int = 0
    symbols: int = 0
    unavailable: int = 0

    def add(self, claimed: set, true: set, decidable: set,
            unavailable: bool = False) -> None:
        if unavailable:
            self.unavailable += 1
            return
        # Only sites the oracle was willing to judge count. A claim about an undecidable site is
        # neither credited nor penalised — scoring it either way would smuggle in an opinion the
        # oracle explicitly declined to hold.
        claimed = claimed & decidable
        self.tp += len(claimed & true)
        self.fp += len(claimed - true)
        self.fn += len(true - claimed)
        self.symbols += 1
        if true and not claimed:
            self.said_nothing_wrongly += 1

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return (self.tp / d) if d else None

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return (self.tp / d) if d else None


def _pct(v: float | None) -> str:
    return "  n/a" if v is None else f"{v * 100:4.0f}%"


def run(root: str, targets: list[tuple[str, str]], exe: str = "codeintel") -> None:
    arms = ("graph", "lsp_raw", "lsp_classified")
    direct = {a: Scores() for a in arms}
    impact = {a: Scores() for a in arms}
    covered: list[float] = []

    print(f"repo: {root}\n")
    for def_file, symbol in targets:
        from oracle_py import target_from_definition
        qn = target_from_definition(root, def_file, symbol)
        t: Truth = truth_for(root, qn)
        covered.append(t.coverage)
        # Decidable population, and the two truths drawn from it. `negatives` are sites the oracle
        # proved are NOT the target; including them is what lets a fabricated caller cost an engine
        # anything. Without them a claim on such a site was silently dropped, and the failure this
        # project has seen at its worst — 32 invented callers for `describe` — scored 100%.
        decidable = t.calls | t.references | t.imports | t.negatives
        true_calls = t.calls
        true_impact = t.calls | t.references        # an import alone does not break when a body moves

        g = graph_answer(root, symbol, exe)
        lr, lc = lsp_answers(root, symbol, qn, exe)
        got = {"graph": g, "lsp_raw": lr, "lsp_classified": lc}
        for a in arms:
            direct[a].add(got[a].callers, true_calls, decidable, got[a].unavailable)
            impact[a].add(got[a].everything, true_impact, decidable, got[a].unavailable)

        print(f"  {symbol:<34} truth: {len(true_calls)} call(s), "
              f"{len(t.references)} ref(s), {len(t.negatives)} proven non-caller(s), "
              f"coverage {t.coverage:.0%}"
              + (f"  [oracle abstained on {len(t.undecidable)}]" if t.undecidable else ""))
        for a in arms:
            if got[a].unavailable:
                print(f"      {a:<15} UNANSWERED — {got[a].reason}"
                      f"  (excluded from scoring)")
                continue
            claimed = got[a].callers & decidable
            miss = true_calls - claimed
            extra = claimed - true_calls
            note = got[a].reason or ""
            print(f"      {a:<15} claimed {len(claimed):>2}  "
                  f"missed {len(miss):>2}  spurious {len(extra):>2}  {note}")

    print("\n" + "=" * 74)
    print(f"{'arm':<16}{'DIRECT CALLERS':>22}{'CHANGE IMPACT':>22}{'':>8}")
    print(f"{'':<16}{'precision':>11}{'recall':>11}{'precision':>11}{'recall':>11}"
          f"{'  wrongly silent':>16}")
    for a in arms:
        d, i = direct[a], impact[a]
        print(f"{a:<16}{_pct(d.precision):>11}{_pct(d.recall):>11}"
              f"{_pct(i.precision):>11}{_pct(i.recall):>11}"
              f"{d.said_nothing_wrongly:>10} / {d.symbols}"
              + (f"   [{d.unavailable} unanswered]" if d.unavailable else ""))
    if covered:
        print(f"\noracle coverage: {sum(covered) / len(covered):.0%} mean "
              f"(sites it judged, of sites mentioning the symbol)")
    print("`wrongly silent` = returned nothing for a symbol that has callers — the deletion trap.")
