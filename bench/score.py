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
import shutil
import subprocess
from dataclasses import dataclass, field

import oracle_py
import oracle_ts
from oracle_py import CALL, UNDECIDABLE, Truth

# `- pkg.mod.sym [CALLS] [?0.75] (path/to/file.py)` and `- module scope of path/to/file.py`
_ROW = re.compile(r"^- (?P<label>.+?)(?P<badges>(?: \[[^\]]+\])*)(?: \((?P<file>[^)]+)\))?$")
_MODULE_SCOPE = re.compile(r"^- module scope of (?P<file>\S+)")
_LSP_REF = re.compile(r"^- (?P<file>[^\s:]+):(?P<line>\d+)")

# The checkout this benchmark ships inside — used only to notice that the `codeintel` on
# PATH is a DIFFERENT build from the source tree the reader is editing.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class _Python:
    """Everything the scorer needs that differs by language, for Python."""

    key = "python"

    def prepare(self, root: str) -> None:
        self.root = root

    def target(self, root: str, def_file: str, symbol: str) -> str:
        return oracle_py.target_from_definition(root, def_file, symbol)

    def truth(self, root: str, qn: str) -> Truth:
        return oracle_py.truth_for(root, qn)

    def enclosing(self, root: str, rel_file: str, line: int) -> str | None:
        """The symbol containing *line*, via the oracle's own descent. None if unreadable."""
        import ast
        try:
            with open(os.path.join(root, rel_file), encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError):
            return None
        return oracle_py._enclosing_map(tree).get(line, "<module>")

    def kinds_at(self, root: str, rel_file: str, line: int, qn: str) -> set[str]:
        if getattr(self, "_aliases", None) is None or self._aliases_for != qn:
            files = oracle_py._walk_py(root)
            self._aliases = oracle_py._alias_set(qn, oracle_py._reexport_map(root, files))
            self._aliases_for = qn
        verdict = oracle_py.label_file(os.path.join(root, rel_file), root, qn, self._aliases)
        return {s.kind for s in verdict.sites if s.line == line}

    _aliases = None
    _aliases_for = None


class _TypeScript:
    """The same seam for TypeScript. One `index_repo` per run, reused by every target."""

    key = "typescript"

    def prepare(self, root: str) -> None:
        self.root = root
        self.repo = oracle_ts.index_repo(root)

    def target(self, root: str, def_file: str, symbol: str) -> str:
        return oracle_ts.target_from_definition(root, def_file, symbol)

    def truth(self, root: str, qn: str) -> Truth:
        return oracle_ts.truth_for(root, qn, self.repo)

    def enclosing(self, root: str, rel_file: str, line: int) -> str | None:
        entry = self.repo.trees.get(os.path.abspath(os.path.join(root, rel_file)))
        if entry is None:
            return None
        tree, src = entry
        return oracle_ts._enclosing_map(tree.root_node, src).get(line, "<module>")

    def kinds_at(self, root: str, rel_file: str, line: int, qn: str) -> set[str]:
        def_file, _, name = qn.partition("::")
        aliases = oracle_ts.alias_set(os.path.join(root, def_file), name, self.repo)
        verdict = oracle_ts.label_file(os.path.join(root, rel_file), root,
                                       os.path.join(root, def_file), name, aliases, self.repo)
        return {s.kind for s in verdict.sites if s.line == line}


LANGUAGES = {"python": _Python, "typescript": _TypeScript}


def _run_codeintel(root: str, op: str, target: str, engine: str, exe: str) -> dict:
    try:
        proc = subprocess.run(
            [exe, "query", "--op", op, "--target", target, "--engine", engine,
             "--project-root", root, "--json"],
            capture_output=True, text=True, timeout=300)
        return json.loads(proc.stdout or "{}")
    except Exception as exc:                                # never let one symbol kill a run
        return {"result": None, "reason": f"harness-error: {type(exc).__name__}: {exc}"}


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
    # Read the structured gaps, for the mirror of the `not-asked` case handled in `lsp_answers`.
    # A repository that is not indexed ON ITS OWN is answered from the enclosing indexed project,
    # which spells every path relative to THAT root: `bench/fixtures/corpus_ts/src/proxy.ts` where
    # the oracle labelled `src/proxy.ts`. No key can ever match, so the arm scores 0% recall with 0
    # spurious — which reads as an engine that found nothing rather than as a harness whose keys
    # never lined up. A silent run of zeros is the exact failure this benchmark exists to catch in
    # the tools it measures, so it is refused here instead of scored.
    gaps = env.get("gaps") or []
    if any(isinstance(g, dict) and g.get("kind") == "ancestor-scope" for g in gaps):
        ans.unavailable = True
        ans.reason = ("ancestor-scope: this repo is not indexed on its own, so the backend answered "
                      "from the project containing it and paths are relative to that root "
                      f"(index it standalone: `codeintel index {root}`)")
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


def lsp_answers(root: str, target_name: str, target_qn: str, exe: str,
                lang) -> tuple[Answer, Answer]:
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
    # `unresolvable` joins these: it is the engine saying its EMPTY answer carries no information,
    # which is a non-answer however confident the count looks. Scoring it would charge an arm a
    # `wrongly silent` for declining honestly — the precise inverse of the reason that column
    # exists, and it would punish the engine for the disclosure this benchmark asked it to make.
    ref_gap = next((g for g in gaps
                    if isinstance(g, dict) and g.get("section") == "references"
                    and g.get("kind") in ("not-asked", "engine-unavailable", "warming",
                                          "unresolvable")), None)
    if not body or ref_gap is not None or "## References" not in body:
        raw.unavailable = classified.unavailable = True
        why = (f"references {ref_gap.get('kind')}: {ref_gap.get('detail') or ''}".strip()
               if ref_gap is not None else "lsp-served-no-references")
        raw.reason = classified.reason = env.get("reason") or why
        return raw, classified
    in_refs = False
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
        enclosing = lang.enclosing(root, file, ln)
        if enclosing is None:
            continue
        key = (file, enclosing)
        raw.callers.add(key)                          # taken at face value: every ref is a caller
        # …and classified: ask the syntax at that exact line what the reference actually is.
        kinds = lang.kinds_at(root, file, ln, target_qn)
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


def _verify_target_sources(root: str, targets: list[tuple[str, str]]) -> None:
    """Fail closed when the oracle cannot read the files that define its targets.

    ``label_file`` treats an OSError like a syntax failure so one bad file can be skipped on a
    normal repository. If the host is denied access to the whole tree, however, that policy turns
    every truth set into an authoritative-looking zero with 100% coverage. Reading each definition
    file up front distinguishes "there are no callers" from "the benchmark saw no source."
    """
    for rel_path, symbol in targets:
        path = os.path.join(root, rel_path)
        try:
            with open(path, "rb") as source:
                source.read(1)
        except OSError as exc:
            raise SystemExit(
                f"benchmark refused to score `{symbol}`: cannot read its source file {path} "
                f"({type(exc).__name__}: {exc}). Grant the host filesystem/privacy access and "
                "run again; an unreadable oracle is not evidence of zero callers."
            ) from exc


def _git_facts(root: str) -> str:
    """``branch @ sha (clean)`` for *root*, or a plain note when it is not a checkout."""
    def _git(*args: str) -> str | None:
        try:
            # `git` by name on purpose: the fact being recorded is what the reader's own
            # PATH resolves, and pinning an absolute path would describe a different tool
            # than the documented command uses.
            proc = subprocess.run(("git", "-C", root, *args),  # noqa: S607
                                  capture_output=True, text=True, timeout=15)
        except Exception:
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    sha = _git("rev-parse", "--short", "HEAD")
    if sha is None:
        return "not a git checkout — the commit this was scored against cannot be recorded"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "(detached)"
    # Only TRACKED modifications count. An untracked `.DS_Store` does not change what was scored,
    # and flagging that tree as dirty would teach a reader to ignore the flag that matters.
    porcelain = _git("status", "--porcelain", "--untracked-files=no") or ""
    dirty = len([line for line in porcelain.splitlines() if line.strip()])
    state = "clean" if not dirty else f"{dirty} tracked file(s) MODIFIED — not a reproducible tree"
    return f"{branch} @ {sha}  ({state})"


def _checkout_version() -> str | None:
    """The version declared by the source tree this file lives in, or None."""
    path = os.path.join(_CHECKOUT, "src", "codeintel", "__init__.py")
    try:
        with open(path, encoding="utf-8") as fh:
            found = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read())
    except OSError:
        return None
    return found.group(1) if found else None


def _provenance(root: str, exe: str) -> None:
    """Print what this run is measuring, before it measures it.

    A table of percentages is a measurement only when the pair (engine build, repository commit)
    that produced it can be named. This harness names neither by itself: it shells out to whatever
    `codeintel` is on PATH — not necessarily the checkout the reader is standing in — and it scores
    whatever is at *root*, which is whatever branch that clone happens to sit on.

    Both moved without anyone noticing. A re-run of this file against a fresh clone of the same
    repository scored graph precision nine points under the table in `bench/README.md` and looked
    like a regression; it was not one. The clone was a different branch with more `_broadcast`
    sites, so the judged population grew from 6 proven non-callers to 11 and the percentage moved
    on its own. Establishing that took a separate investigation, and until it was done a day of
    measurement was unreportable — not wrong, just unattributable, which costs the same.

    Two subprocesses remove the whole class, so this prints on every run, and it is what belongs
    beside any number copied out of one.
    """
    print("provenance — what this run measured")
    print(f"  scored tree  {root}")
    print(f"               {_git_facts(root)}")

    resolved = shutil.which(exe) or exe
    installed = None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        installed = (proc.stdout or proc.stderr).strip() or None
    except Exception as exc:
        installed = f"could not run `{exe} --version` ({type(exc).__name__})"
    print(f"  engine       {installed}  ->  {resolved}")

    try:
        report = subprocess.run([exe, "doctor", root, "--json"],
                                capture_output=True, text=True, timeout=120)
        versions = (json.loads(report.stdout or "{}").get("versions") or {})
    except Exception:
        versions = {}
    backends = ", ".join(f"{k} {v}" for k, v in versions.items()
                         if v and k != "codeintel") or "unknown — `codeintel doctor --json` failed"
    print(f"  backends     {backends}")

    # The skew that this project's own re-runs actually hit: the reader edits the checkout, runs the
    # benchmark, and scores a build installed weeks ago. Nothing else in the output would say so.
    declared, running = _checkout_version(), (versions.get("codeintel") or "")
    if declared and running and declared != running:
        print(f"  !! the checkout at {_CHECKOUT} declares {declared}, but the `codeintel` on PATH "
              f"is {running}.\n"
              f"     This run measures the INSTALLED build, not your working tree.")
    print()

def run(root: str, targets: list[tuple[str, str]], exe: str = "codeintel",
        language: str = "python") -> None:
    _provenance(root, exe)
    _verify_target_sources(root, targets)
    lang = LANGUAGES[language]()
    lang.prepare(root)
    arms = ("graph", "lsp_raw", "lsp_classified")
    direct = {a: Scores() for a in arms}
    impact = {a: Scores() for a in arms}
    covered: list[float] = []

    print(f"repo: {root}  ({language})\n")
    for def_file, symbol in targets:
        qn = lang.target(root, def_file, symbol)
        t: Truth = lang.truth(root, qn)
        covered.append(t.coverage)
        # Decidable population, and the two truths drawn from it. `negatives` are sites the oracle
        # proved are NOT the target; including them is what lets a fabricated caller cost an engine
        # anything. Without them a claim on such a site was silently dropped, and the failure this
        # project has seen at its worst — 32 invented callers for `describe` — scored 100%.
        decidable = t.calls | t.references | t.imports | t.negatives
        true_calls = t.calls
        true_impact = t.calls | t.references        # an import alone does not break when a body moves

        g = graph_answer(root, symbol, exe)
        lr, lc = lsp_answers(root, symbol, qn, exe, lang)
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
