"""`code.query op=changed target=<ref>`: who uses the functions this branch removes or rewrites.

Split out of `graph_ops.py` the way `graph_ops.py` was split out of the provider: the op there is
ONE method, and this one is a pipeline — git for what changed, `symbol_diff` for which definitions,
the graph for who calls the ones that still exist, and `git grep` for who still mentions the ones
that do not — with an envelope to keep honest at every join. It would have doubled `graph_ops.py`
to put it beside the ops it is not like.

THE QUESTION, AND WHY EACH HALF NEEDS A DIFFERENT SOURCE. A symbol the branch REWROTE is in the
graph, so its callers are an ordinary `callers` answer — and this module asks `_op_callers` for them
rather than running a second query and a second labeller, so a row is `resolved` or `name-matched`
by exactly the rule `callers` applies. A symbol the branch REMOVED is not in the graph at all: the
index was built from a tree that no longer has it. Its surviving users can only come from text, and
a text match is not a call, so those rows are published as `name-matched` mentions and the group is
labelled `discovery` — never `evidence`.

THE SPLIT THAT IS THE POINT. Each group's callers are divided into those the diff ALSO touched
(probably edited together with the symbol) and those it did NOT. The second list is the one that
can break: it is what a reviewer would otherwise have to find by hand.

HOW IT STAYS HONEST. Every way the list can be short of the truth becomes a named gap rather than a
quiet omission: an unreadable or unsupported file, a symbol cap, a caller lookup that failed, a
symbol the index has never seen, a group with no recorded callers (which is not proof it has none),
and an index that plausibly predates the tree. `safe_for_destructive` is the existing derivation —
no gap, no unverified row, nothing withheld — so it is false for any answer containing a text
mention or a gap, whatever else is true of it.

That includes what the op does not COMPARE, which is the easy one to forget: a changed file that is
not source it reads (a manifest, a config, a language it has no reading of), a file changed only
outside any definition, a file that was renamed (so its importers were never asked about), and a
removed name that is still written in a non-source file. Each is counted, named and raised whenever
the count is above zero — "these were not compared" says nothing about whether they are harmless,
and an answer that is silent about them reads as one that checked.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from codeintel import qualifier_scan
from codeintel.changed_range import (
    MAX_SOURCE_BYTES,
    FileChange,
    RangeBase,
    RangeRefusal,
    changed_files,
    graph_index_age,
    grep_mentions,
    read_new,
    read_old,
    resolve_base,
)
from codeintel.graph_answer import AnswerRendering
from codeintel.graph_backend import BackendRefused
from codeintel.graph_confidence import (
    _NAME_MATCHED,
    _QUALIFIER_BYPASSES,
    _SAME_MODULE_FOREIGN,
    _evidence_class,
)
from codeintel.provider import log_swallowed
from codeintel.source_kind import is_code_path
from codeintel.symbol_diff import (
    ADDED,
    BODY,
    REMOVED,
    SEVERITY,
    SIGNATURE,
    STATUS_OK,
    STATUS_UNPARSABLE,
    STATUS_UNSUPPORTED,
    FileSymbolDiff,
    SymbolChange,
    classify_mentions,
    diff_file,
    language_of,
)

# Evidence classes, the three words `provider.py` stamps on every envelope. Spelled out here because
# a GROUP has one of its own, finer than the envelope's: the envelope's ceiling for `changed` is
# `discovery`, and this says which parts of the answer rise above it.
_EVIDENCE = "evidence"
_DISCOVERY = "discovery"
_ADVISORY = "advisory"


@dataclass
class _Entry:
    """One changed symbol and the file it lives in (the NEW path, or the old one if the file went)."""

    path: str
    old_path: str | None
    change: SymbolChange

    @property
    def qn(self) -> str:
        return self.change.qualified_name

    @property
    def bare(self) -> str:
        return self.change.qualified_name.rsplit(".", 1)[-1]


@dataclass
class _Lookup:
    """What was found out about ONE changed symbol's users, and how far to trust it."""

    entry: _Entry
    source: str                                   # "graph" | "text"
    # ok | no-edges | not-indexed | unavailable | inconclusive   (graph)
    # ok | none                                                  (text)
    state: str = "ok"
    detail: str = ""
    untouched: list[dict] = field(default_factory=list)
    also: list[dict] = field(default_factory=list)
    # Callers of a BASE method (`graph_dispatch.py`). Kept out of the two lists above: whether the
    # diff touched such a caller says nothing about whether its author considered THIS symbol.
    through: list[dict] = field(default_factory=list)
    mentions: list[dict] = field(default_factory=list)
    quiet: int = 0                                # mentions that are only comments or definitions
    withheld: int = 0                             # rows found and not printed
    complete: bool = True
    cap_hit: bool = False
    moved_to: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    mention_search_truncated: bool = False
    outside: list[str] = field(default_factory=list)  # non-source files that still name it (removed)

    @property
    def live(self) -> int:
        return len(self.mentions)


class ChangedSince(AnswerRendering):
    """The symbol-level, base-aware `changed`. Mixed in beneath `GraphOps`.

    Inherits `AnswerRendering` for the same reason `GraphOps` does: it reuses `_display`,
    `_looks_like_test`, `_add_gap` and the `_pending_*` accumulators rather than restating them."""

    if TYPE_CHECKING:
        _last_failure: Any
        _pending_null: tuple[str, str] | None
        # Annotations and not `def` stubs, which is how `graph_ops.py` declares its own seams: the
        # source-reading tests key every function by NAME across the provider's whole MRO, so a
        # stub called `_op_callers` here would shadow the real one in `graph_ops.py`.
        _clear_failure: Callable[[], None]
        _refusal_fix: Callable[[BackendRefused], str]
        _op_callers: Callable[[str, str, int], str | None]
        _node_locations: Callable[[str, str, int], list[str]]

    # How many changed symbols get a users-lookup. Severity-ordered, so what the cap drops is the
    # least consequential tail, and what it dropped is named.
    _RANGE_SYMBOL_CAP = 40
    _RANGE_FILE_CAP = 300
    # Rows printed per sub-list. The rest are COUNTED and withheld, which the envelope reports.
    _RANGE_ROWS_SHOWN = 12
    # Wall-clock allowance for all the graph lookups together. Each is a backend round trip, and
    # without a warm daemon each costs seconds, so forty of them are not a thing to wait on blind.
    _RANGE_LOOKUP_BUDGET_S = 120.0
    _RANGE_LOOKUP_WORKERS = 4

    # ------------------------------------------------------------------------------- entry

    def _refuse(self, reason: str, hint: str) -> str | None:
        """No answer, and WHY — as the envelope's `reason`/`hint` rather than a bare `None`, which
        `build_result` would report as `not-in-graph`: a claim about the index that this op never
        made and that sends the reader to re-index a repository that was fine. Always returns
        `None`, typed as the op's own return so a caller can `return self._refuse(...)`."""
        self._pending_null = (reason, hint)
        return None

    def _answer_changed_since(
        self, ref: str, project: str, timeout_ms: int, root: str
    ) -> str | None:
        """What `ref`'s branch changed, at symbol level, and who still uses it. Never raises."""
        try:
            return self._changed_since(ref, project, timeout_ms, root)
        except Exception as exc:
            log_swallowed("ChangedSince._answer_changed_since", exc)
            return self._refuse(
                "error",
                f"comparing against `{ref}` failed unexpectedly (set CODEINTEL_DEBUG=1 for the "
                "traceback). This is not a statement about your code.")

    def _changed_since(self, ref: str, project: str, timeout_ms: int, root: str) -> str | None:
        if not root:
            return self._refuse(
                "no-project-root",
                "comparing against a base ref needs `project_root`: it is the repository git is "
                "asked about.")
        base = resolve_base(root, ref)
        if isinstance(base, RangeRefusal):
            return self._refuse(base.reason, base.hint)
        found = changed_files(root, base.base_sha)
        if isinstance(found, RangeRefusal):
            return self._refuse(found.reason, found.hint)

        source = [c for c in found.files
                  if is_code_path(c.path) or (c.old_path and is_code_path(c.old_path))]
        in_source = {id(c) for c in source}
        other = [c for c in found.files if id(c) not in in_source]
        files_cut = max(0, len(source) - self._RANGE_FILE_CAP)
        source = source[: self._RANGE_FILE_CAP]

        diffs = [(c, _diff_change(root, base.base_sha, c)) for c in source]
        entries = [_Entry(c.path, c.old_path, ch) for c, d in diffs for ch in d.changes]
        by_kind = {k: sum(1 for e in entries if e.change.change == k)
                   for k in (REMOVED, SIGNATURE, BODY, ADDED)}
        coarse = [(c, d) for c, d in diffs if d.status != STATUS_OK]
        unnamed = sum(d.unnamed for _, d in diffs)

        gaps: list[tuple[str, str]] = []               # (kind, detail)
        if files_cut:
            gaps.append((
                "files-truncated",
                f"{files_cut} further source file(s) changed and were not compared — the diff "
                f"is capped at {self._RANGE_FILE_CAP} files"))
        if found.untracked_error:
            gaps.append((
                "untracked-files-unknown",
                f"git could not list the untracked files ({found.untracked_error}), so a file that "
                "is new and not yet added is missing from this comparison — what it defines, and "
                "what it calls, was not looked at"))
        if other:
            gaps.append((
                "non-source-changes-not-compared",
                f"{len(other)} changed file(s) are not source this op reads — configuration, "
                "documentation, a generated file, or a language it has no reading of (Ruby, PHP, C#, "
                f"Kotlin, Swift …) — and were not compared: {_named([c.path for c in other], 5)}. "
                "A change there can break a caller with no definition-level change showing "
                "anywhere below"))
        module_level = _only_outside_definitions(diffs)
        if module_level:
            gaps.append((
                "module-level-not-compared",
                f"{len(module_level)} changed file(s) show no definition-level change: whatever "
                "changed in them is outside any definition (module-level statements, imports, "
                "constants) or is only the file's name, and this op compares definitions, not a "
                f"file's top-level statements: {_named(module_level, 5)}"))
        renames = [(c.old_path, c.path) for c, _ in diffs if c.status == "R" and c.old_path]
        if renames:
            moved = ", ".join(f"`{o}` → `{n}`" for o, n in renames[:5])
            gaps.append((
                "renamed-module-importers-unchecked",
                f"{len(renames)} file(s) were renamed or moved ({moved}"
                + (", …" if len(renames) > 5 else "")
                + "). Importers of the old module path were not checked: an import of it breaks "
                "with no definition changing, so a clean caller list below says nothing about them"))
        unsupported = [(c, d) for c, d in coarse if d.status == STATUS_UNSUPPORTED]
        unparsable = [(c, d) for c, d in coarse if d.status == STATUS_UNPARSABLE]
        if unsupported:
            names = ", ".join(f"`{c.path}`" for c, _ in unsupported[:5])
            gaps.append((
                "symbol-diff-unsupported-language",
                f"{len(unsupported)} changed file(s) are in a language with no definition-level "
                f"reading, so they are reported file-granular only: {names}"
                + (", …" if len(unsupported) > 5 else "")))
        if unparsable:
            names = ", ".join(f"`{c.path}` ({d.detail})" for c, d in unparsable[:3])
            gaps.append((
                "symbol-diff-unparsable",
                f"{len(unparsable)} changed file(s) could not be compared and were skipped — "
                f"which of their definitions changed is unknown, not none: {names}"
                + (", …" if len(unparsable) > 3 else "")))
        if unnamed:
            gaps.append((
                "symbol-diff-unnamed-skipped",
                f"{unnamed} definition(s) have no usable name (an anonymous export, say) and were "
                "not compared"))

        head = f"## Changes since `{base.ref}`"
        if not source:
            # Not a lookup miss, but not "nothing changed" either: git compared the two trees and
            # found no file of a language this op reads that differs. Anything else that changed is
            # said to have been NOT compared — an answer that read "no source file differs" over a
            # branch that only touched `app/user.rb` told a reader the branch was inert.
            self._publish_gaps(gaps)
            between = (f"the merge-base `{base.base_sha[:10]}` of `{base.ref}` and HEAD and the "
                       "working tree")
            if not other:
                return f"{head}\n(no file differs between {between})" + self._limits(gaps)
            return (f"{head}\n(compared: the source files of a language this op reads, between "
                    f"{between} — none of them differs. NOT compared: {len(other)} other changed "
                    f"file(s), {_named([c.path for c in other], 5)}. This is not a statement that "
                    "nothing changed)" + self._limits(gaps))

        # ------------------------------------------------------------ pick whom to look up
        touched: dict[str, set[str]] = {}
        for e in entries:
            if e.change.change != REMOVED:
                touched.setdefault(e.path, set()).add(e.qn)
        files_in_diff = {c.path for c, _ in diffs}
        added_at: dict[str, list[str]] = {}
        for e in entries:
            if e.change.change == ADDED:
                added_at.setdefault(e.qn, []).append(e.path)

        candidates = sorted(
            (e for e in entries if e.change.change != ADDED),
            key=lambda e: (SEVERITY[e.change.change], _is_class_body(e.change),
                           self._looks_like_test(e.path, e.bare),
                           e.path, e.change.new_line or e.change.old_line or 0))
        looked, skipped = candidates[: self._RANGE_SYMBOL_CAP], candidates[self._RANGE_SYMBOL_CAP:]
        if skipped:
            gaps.append((
                "symbols-truncated",
                f"{len(skipped)} of {len(candidates)} changed symbols were not looked up — the "
                f"lookup is capped at {self._RANGE_SYMBOL_CAP}, most severe first (removed, then "
                f"signature, then body), so what was dropped is the least consequential tail"))

        # --------------------------------------------------------------------- look them up
        deadline = time.monotonic() + self._RANGE_LOOKUP_BUDGET_S
        # Read BEFORE the lookups, not after them. The age is the db file's mtime, and the lookups
        # are backend processes that open that very file: if opening it (or a WAL checkpoint on
        # close) moves the mtime, a stamp taken afterwards is "just now" for every index there is,
        # and `stale-index` can never fire. Taken first it is what the index was when the question
        # was asked. Only whether to ASK is deferred: it is raised on a graph lookup that answered.
        age_paths = [c.path for c, _ in diffs if c.status != "D"]
        age = (graph_index_age(project, root, age_paths)
               if any(e.change.change != REMOVED for e in looked) else None)
        # The graph lookups are independent backend round trips, each costing seconds without a
        # warm daemon, so they run a few at a time. The state they accumulate is per-thread
        # (`PerThread`), which is the property that makes this safe; the results come back in order.
        # The root the answering project is registered under is a fact about THIS request, so it is
        # per-thread like the rest, and a worker does not inherit it. `callers` reads it to find the
        # files its qualifier scan opens; without it every worker would scan nothing, and the rows
        # here would say less than the same `callers` answer says.
        answered_root = self._answered_root
        # One allowance of reading time for every scan in this answer, not one per symbol: forty
        # symbols of two hundred files each would otherwise be forty times the bound `qualifier_scan`
        # promises. An allowance of time spent and not an instant on the clock, because the lookups
        # this answer mostly waits on take seconds each and an instant would pass before the later
        # symbols were scanned.
        scan_budget = self._scan_budget = qualifier_scan.Budget()
        with ThreadPoolExecutor(max_workers=self._RANGE_LOOKUP_WORKERS) as pool:
            def one(e: _Entry) -> _Lookup:
                self._answered_root = answered_root
                self._scan_budget = scan_budget
                if e.change.change == REMOVED:
                    lk = self._lookup_mentions(root, e)
                    lk.moved_to = [p for p in added_at.get(e.qn, []) if p != e.path]
                    return lk
                return self._lookup_callers(e, project, timeout_ms, touched, files_in_diff, deadline)

            lookups = list(pool.map(one, looked))
        gaps.extend(self._lookup_gaps(lookups))

        # Reported whenever a graph lookup was answered at all — including "never indexed", for which
        # a stale index is the likeliest explanation.
        if age is not None and any(lk.source == "graph" and lk.state != "unavailable"
                                   for lk in lookups):
            if age.state == "stale":
                gaps.append((
                    "stale-index",
                    f"{age.detail}, so the callers below describe an older tree than the one the "
                    "symbols were diffed in — a caller added since then is missing from them"))
            elif age.state == "unknown":
                gaps.append((
                    "index-age-unknown",
                    f"{age.detail}, so these callers cannot be called current"))

        # -------------------------------------------------------------------------- render
        sections = self._sections(lookups)
        body = self._render_since(
            base, head, by_kind, len(source), other, diffs, coarse, entries, sections, skipped)
        # Published in the order PRINTED, so `rows[]` is the `- ` lines of the body line for line.
        printed = [lk for _, group, _ in sections for lk in group]
        self._pending_rows += tuple(r for lk in printed for r in self._printed_rows(lk))
        self._pending_withheld += sum(lk.withheld for lk in printed)
        self._pending_row_cap = self._pending_row_cap or any(lk.cap_hit for lk in printed)
        self._publish_gaps(gaps)
        return body + self._limits(gaps)

    # ------------------------------------------------------------------------ lookups

    def _lookup_callers(
        self, entry: _Entry, project: str, timeout_ms: int,
        touched: dict[str, set[str]], files: set[str], deadline: float,
    ) -> _Lookup:
        """Callers of a symbol that still exists, by asking `_op_callers` and keeping what it
        PUBLISHED. Its structured rows are the row-production and the evidence classification this
        op is meant to reuse; reading them back is the seam that does not fork either."""
        lk = _Lookup(entry, "graph")
        if time.monotonic() > deadline:
            lk.state, lk.complete = "unavailable", False
            lk.detail = ("the time allowed for caller lookups ran out before this symbol was "
                         "reached")
            return lk
        # `name@file` is codeintel's own disambiguator. A path containing `@` would be split at the
        # wrong place, so those fall back to the qualified name alone and the ambiguity, if any, is
        # reported by the ordinary machinery.
        target = entry.qn if "@" in entry.path else f"{entry.qn}@{entry.path}"
        saved = (self._pending_gaps, self._pending_rows, self._pending_row_cap,
                 self._pending_withheld, self._pending_nonrow_lines)
        self._pending_gaps, self._pending_rows = (), ()
        self._pending_row_cap, self._pending_withheld, self._pending_nonrow_lines = False, 0, False
        located: list[str] = []
        omitted = 0
        try:
            self._clear_failure()
            text = self._op_callers(target, project, timeout_ms)
            rows, lk.gaps, lk.cap_hit, omitted = self._harvest()
            miss = self._last_failure
            if not rows and text is None and miss is None:
                # "No edges" and "never indexed" license opposite conclusions — the same two cases
                # `build_result` separates for a bare `callers` — so ask which one this is.
                located = self._node_locations(target, project, timeout_ms)
                miss = self._last_failure
        finally:
            self._clear_failure()
            (self._pending_gaps, self._pending_rows, self._pending_row_cap,
             self._pending_withheld, self._pending_nonrow_lines) = saved

        if rows:
            lk.state = "ok"
            for r in rows:
                row = dict(r, module_scope_in_diff=bool(
                    r.get("module_scope") and str(r.get("file") or "") in files))
                if r.get("via"):
                    lk.through.append(row)
                else:
                    (lk.also if self._caller_is_changed(r, touched) else lk.untouched).append(row)
            shown = self._RANGE_ROWS_SHOWN
            # Two different ways a known row goes unprinted, and both are WITHHELD: the rows this
            # group does not print (its own per-list limit), and the ones `callers` itself kept back
            # (its distinct-caller cap) — which it counts and does not publish. Counting only the
            # first made "120 callers, 50 kept" read as "50 in total" in the envelope.
            lk.withheld = (max(0, len(lk.untouched) - shown) + max(0, len(lk.also) - shown)
                           + sum(max(0, len(rs) - shown) for rs in _by_base(lk.through).values())
                           + omitted)
            lk.complete = not lk.cap_hit and not lk.gaps
            if miss is not None:
                # Rows AND a failed backend call: some of the lookup came back and some did not.
                # Left unsaid, the only trace is whatever `callers` happened to raise about its row
                # cap, which names the symptom and not the cause.
                lk.gaps.append({"section": "callers", "kind": "callers-incomplete",
                                "detail": self._miss_detail(miss)})
                lk.complete = False
        elif miss is not None:
            lk.state, lk.complete, lk.detail = "unavailable", False, self._miss_detail(miss)
        elif text is None:
            lk.state = "no-edges" if located else "not-indexed"
            lk.complete = bool(located)
        else:
            lk.state, lk.complete = "inconclusive", False
        return lk

    def _harvest(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int]:
        """What the lookup just published: its rows, its gaps, whether it hit the backend's row cap,
        and how many rows it kept back that are in none of the first three."""
        return ([dict(r) for r in self._pending_rows], list(self._pending_gaps),
                bool(self._pending_row_cap), int(self._pending_withheld))

    def _miss_detail(self, miss: Any) -> str:
        """A failed backend call, as one sentence that also says what to do about it.

        A refusal has a fix and the reader of this envelope is usually an agent with no shell: the
        `callers` path already puts the message and the remedy in its hint, and a gap that carried
        only the message would leave `changed <ref>` the one op that says what went wrong and not
        how to get past it."""
        if isinstance(miss, BackendRefused):
            return f"{miss.describe()} — to fix: {self._refusal_fix(miss)}"
        return str(miss.describe())

    @staticmethod
    def _caller_is_changed(row: dict, touched: dict[str, set[str]]) -> bool:
        """Whether a caller is itself a definition this diff added, rewrote or re-signed.

        A MODULE-SCOPE caller is never counted: the diff is compared definition by definition, so
        whether a file's top-level statements changed is not something it knows. It is left on the
        untouched side — over-reporting a risk costs a reader a line, and under-reporting it is the
        failure this op exists to prevent."""
        if row.get("module_scope"):
            return False
        file = str(row.get("file") or "")
        keys = touched.get(file)
        if not keys:
            return False
        qn = str(row.get("qualified_name") or "")
        return any(_is_symbol(qn, file, k) for k in keys)

    def _lookup_mentions(self, root: str, entry: _Entry) -> _Lookup:
        """Who still mentions a REMOVED symbol, from the working tree's text.

        The graph cannot say: it was built from a tree that had the function and has been, at best,
        partly refreshed since. So this is `git grep -w`, and everything it returns is a NAME match
        — a same-named symbol elsewhere is a hit, and so is a comment. Python files are read with
        the parser so each hit can say whether it is code, a string or a comment; the rest cannot be
        told apart and say nothing."""
        lk = _Lookup(entry, "text")
        search = grep_mentions(root, entry.bare)
        if search.error:
            lk.state, lk.complete, lk.detail = "unavailable", False, search.error
            return lk
        lk.mention_search_truncated = search.truncated
        # A search that was cut off knows only a LOWER BOUND, so the group's total is unknown, which
        # is what `cap_hit` means to the envelope: `evidence.total` is `None` and not the number of
        # rows that happened to fit. `truncated` alone left it reading "200 in total".
        lk.cap_hit = search.truncated
        lk.complete = not search.truncated
        lk.outside = list(search.outside)
        labels: dict[tuple[str, int], str] = {}
        by_file: dict[str, list[int]] = {}
        for m in search.hits:
            by_file.setdefault(m.path, []).append(m.line)
        for path, lines in by_file.items():
            if language_of(path) == "python":
                text = read_new(root, path)
                if text is not None:
                    for line, label in classify_mentions(text, entry.bare, lines).items():
                        labels[(path, line)] = label
        order = {"code": 0, "": 0, "string": 1}
        rows = []
        for m in search.hits:
            label = labels.get((m.path, m.line), "")
            if label in ("comment", "definition"):
                lk.quiet += 1
                continue
            rows.append(self._mention_row(entry, m.path, m.line, m.text, label))
        rows.sort(key=lambda r: (order.get(r["label"], 2), r["file"], r["line"]))
        lk.mentions = rows
        lk.withheld = max(0, len(rows) - self._RANGE_ROWS_SHOWN)
        lk.state = "ok" if rows else "none"
        return lk

    @staticmethod
    def _mention_row(entry: _Entry, path: str, line: int, text: str, label: str) -> dict[str, Any]:
        """A text mention, shaped like a graph row so `rows[]` stays one list — and unable to claim
        more than it is: it is never `verified`, its strategy says `git-grep`, and its `why` says
        what a reader must not take from it."""
        return {
            "relation": "mention",
            "name": entry.bare,
            "qualified_name": "",
            "file": path,
            "line": line,
            "module_scope": False,
            "edge": None,
            "verified": False,
            # Nobody scanned a text mention for a qualifier: it is a hit for a bare name in a tree
            # the graph never saw, so there is no qualifier it was narrowed by. `null`, as on every
            # row the scan did not judge.
            "qualifier": None,
            "qualifier_seen": None,
            "evidence": _NAME_MATCHED,
            "strategy": "git-grep",
            "confidence": None,
            "why": "a text match for the bare name found by `git grep -w`; not a resolved call",
            "label": label,
            "text": text.strip()[:160],
            "changed_symbol": entry.qn,
            "change": REMOVED,
            "group_class": _DISCOVERY,
        }

    # ------------------------------------------------------------------------- gaps

    def _lookup_gaps(self, lookups: list[_Lookup]) -> list[tuple[str, str]]:
        """Every way a lookup fell short, one named gap per KIND with the symbols it affected."""
        by_kind: dict[str, list[tuple[str, str]]] = {}

        def note(kind: str, lk: _Lookup, detail: str = "") -> None:
            by_kind.setdefault(kind, []).append((lk.entry.qn, detail))

        for lk in lookups:
            if lk.source == "text":
                if lk.state == "unavailable":
                    note("callers-unavailable", lk, lk.detail)
                if lk.mention_search_truncated:
                    note("mentions-truncated", lk)
                if lk.outside:
                    note("mentions-outside-source", lk, _named(lk.outside, 5))
                if lk.state in ("ok", "none"):
                    # Raised whether or not anything was found. A group of text matches is
                    # `discovery`, never `evidence`, and "nothing mentions it" is no better: a name
                    # built at runtime, a `getattr`, a string key, a caller outside the repository.
                    note("text-mention-only", lk)
                continue
            if lk.state == "unavailable":
                note("callers-unavailable", lk, lk.detail)
            elif lk.state == "not-indexed":
                note("symbol-not-indexed", lk)
            elif lk.state == "no-edges":
                note("no-graph-callers", lk)
            elif lk.state == "inconclusive":
                note("callers-inconclusive", lk)
            for g in lk.gaps:
                note(str(g.get("kind") or "caller-gap"), lk, str(g.get("detail") or ""))
        prose = {
            "callers-unavailable":
                "{names}: the lookup did not complete, so their callers are UNKNOWN, not none",
            "symbol-not-indexed":
                "{names}: not in the graph index (it predates them, or never saw them), so their "
                "callers are UNKNOWN, not none",
            "no-graph-callers":
                "{names}: the graph records no callers. That is not proof there are none: "
                "framework dispatch, a call through a value, and a stale index all look like this",
            "callers-inconclusive":
                "{names}: the graph returned no rows for exactly this symbol (see the lookup's "
                "own gap) — unknown, not none",
            "text-mention-only":
                "{names}: removed, so any surviving users could only be searched for as TEXT. A "
                "name mention is not a resolved call, and finding none is not proof there are "
                "none (a name built at runtime, a `getattr`, a caller outside this repository)",
            "mentions-truncated":
                "{names}: the text search hit its cap, so the mention list is a lower bound",
            "mentions-outside-source":
                "{names}: still written in files this op does not read as code. A removed function "
                "named in a manifest's entry points or in a YAML or JSON config fails at runtime "
                "all the same, and finding it there is the only way it would have been found",
            "callers-incomplete":
                "{names}: a backend call failed while their callers were being looked up, so the "
                "rows listed may be missing some — a lower bound, not the whole list",
        }
        out = []
        for kind, items in by_kind.items():
            # One gap per KIND, but each symbol keeps ITS OWN detail: a merged gap that printed the
            # first symbol's numbers after every name ("12 of 40 rows ..." for all of them) states
            # a figure about a symbol it was never measured for.
            per_symbol: dict[str, list[str]] = {}
            for qn, d in items:
                found = per_symbol.setdefault(qn, [])
                if d and d not in found:
                    found.append(d)
            shown = list(per_symbol)[:6]
            names = ", ".join(f"`{qn}`" for qn in shown)
            if len(per_symbol) > 6:
                names += f", … ({len(per_symbol)} in all)"
            each = [f"`{qn}`: {'; '.join(per_symbol[qn])}" for qn in shown if per_symbol[qn]]
            if kind in prose:
                detail = prose[kind].format(names=names)
                if each:
                    detail += f" ({'; '.join(each)})"
            elif each:
                detail = "for " + "; ".join(
                    f"`{qn}`: {'; '.join(per_symbol[qn])}" if per_symbol[qn] else f"`{qn}`"
                    for qn in shown)
            else:
                detail = f"raised while looking up {names}"
            out.append((kind, detail))
        return out

    def _publish_gaps(self, gaps: list[tuple[str, str]]) -> None:
        for kind, detail in gaps:
            self._add_gap("changed", kind, detail)

    @staticmethod
    def _limits(gaps: list[tuple[str, str]]) -> str:
        """The gaps in the body. `gaps` is the field an integration branches on and the body is the
        field an agent reads, so everything raised is also said here — quoted, so none of it can be
        read back as a result row."""
        if not gaps:
            return ""
        return "\n\n### Limits of this answer\n" + "\n".join(
            f"> **{kind}** — {detail}." for kind, detail in gaps)

    # ----------------------------------------------------------------------- rendering

    def _printed_rows(self, lk: _Lookup) -> list[dict[str, Any]]:
        """The rows this lookup's group PRINTS, in print order, each stamped with its group — the
        list `rows[]` must equal, line for line."""
        shown = self._RANGE_ROWS_SHOWN
        klass = self._group_class(lk)
        e = lk.entry
        if lk.source == "text":
            return [dict(r, group_class=klass) for r in lk.mentions[:shown]]
        out: list[dict[str, Any]] = []
        lists = [("untouched", lk.untouched), ("also-changed", lk.also),
                 *(("through-base", rs) for rs in _by_base(lk.through).values())]
        for status, rows in lists:
            out.extend(dict(r, changed_symbol=e.qn, change=e.change.change,
                            caller_status=status, group_class=klass) for r in rows[:shown])
        return out

    @staticmethod
    def _group_class(lk: _Lookup) -> str:
        """What THIS group's answer can be used for, by the envelope's three words.

        `evidence` only when every caller row followed a real binding, the lookup was whole, and
        nothing was withheld — the same bar `safe_for_destructive` sets for the whole answer. A
        group of text mentions is `discovery`: matched candidates, a place to look. Anything else a
        graph lookup produced is `advisory`."""
        if lk.source == "text":
            return _DISCOVERY
        rows = lk.untouched + lk.also + lk.through
        if (lk.state == "ok" and lk.complete and rows and not lk.withheld
                and all(r.get("verified") is True for r in rows)):
            return _EVIDENCE
        return _ADVISORY

    def _caller_line(self, row: dict) -> str:
        """A graph row as the same line `callers` prints for it — rebuilt from the structured row
        so `_display` stays the one place that knows the badge conventions."""
        raw: dict[str, Any] = {
            "a.name": row.get("name"), "a.qualified_name": row.get("qualified_name"),
            "a.file_path": row.get("file"), "type(c)": row.get("edge") or "",
        }
        if row.get("module_scope"):
            raw["_module_scope"] = row.get("file")
        if row.get("evidence") == _NAME_MATCHED:
            raw["_low_confidence"] = row.get("confidence")
            if row.get("via"):
                # A caller of a BASE method (see `graph_dispatch.py`): the badge says which kind of
                # base, and its strategy — whatever it is — is the account of how the call reached
                # the base, so the `same_module` reading below must not be applied to it.
                raw["_via"], raw["_via_kind"] = row.get("via"), row.get("via_kind")
            # A `same_module` edge is only ever name-matched because the call-site check found it
            # written through a receiver (see `_same_module_call`), so the strategy IS the verdict.
            # Without it the badge reads `[?0.90]` — the backend's high score behind a question
            # mark, which is the reading `callers` prints "qualified call" to prevent.
            elif _evidence_class(str(row.get("strategy") or "")) == "same-module":
                raw["_same_module"] = _SAME_MODULE_FOREIGN
            # What the qualifier scan found, carried through as `callers` published it. The label on
            # the line is the same one, from the same function, as in `callers`; `changed` has no
            # `Checked:` note, so the group that prints a label prints the caveat beneath its rows
            # (`_qualifier_caveat`).
            raw["_qualifier"], raw["_qualifier_seen"] = row.get("qualifier"), row.get("qualifier_seen")
        line = self._display(raw, "a.name", "a.qualified_name", "a.file_path")
        if row.get("module_scope_in_diff"):
            line += "  (module-level code in a file this diff touches — not compared)"
        return line

    @staticmethod
    def _where(e: _Entry) -> str:
        ch = e.change
        was = e.old_path or e.path
        if ch.old_line and ch.new_line:
            moved = f"{was}:{ch.old_line} → " + (
                f"{ch.new_line}" if was == e.path else f"{e.path}:{ch.new_line}")
            return moved if (was != e.path or ch.old_line != ch.new_line) else f"{e.path}:{ch.new_line}"
        if ch.new_line:
            return f"{e.path}:{ch.new_line}"
        return f"{was}:{ch.old_line}" if ch.old_line else was

    def _group_heading(self, lk: _Lookup) -> str:
        e, ch = lk.entry, lk.entry.change
        what = ch.change + (f": {', '.join(ch.facets)}" if ch.facets else "")
        return f"#### `{e.qn}` — {what} · {self._where(e)} · [{self._group_class(lk)}]"

    def _render_graph_group(self, lk: _Lookup) -> list[str]:
        out = [self._group_heading(lk)]
        shown = self._RANGE_ROWS_SHOWN
        if lk.state == "ok":
            lists = [
                (f"**Callers this diff did NOT touch — these may break ({len(lk.untouched)}):**",
                 lk.untouched),
                (f"**Callers also changed in this diff — probably updated together ({len(lk.also)}):**",
                 lk.also),
                # Apart from the two above, and one list per base: a caller of the BASE is not a
                # caller of this symbol, and whether the diff touched it does not say that its
                # author considered this override. It may be the only production caller there is.
                *((f"**Callers through `{_short_base(via)}` — they call the "
                   f"{'base class' if rs[0].get('via_kind') == 'base' else 'Protocol'}, not this symbol, "
                   "and reach it only by dispatch; never counted as verified callers "
                   f"({len(rs)}):**", rs)
                  for via, rs in _by_base(lk.through).items()),
            ]
            for title, rows in lists:
                if not rows:
                    continue
                out.append(title)
                out.extend(self._caller_line(r) for r in rows[:shown])
                if len(rows) > shown:
                    out.append(f"… (+{len(rows) - shown} more, not shown)")
            refuted = sorted({str(r.get("qualifier") or "") for _, rows in lists for r in rows[:shown]
                              if r.get("qualifier_seen") is False} - {""})
            if refuted:
                out.append(self._qualifier_caveat(refuted))
            return out
        out.append({
            "no-edges": "_The graph records no caller of this symbol. That is not proof there is "
                        "none — framework dispatch, a call through a value and a stale index all "
                        "look like this._",
            "not-indexed": "_This symbol is not in the graph index — the index predates it or "
                           "never saw it — so its callers are UNKNOWN, not none._",
            "unavailable": f"_The caller lookup did not complete ({lk.detail}) — its callers are "
                           "UNKNOWN, not none._",
            "inconclusive": "_The graph returned no rows for exactly this symbol (see the limits "
                            "below) — its callers are UNKNOWN, not none._",
        }.get(lk.state, "_No caller information._"))
        return out

    def _qualifier_caveat(self, tokens: list[str]) -> str:
        """The note a `[never writes …]` mark travels with, once per group that prints one.

        `callers` says this in the `Checked:` note above its rows. `changed` prints no such note, and a
        mark on a line with the qualification left off would say more than the scan found — so the
        group that carries a mark carries its caveat and the command that reproduces it."""
        root = getattr(self, "_answered_root", None) or "."
        names = ", ".join(f"`{t}`" for t in tokens)
        rerun = " ".join(f"`rg -n --fixed-strings '{t}' {root}`" for t in tokens)
        return (f"_Rows marked `[never writes …]` are in files that do not write {names}, the name the "
                f"symbol is qualified by. That is a text search of the files as they are on disk, and "
                f"it narrows; it does not decide. A file can reach the method through "
                f"{_QUALIFIER_BYPASSES}, so these are the callers to doubt first, not callers proven "
                f"false. Re-run it yourself: {rerun}_")

    def _render_removed_group(self, lk: _Lookup) -> list[str]:
        e = lk.entry
        out = [self._group_heading(lk)]
        shown = self._RANGE_ROWS_SHOWN
        if lk.state == "unavailable":
            out.append(f"_The text search did not complete ({lk.detail}) — whether anything still "
                       "mentions this name is UNKNOWN, not no._")
            return out
        if lk.mentions or not lk.outside:
            out.append(f"**Surviving mentions of the name `{e.bare}` ({lk.live}) — text matches, NOT "
                       "resolved calls:**")
        for r in lk.mentions[:shown]:
            tag = f"text mention, {r['label']}" if r["label"] else "text mention"
            out.append(f"- {r['file']}:{r['line']}  `{r['text']}`  [{tag}]")
        if len(lk.mentions) > shown:
            out.append(f"… (+{len(lk.mentions) - shown} more, not shown)")
        if lk.outside:
            out.append(f"_The name `{e.bare}` is also written in {len(lk.outside)} file(s) this op "
                       f"does not read as code: {_named(lk.outside, 5)} — an entry point or a config "
                       "that names a removed function fails at runtime, so check them._")
        if lk.quiet:
            out.append(f"_{lk.quiet} comment(s) or definition(s) that match the name were set "
                       "aside._")
        if lk.moved_to:
            out.append(f"_A symbol named `{e.qn}` was ADDED in {', '.join(f'`{p}`' for p in lk.moved_to[:3])}"
                       " — if this was a move, the callers that break are the ones importing the "
                       "old location._")
        return out

    def _sections(self, lookups: list[_Lookup]) -> list[tuple[str, list[_Lookup], str]]:
        """The printed groups, in the order a reader should meet them: what was removed and is still
        mentioned, then what changed signature, then what changed body. Within a section the
        symbols with the most callers the diff did NOT touch come first — those are the ones a
        reviewer has most to check. The third element says how a group renders; `quiet` groups
        print no rows."""
        removed = [lk for lk in lookups if lk.entry.change.change == REMOVED]
        # A name that survives only in a manifest or a config is still mentioned: filing it under
        # "nothing else mentions the name" would contradict the gap raised for it beside it.
        live = sorted((lk for lk in removed if lk.state in ("ok", "unavailable") or lk.outside),
                      key=lambda lk: (-lk.live, lk.entry.qn))
        live_ids = {id(lk) for lk in live}
        quiet = [lk for lk in removed if id(lk) not in live_ids]

        def graph(kind: str) -> list[_Lookup]:
            # A class's own statements changing (an attribute added) lists every constructor call
            # beneath it, and would bury the functions whose behaviour actually moved.
            return sorted((lk for lk in lookups if lk.entry.change.change == kind),
                          key=lambda lk: (_is_class_body(lk.entry.change), -len(lk.untouched),
                                          lk.entry.qn))

        return [
            ("Removed — and still mentioned", live, "removed"),
            ("Signature changed — callers may no longer fit", graph(SIGNATURE), "graph"),
            ("Body changed — same signature, different behaviour", graph(BODY), "graph"),
            ("Removed — nothing else mentions the name", quiet, "quiet"),
        ]

    def _render_since(
        self, base: RangeBase, head: str, by_kind: dict[str, int], n_files: int,
        other: list[FileChange], diffs: list[tuple[FileChange, FileSymbolDiff]],
        coarse: list[tuple[FileChange, FileSymbolDiff]],
        entries: list[_Entry], sections: list[tuple[str, list[_Lookup], str]],
        skipped: list[_Entry],
    ) -> str:
        parts = [
            f"{head} ({by_kind[REMOVED]} removed · {by_kind[SIGNATURE]} signature · "
            f"{by_kind[BODY]} body · {by_kind[ADDED]} added, across {n_files} source file(s))",
            f"_Compared the merge-base `{base.base_sha[:10]}` of `{base.ref}` and HEAD against the "
            f"WORKING TREE — committed and uncommitted changes together: what this branch would "
            f"change if it were committed now._",
        ]
        notes = []
        if other:
            notes.append(f"{len(other)} changed file(s) that are not source this op reads were NOT "
                         f"compared: {_named([c.path for c in other], 5)}")
        renames = [(c.old_path, c.path) for c, _ in diffs if c.status == "R" and c.old_path]
        if renames:
            notes.append("renamed (anything importing the old module path may break): "
                         + ", ".join(f"`{o}` → `{n}`" for o, n in renames[:5]))
        deleted = [c.path for c, _ in diffs if c.status == "D"]
        if deleted:
            notes.append("deleted: " + ", ".join(f"`{p}`" for p in deleted[:5]))
        if notes:
            parts.append("_" + "; ".join(notes) + "._")

        quiet_files = _only_outside_definitions(diffs)
        if quiet_files:
            parts.append(
                "_These changed outside any definition (module-level statements, imports or "
                "constants), which a definition-level diff does not compare: "
                + _named(quiet_files, 8) + "._")

        for title, group, how in sections:
            if not group:
                continue
            parts.append(f"### {title} ({len(group)})")
            for lk in group:
                if how == "graph":
                    parts.extend(self._render_graph_group(lk))
                elif how == "removed":
                    parts.extend(self._render_removed_group(lk))
                else:
                    extra = (f" (only {lk.quiet} comment(s) or definition(s) match the name)"
                             if lk.quiet else "")
                    parts.append(f"* `{lk.entry.qn}` · {self._where(lk.entry)}{extra}")
            if how == "removed":
                parts.append("_These are NAME mentions from `git grep -w`, not resolved calls: a "
                             "same-named symbol elsewhere matches too. Comments and definitions "
                             "are set aside where the file is Python (the parser can tell); in "
                             "other languages a hit may be a comment or a string._")
        added = [e for e in entries if e.change.change == ADDED]
        if added:
            parts.append(f"### Added ({len(added)})")
            parts.append(", ".join(f"`{e.qn}`" for e in added[:30])
                         + (f", … (+{len(added) - 30} more)" if len(added) > 30 else ""))
        if skipped:
            parts.append(f"### Not looked up ({len(skipped)})")
            parts.append(", ".join(f"`{e.qn}` ({e.change.change})" for e in skipped[:30])
                         + (f", … (+{len(skipped) - 30} more)" if len(skipped) > 30 else ""))
        if coarse:
            parts.append(f"### Compared at file granularity only ({len(coarse)})")
            for c, d in coarse:
                parts.append(f"* `{c.path}` — {d.status}: {d.detail}")
        return "\n".join(parts)


# ----------------------------------------------------------------------------- module helpers

def _by_base(rows: list[dict]) -> dict[str, list[dict]]:
    """Rows that reached a symbol through a base, grouped by the base method they called, in the
    order the bases first appear."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r.get("via") or ""), []).append(r)
    return out


def _short_base(via: str) -> str:
    """`Base.m` from the qualified name of a base method."""
    return ".".join(via.split(".")[-2:])


def _diff_change(root: str, base_sha: str, change: FileChange) -> FileSymbolDiff:
    """One file's symbol diff. A side that SHOULD exist and cannot be read is `unparsable`, not
    absent: passing `None` for it would classify every definition in the file as added or removed."""
    old = None if change.status == "A" else read_old(root, base_sha, change.old_path or change.path)
    new = None if change.status == "D" else read_new(root, change.path)
    if (change.status != "A" and old is None) or (change.status != "D" and new is None):
        return FileSymbolDiff(
            change.path, STATUS_UNPARSABLE, language_of(change.path),
            detail=("could not be read (missing, larger than "
                    f"{MAX_SOURCE_BYTES / 1_000_000:g} MB, or outside the repository)"))
    return diff_file(change.path, old, new)


def _named(paths: list[str], limit: int) -> str:
    """Up to *limit* paths, quoted, with what was left out counted rather than dropped."""
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f", … (+{len(paths) - limit} more)" if len(paths) > limit else "")


def _only_outside_definitions(diffs: list[tuple[FileChange, FileSymbolDiff]]) -> list[str]:
    """Changed files whose diff reported no definition at all: whatever changed in them is outside
    any definition (or is only the file's name), which a definition-level comparison cannot see."""
    return [c.path for c, d in diffs if d.status == STATUS_OK and not d.changes]


def _is_class_body(change: SymbolChange) -> bool:
    """A class whose own statements (not its header, not its methods) are what changed."""
    return change.kind == "class" and change.change == BODY


def _is_symbol(row_qn: str, row_file: str, key: str) -> bool:
    """Whether a graph row's qualified name is the definition `key` (`Class.method`) in `row_file`.

    The graph writes `<module path>.<Class>.<method>`, so a bare suffix test would take
    `Foo.run` for the top-level `run` of the same file. The part before the key has to be the
    file's own module path — or a suffix/extension of it, because the project prefix is stripped by
    heuristic and may or may not have been."""
    if row_qn == key:
        return True
    suffix = "." + key
    if not row_qn.endswith(suffix):
        return False
    module = row_qn[: -len(suffix)]
    stem = os.path.splitext(row_file.replace("\\", "/"))[0]
    dotted = {stem.replace("/", ".")}
    if stem.endswith("/__init__"):
        dotted.add(stem[: -len("/__init__")].replace("/", "."))
    return any(d == module or d.endswith("." + module) or module.endswith("." + d)
               for d in dotted)
