"""Turning graph rows into an answer a reader can act on — and into the caveats beside it.

Phase 4 of `docs/refactor-graph-provider.md`, arrived at from the other direction. That phase
proposed an `EdgeAnswerRenderer` returning `(text, gaps)` instead of mutating `_pending_gaps`; this
is the same seam with the contract left alone, because the brief this split was done under required
behaviour to be provably unchanged and `(text, gaps)` is a change to what a renderer promises. The
redesign stays open; the separation does not have to wait for it.

A MIXIN rather than a collaborator, for one reason worth stating: every method here already reads
as `self.<something>`, and `GraphProvider._is_noise` / `gp._display(...)` are called from
`mapper.py`, `grapher.py` and six test modules by those exact names. Inheritance keeps every one of
those call sites resolving to the same function object, so the move is provable by reading rather
than by hoping the delegators are complete. `_add_gap` is the ONE thing this needs from the class
it is mixed into, which is what made the seam worth cutting here.

What lives here is the part that has to stay honest: a heading counts the rows beneath it, a note
says how those rows were resolved, a collision is disclosed rather than silently dropped, and an
answer emptied by our own filter never reads as an answer about the code.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codeintel.graph_confidence import (
    _EDGE_CONFIDENCE_FLOOR,
    _EDGE_CONFIDENCE_WEAK,
    _NAME_MATCHED,
    _RESOLVED,
    _UNSTATED,
    _confidence_badge,
    _edge_confidence,
    _evidence_class,
)
from codeintel.graph_edges import (
    _CANDIDATE_CAP,
    _DIRECT_KIND,
    _EDGE_KINDS,
    _EDGE_ROW_LIMIT,
    _EdgeGroup,
)
from codeintel.graph_render import (
    _collapse_repeats,
    _is_archived_path,
    _is_module_scope_node,
    _is_non_code,
    _lang_family,
    _strip_project_prefix,
)
from codeintel.graph_targets import _SymbolTarget


class AnswerRendering:
    """Row rendering, ambiguity disclosure and the notes that qualify a count.

    Mixed into `GraphProvider`. Requires `self._add_gap` from the class it joins — every gap raised
    here is also stated in the body text, because `gaps` is the field an integration branches on
    and the body is the field an agent reads.
    """

    if TYPE_CHECKING:
        # The whole contract this mixin has with its host, declared rather than assumed. A type
        # checker verifies it at the join, so a future host that does not record gaps fails here
        # instead of silently dropping every caveat these methods raise.
        _answered_root: str | None
        _pending_gaps: tuple[dict[str, Any], ...]
        _pending_rows: tuple[dict[str, Any], ...]
        _pending_row_cap: bool
        _pending_withheld: int
        _pending_nonrow_lines: bool

        def _add_gap(self, section: str, kind: str, detail: str) -> None: ...

    @staticmethod
    def _display(row: dict, name_key: str, qn_key: str, file_key: str) -> str:
        # A module-scope container row (marked by `_collapse_module_scope`) renders as the LOCATION
        # it is, never as the synthetic `__file__`/module symbol the backend attached the edge to.
        # The edge is real — code at that file's module scope references the symbol — so it stays,
        # but calling it `src.click.core.__file__` asserts a caller/callee that does not exist. No
        # edge badge: after the File/Module double-representation is collapsed the CALLS-vs-USAGE
        # distinction is a backend artifact of which node kind carried the edge, not a fact about the
        # code.
        scope = row.get("_module_scope")
        if scope is not None:
            where = str(scope) or str(row.get(file_key) or "")
            # The name-match badge has to be reachable from this branch too. On the repository that
            # motivated the floor, 31 of the 32 fabricated rows were module-scope ones, so a badge
            # applied only to the named-symbol branch below would have marked exactly one of them
            # and left the summary count looking unsupported by the list it summarises.
            # The suppressed badge above is about CALLS-vs-USAGE only — at module scope that
            # distinction reflects which node kind carried the edge, not a fact about the code, so
            # printing it would assert something the backend did not mean. `CALL_REFERENCE` is a
            # different claim: it says the symbol was PASSED rather than invoked, which is true of
            # the code regardless of which node holds the edge. It is shown, so a header reading
            # "N other reference(s)" is always supported by the rows beneath it.
            kind = str(row.get("type(c)") or "").strip()
            kind_badge = f" [{kind}]" if kind and kind not in ("CALLS", "USAGE") else ""
            mark = kind_badge + _confidence_badge(row)
            return f"- module scope of {where}{mark}" if where else f"- module scope{mark}"
        name = str(row.get(name_key) or "?")
        qn = _strip_project_prefix(str(row.get(qn_key) or ""), may_be_filename=False)
        file = str(row.get(file_key) or "")
        edge = str(row.get("type(c)") or "").strip()
        label = qn or name
        tail = f" ({file})" if file and file != qn else ""
        badge = f" [{edge}]" if edge else ""
        # A row the backend resolved by bare name rather than by import carries its score into the
        # line itself. The summary note says how many there are; only the badge says WHICH, and a
        # reader scanning for "is my symbol in here" reads rows, not notes.
        badge += _confidence_badge(row)
        return f"- {label}{badge}{tail}"

    @staticmethod
    def _collapse_module_scope(
        groups: list[_EdgeGroup], label_key: str, file_key: str
    ) -> None:
        """Relabel the DISPLAYED-side module-scope rows in each group, in place.

        Both edge ops render one end of the edge as their rows — the caller for `callers`, the callee
        for `callees` — so the pseudo-node filter lives here, off the displayed side's label/file
        keys, and both ops reach it the same way. A row whose displayed node is a whole-file container
        (`_is_module_scope_node`) is not a symbol; the backend has simply no node for module- or
        class-body-scope code and hangs the edge off the file. Three choices were weighed on what the
        backend actually emits:

        * DROP the row. Rejected: it under-counts, and worse it manufactures false absences. The edge
          is real — `src.click.core.__file__` references `builtins.len` and an exception class from
          another file, which is module-scope code, not a containment artifact — and on the pinned
          corpus 147 symbols are referenced ONLY from module scope, so dropping would report a live,
          referenced function as having zero callers: the "safe to delete" misread this project keeps
          re-learning to avoid.
        * RELABEL it as the location it is. Chosen: the edge stays, the count stays honest, and the
          row stops asserting a caller/callee that does not exist.
        * Something else — leave it. Rejected: the row reads as a real symbol a reader will try to
          open.

        A single file can carry BOTH a `File` (`__file__`) and a `Module` representation of the same
        scope (38 target/file pairs do on the corpus), so collapse them to one row per file — two
        rows both reading "module scope of core.py" is the double-count relabelling would otherwise
        introduce. Real callable rows are never touched, so a symbol called from both a function and
        module scope keeps both.
        """
        for group in groups:
            seen_files: set[str] = set()
            kept: list[dict] = []
            for row in group.rows:
                if not _is_module_scope_node(row.get(label_key)):
                    kept.append(row)
                    continue
                fp = str(row.get(file_key) or "")
                if fp in seen_files:
                    continue                      # File+Module double-representation of one file
                seen_files.add(fp)
                marked = dict(row)                # don't mutate the shared backend row
                marked["_module_scope"] = fp
                kept.append(marked)
            group.rows = kept

    @staticmethod
    def _drop_edge_collisions(groups: list[_EdgeGroup], file_key: str, label_key: str) -> int:
        """Drop the displayed-side rows that are name collisions rather than real edges, in place.

        The extractor emits an edge for a bare local name, so a symbol whose body says `conn`,
        `tmp_path` or `write` acquires an edge to anything else in the repository carrying that name
        -- including a function in another language, or a node in a data file. A call edge cannot
        cross a language family without an FFI/IPC mechanism the extractor does not emit
        (`_LANG_FAMILIES`), and a `.json`/`.md` file defines no callable at all, so a displayed
        endpoint in a different family than the OTHER end of the edge, or in a non-code file, is a
        collision and dropping it costs nothing real.

        This began as `callees`-only. A Python function's CALLERS are polluted the same way -- a `.ts`
        function three files over that shares the bare name -- so both ops now share the one filter.
        `anchor` is the family of the far end (the group's own key symbol: the caller for `callees`,
        the called symbol for `callers`), so the comparison is per-group and cannot be confused by an
        unrelated same-named symbol elsewhere in the result.

        Module-scope nodes are left untouched, for `_collapse_module_scope` to relabel. The backend
        mis-attributes their file path -- it labelled `examples/aliases/aliases.py`'s module scope
        `aliases.ini`, a sibling file -- so a non-code or cross-language path on one is its own
        artifact, NOT evidence the reference is spurious; the seven click-API references behind that
        one node are genuine. Returns the number dropped, which the caller must disclose."""
        dropped = 0
        for group in groups:
            anchor_fam = _lang_family(group.file)
            keep: list[dict] = []
            for r in group.rows:
                if _is_module_scope_node(r.get(label_key)):
                    keep.append(r)              # a location, relabelled later; never a collision
                    continue
                path = str(r.get(file_key) or "")
                if _is_non_code(path):
                    dropped += 1                # a data/doc file defines no callable
                    continue
                fam = _lang_family(path)
                if fam and anchor_fam and fam != anchor_fam:
                    dropped += 1                # a cross-language collision, against THIS group's key
                    continue
                keep.append(r)
            group.rows = keep
        return dropped

    @staticmethod
    def _looks_like_test(fp: str, name: str) -> bool:
        """Heuristic test detection. The backend's own ``is_test`` flag comes back False for pytest
        functions (verified by dogfooding), so dead-code / hotspot scans must filter by path+name
        or drown in test noise — this is the single most load-bearing renderer detail.

        Every convention here was Python's until this recognised none of JavaScript's, and a real
        repo showed what that costs: on brightsky-ai the ranked-symbols table's top three rows were
        NestJS's `Injectable` (138), `Inject` (59) and `Optional` (20), each bound by name collision
        to `backend/src/__tests__/agent/ack-checkpoint.service.spec.ts`. `__tests__` is not
        `/tests/` and `.spec.ts` is not `_test.py`, so all three sailed through a filter whose whole
        job was to stop them — and they were reported as the repo's most load-bearing code.

        `__tests__` / `__test__` (Jest's directory convention) and the `.spec.` / `.test.` filename
        infixes are added for that. Both are unambiguous — no non-test file is named
        `foo.spec.ts` — which is the bar for widening this filter at all: over-filtering is what
        retired `deadcode` at 25% precision, so a convention gets added when it cannot hide a real
        symbol, not when it merely looks test-shaped.

        `fixtures/` clears that same bar, and this repo's own committed `CODE_INTEL.md` is the
        evidence: **all ten** of its "Entry Points" were `bench/fixtures/corpus_ts/src/*.ts`. Those
        files are not merely test-adjacent — they are written to have a known answer, so a
        `forwardReleasedItem` in one denotes nothing in this project at all. Presenting them to a
        newcomer as the code to understand first is the wrong-answer-that-survives-in-the-tree
        failure the ranking filter exists to stop, and `fixtures/` names test material in every
        ecosystem that has the convention (pytest, Rails, Jest, Go testdata's sibling)."""
        f = (fp or "").lower()
        if f.startswith(("tests/", "test/")) or "/tests/" in f or "/test/" in f:
            return True
        if "__tests__/" in f or "__test__/" in f or f.startswith(("__tests__/", "__test__/")):
            return True
        if f.startswith(("fixtures/", "__fixtures__/")) or "/fixtures/" in f or "/__fixtures__/" in f:
            return True
        base = f.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        return (base.startswith("test_") or base.endswith("_test.py")
                or base == "conftest.py" or (name or "").startswith("test_")
                or stem.endswith((".spec", ".test")))

    @staticmethod
    def _is_synthetic(fp: str) -> bool:
        """Builtins / generated nodes carry an empty or ``<...>`` file_path (e.g. <python-builtins>)."""
        return (not fp) or fp.startswith("<")

    @staticmethod
    def _is_data_file(fp: str) -> bool:
        """Whether *fp* is a pure-data serialization format (JSON) that a permissive parser can
        still emit "symbol"-shaped nodes for — a JSON object's top-level keys, walked as if they
        were definitions — but that never defines a CALLABLE in any language. On this repo's own
        committed CODE_INTEL.md, two untracked JSON blobs (`pathly/project/SPEC.md.comments.json`,
        `...diagrams.json`) ranked as the 2nd and 8th most load-bearing symbols in the project,
        because their top-level keys (`body`, `status`) were indexed as `Variable` nodes with real
        USAGE edges from sibling JSON files reusing the same key names.

        Deliberately a DENYLIST of one unambiguous data extension, not an allowlist of code
        extensions the way `source_kind.is_code_path` is: `_is_noise` is shared by every language
        the graph backend indexes, so allowlisting would silently hide a real symbol in any
        language not on that list — the exact over-filtering failure `deadcode` was retired for
        (see `_WITHDRAWN_OPS` above). `.json` can never contain a callable in any language, so
        excluding it by extension carries none of that risk.

        Scoped to `.json` alone, not its sibling data formats (`.yaml`/`.toml`/...), because that
        is the one measured on this repo — `test_scan_ops_hide_archived_code` pins that a `.yml`
        under `.github/workflows/` is NOT noise (a live workflow, not data-format junk), and a
        broader denylist would need its own evidence before touching that boundary."""
        return fp.rsplit(".", 1)[-1].lower() == "json" if "." in fp else False

    @classmethod
    def _is_noise(cls, r: dict) -> bool:
        """Rows a code-quality scan should hide: builtins/generated nodes, test code (the backend's
        own ``is_test`` is unreliable — see ``_looks_like_test``), and pure-data files a permissive
        parser mistook for symbols (see ``_is_data_file``). Shared by the repo-scan ops.

        Does NOT filter by symbol NAME, on purpose — not even a name shaped like a builtin/stdlib
        method (`get`, `keys`, ...). A real project method named `get` (e.g.
        `ContentHashCache.get` in this very repo) must survive here; a wrong-but-plausible-looking
        fan-in count on a REAL symbol is a backend call-resolution precision problem (bare-name
        matching with no type inference), not a noise-filtering one, and blacklisting the name
        would hide the real symbol along with the noise — the same over-aggressive-filter failure
        that retired `deadcode` at 25% precision. See
        `tests/test_graph_provider.py::test_is_noise_does_not_filter_a_real_symbol_named_get`."""
        fp = str(r.get("file_path") or "")
        return (cls._is_synthetic(fp)
                or cls._looks_like_test(fp, str(r.get("name") or ""))
                or _is_archived_path(fp)
                or cls._is_data_file(fp))

    def _render_scan(self, kept: list[dict], title: str, cap: int, meta_fn) -> str:
        """Render a repo-scan op's markdown from filtered+sorted rows: ``## title (count)`` + one
        ``- label (file)  [meta]`` line per row (top ``cap``) + a ``+N more`` note when truncated.
        ``meta_fn(row) -> list[str]`` supplies the per-op metric badge, so the repo-scan ops share
        the row format and truncation note (the drift-prone parts) and differ only in their metrics."""
        lines = []
        for r in kept[:cap]:
            qualified = str(r.get("qualified_name") or "")
            label = _collapse_repeats(
                _strip_project_prefix(qualified, may_be_filename=False) if qualified
                else _strip_project_prefix(str(r.get("name") or "?")))
            fp = str(r.get("file_path") or "")
            meta = meta_fn(r)
            badge = f"  [{', '.join(meta)}]" if meta else ""
            tail = f"  ({fp})" if fp else ""
            lines.append(f"- {label}{tail}{badge}")
        body = "\n".join(lines)
        if len(kept) > cap:
            body += f"\n… (+{len(kept) - cap} more)"
        return f"## {title} ({len(kept)})\n" + body

    def _no_symbol_matched_the_hint(
        self, op: str, target: str, wanted: _SymbolTarget, candidates: list[_EdgeGroup]
    ) -> str:
        """The caller named a specific symbol, and no symbol matching it has edges of this kind.

        Two wrong answers to avoid. Falling back to every symbol with that bare name answers a
        question the caller explicitly narrowed away from. Reporting zero rows claims the symbol has
        none, which is a statement about the code rather than about the lookup. So: say the hint
        matched nothing, and name the symbols that DO carry the name — the information needed to ask
        again correctly, and already in hand.

        Careful about what is claimed: the population here is the symbols this op's own query
        returned, NOT the index. A symbol can be perfectly well indexed and still be absent from
        this list — `Group.invoke` has callees but no callers on one real repository — so saying
        "not in this index" would be a second false claim in a message written to avoid the first."""
        self._add_gap(
            op, "target-hint-unmatched",
            f"no symbol matching {wanted.describe()} has {op} here; {len(candidates)} other "
            f"symbol(s) named `{wanted.name}` do, so this is not evidence that `{target}` has none",
        )
        # These bullets are candidate SYMBOLS, not result rows, and `impact` can compose this half
        # with a half that did have rows. Flagged so the envelope withholds its row summary rather
        # than publishing a `returned` that undercounts the `- ` lines in the body it describes.
        self._pending_nonrow_lines = True
        # And they are quoted, for the same reason the settle note and the first screen are: an
        # answer's body is parsed back into caller keys by `bench/score.py::graph_answer`, which
        # takes every line starting with `- ` as a row. Rendered flat, this listing scored as a
        # fabricated caller — the benchmark measuring the disclosure instead of the engine, in the
        # direction that makes the tool look worse — and it silenced the `graph_verified` arm
        # outright, which refuses an answer whose body shows rows the envelope does not publish.
        # Withholding the row summary was half the fix; the line shape is the other half.
        listing = "\n".join(f"> - {g.describe()}" for g in candidates[:_CANDIDATE_CAP])
        more = (f"\n> … (+{len(candidates) - _CANDIDATE_CAP} more)"
                if len(candidates) > _CANDIDATE_CAP else "")
        return (f"## {op.capitalize()} of {target}\n"
                f"**No symbol matching {wanted.describe()} has {op} in this index** — which says "
                f"nothing about whether that symbol exists or what it calls; it may simply have no "
                f"edge of this kind. {len(candidates)} symbol(s) named `{wanted.name}` do have "
                f"{op} here:\n" + listing + more)

    def _row_cap_note(self, op: str, target: str) -> str:
        """Disclose a list this op truncated itself.

        A query that came back exactly at its own `LIMIT` has almost certainly been cut short, and
        the rendered list gives no sign of it. For `callees` in particular the whole point is
        "everything this reaches", so a silently-capped list is the partial-reads-as-complete
        failure in its purest form."""
        self._add_gap(
            op, "row-cap-reached",
            f"the query returned the maximum {_EDGE_ROW_LIMIT} rows, so this list is truncated "
            f"and may be missing rows — not a complete answer for `{target}`",
        )
        return (f"\n\n_Truncated: the graph returned the maximum {_EDGE_ROW_LIMIT} rows, so rows "
                f"beyond that are missing from this list._")

    @staticmethod
    def _kind_counts(groups: list[_EdgeGroup]) -> dict[str, int]:
        """How many rows of each relationship kind this answer holds."""
        out: dict[str, int] = {}
        for g in groups:
            for r in g.rows:
                kind = str(r.get("type(c)") or "").strip() or "?"
                # A module-scope row's CALLS/USAGE label is an artifact of which node kind carried
                # the edge (see `_display`), so it is counted as a direct row rather than split out
                # as a distinct KIND of fact. Anything else — notably CALL_REFERENCE — is a real
                # claim about the code and is counted as itself.
                if r.get("_module_scope") is not None and kind in ("CALLS", "USAGE"):
                    kind = _DIRECT_KIND
                out[kind] = out.get(kind, 0) + 1
        return out

    def _kind_note(self, op: str, counts: dict[str, int]) -> str:
        """Name every non-call relationship in the answer, and what it actually asserts.

        The count in the heading is the direct calls. Everything else is real — the edges exist and
        an agent asking "what depends on this" needs them — but it is not a call, and printing it
        under a caller count is the category error that made a registered callback look unused."""
        others = {k: n for k, n in counts.items() if k != _DIRECT_KIND}
        if not others:
            return ""
        total = sum(others.values())
        parts = [f"{n} `{k}` row(s) ({_EDGE_KINDS.get(k, 'relationship kind not described here')})"
                 for k, n in sorted(others.items(), key=lambda kv: -kv[1])]
        self._add_gap(
            op, "non-call-relationships",
            f"{total} of the rows are not direct calls: " + "; ".join(
                f"{n} {k}" for k, n in sorted(others.items(), key=lambda kv: -kv[1])),
        )
        return ("\n\n_Not calls — " + "; ".join(parts) + ". Each row is badged with its kind. "
                "For \"what would break if this changed\" you want all of them; for \"what calls "
                "this\" you want only the `CALLS` rows._")

    def _confidence_note(self, op: str, groups: list[_EdgeGroup]) -> str:
        """Mark every row the backend resolved by NAME rather than by import, and disclose the count.

        This is the fix for the failure that motivated the floor. Asking `callers describe` on a
        TypeScript repo returned 32 rows — every one a call to vitest's global `describe`, imported
        from "vitest" in the very file it appears in — all bound to the project's own
        `domain.budget.describe` because that was the only indexed symbol with the name. The backend
        had stamped 31 of them 0.75 and one 0.38; codeintel dropped the column and rendered all 32
        as plain callers under `confidence: "complete"`, while the one REAL caller (reached through
        an aliased import) was missing entirely.

        What this does NOT do is call every sub-floor row wrong. `runAlerts -> evaluate` is a real,
        hand-verified caller and the backend stamped it 0.75, so a note reading "these may not be
        callers at all" over a two-row answer would replace a false positive with a false alarm —
        the same defect facing the other way. The two tiers are reported as what they are: a
        `unique_name` binding is unverified, a suffix/fuzzy binding is probably junk.

        The one signal that separates the two cases cheaply is the SHARE. A project symbol with a
        genuinely unique name picks up an unverified row here and there; a name the index does not
        own — a framework global, a builtin method — collects nothing else, because every call in
        the repository lands on it. So an answer that is entirely unverified is called out as the
        collision signature it almost always is.

        Rows are marked in place so the badge travels with the row into whichever section the
        renderer puts it, including the per-symbol sections of an ambiguous answer that a note alone
        would never reach. Unstamped rows are counted apart and never badged: silence from the
        backend is not a low score, and flattening the two is the same error one level down.
        """
        weak = unverified = unstamped = no_column = total = 0
        guessed_by: set[str] = set()
        for g in groups:
            for r in g.rows:
                total += 1
                # PROVENANCE FIRST. `c.strategy` says how the edge was resolved, which is the fact;
                # the float is a summary of it. Where the backend reports a strategy it decides,
                # and the numeric floor below is only the fallback for a backend generation that
                # does not (0.9.x, and any edge the newer one leaves unlabelled).
                evidence = _evidence_class(str(r.get("strategy") or ""))
                if evidence == "name-guess":
                    r["_low_confidence"] = _edge_confidence(r)
                    r["_evidence"] = evidence
                    r["_bucket"] = _NAME_MATCHED
                    guessed_by.add(str(r.get("strategy") or "").strip())
                    unverified += 1
                    continue
                if evidence in ("lsp", "import", "same-module"):
                    r["_evidence"] = evidence
                    r["_bucket"] = _RESOLVED
                    continue
                conf = _edge_confidence(r)
                if conf is None:
                    # Two different silences, and only one of them is about the code. A row with NO
                    # `c.confidence` key came from a backend (or a generation) that does not return
                    # the column at all; a row whose key is present but empty is an edge that
                    # backend declined to score. The first says nothing about this answer, the
                    # second says this specific edge's provenance is unknown.
                    r["_bucket"] = _UNSTATED
                    if "c.confidence" in r:
                        unstamped += 1
                    else:
                        no_column += 1
                elif conf <= _EDGE_CONFIDENCE_WEAK:
                    r["_low_confidence"] = conf
                    r["_bucket"] = _NAME_MATCHED
                    weak += 1
                elif conf < _EDGE_CONFIDENCE_FLOOR:
                    r["_low_confidence"] = conf
                    r["_bucket"] = _NAME_MATCHED
                    unverified += 1
                else:
                    # At or above the floor with no strategy reported: the cascade consulted the
                    # file's imports to get here, which is the same class of evidence as a stated
                    # `import_map`, so it is counted as resolved rather than as a third silence.
                    r["_bucket"] = _RESOLVED
        if not (weak or unverified or unstamped):
            return ""
        # A backend that never returns the column at all is not producing partial answers — it is a
        # generation that does not report confidence. Marking every such answer `partial` would
        # repeat, one level up, the defect `attach_confidence` exists to fix: a field that fires
        # everywhere tells a reader nothing, and "partial" has to keep meaning "a named part of THIS
        # answer could not be retrieved". An edge the backend returned UNSCORED is the opposite case
        # and stays a gap, even when every row in one answer happens to be unscored — that is how a
        # symbol the repository never defines (`get`, resolved onto `dict.get`) is caught.
        if no_column == total:
            return ""

        details, parts = [], []
        if weak:
            details.append(
                f"{weak} of {total} row(s) were resolved by suffix or string-similarity match "
                f"(confidence <= {_EDGE_CONFIDENCE_WEAK}) and are likely spurious")
            parts.append(
                f"**{weak} of {total} row(s) are LIKELY SPURIOUS** — resolved by suffix or "
                f"string-similarity match (confidence <= {_EDGE_CONFIDENCE_WEAK}), not by any "
                f"import.")
        if unverified:
            # Name the STRATEGY when the backend reported one. "resolved by `unique_name`" is a
            # fact about how the edge was produced; "confidence < 0.85" is a fact about a threshold
            # this code chose, and only the first tells a reader what to distrust.
            named = ", ".join(f"`{st}`" for st in sorted(s for s in guessed_by if s))
            how = (f"by name matching ({named})" if named
                   else f"by bare symbol name (confidence < {_EDGE_CONFIDENCE_FLOOR})")
            details.append(
                f"{unverified} of {total} row(s) were resolved {how}, not by following an import or "
                f"a language-server binding")
            parts.append(
                f"**{unverified} of {total} row(s) are UNVERIFIED** — resolved {how}, not by "
                f"following an import or a language-server binding. Correct when the call really "
                f"targets this symbol; wrong when it targets a same-named symbol the index never "
                f"saw.")
        if unstamped:
            details.append(
                f"{unstamped} of {total} row(s) carry no confidence from the backend at all, so how "
                f"they were resolved is unknown")
            parts.append(
                f"{unstamped} of {total} row(s) carry no confidence from the backend, so how they "
                f"were resolved is unknown — re-index to have them scored.")
        # The collision signature: nothing here was resolved through an import, across enough rows
        # that the pattern means something. A project symbol collects the occasional unverified
        # caller; a name the index does not own collects every call in the repository.
        if total >= 5 and (weak + unverified) == total:
            # Raised as its OWN kind, not folded into the one above, because this is the condition
            # the gateway escalates on: it is machine-checkable, and matching on a phrase inside a
            # prose `detail` would be a string-matching contract between two modules — the kind that
            # breaks silently the first time the wording is improved.
            self._add_gap(
                op, "all-rows-name-resolved",
                "no row in this answer was resolved through an import, which is the signature of a "
                "name the index does not own (a library function, a framework global, a builtin "
                "method) collecting every call site that mentions it",
            )
            parts.append(
                "**Not one row here was resolved through an import.** That is the signature of a "
                "name this index does not own — a library function, a framework global, a builtin "
                "method — collecting every call site in the repository that mentions it. Treat the "
                "whole answer as unconfirmed until `--engine lsp` agrees.")
        self._add_gap(op, "low-confidence-edges", "; ".join(details))
        marked = "Marked `[?…]` below. " if (weak or unverified) else ""
        return "\n\n_" + marked + " ".join(parts) + "_"

    def _collision_note(self, op: str, dropped: int) -> str:
        """Disclose rows dropped as name collisions, in the body AND as a machine-readable gap.

        Shared by both edge ops so one cannot end up disclosing while the other stays silent -- the
        exact drift this project keeps guarding against. The far end named differs by op: a `callees`
        row is a collision against the CALLER, a `callers` row against the symbol being called."""
        if not dropped:
            return ""
        anchor = "caller" if op == "callees" else "called symbol"
        self._add_gap(
            op, "name-collisions-dropped",
            f"{dropped} row(s) were dropped as name collisions (a different language, or a "
            f"non-code file, than the {anchor}); resolution is by symbol name, not by type",
        )
        return (f"\n\n_{dropped} row(s) dropped as name collisions (a different language, or a "
                f"non-code file, than the {anchor})._")

    def _empty_edge_answer(
        self, op: str, unit: str, target: str, wanted: _SymbolTarget,
        selected: list[_EdgeGroup], notes: str, truncated: bool, dropped: int,
    ) -> str:
        """Every row this op found was set aside by its own collision filter.

        `rows` was non-empty (the genuine miss returned None earlier), so this is an answer WE
        emptied, not an absence in the repository. Routing it into the `not-in-graph` branch would
        read as "0 {unit}s" -- a statement about the code -- when the honest one is "N found, all
        filtered for a reason that has nothing to do with it". Shared by both ops for the same
        anti-drift reason."""
        if not dropped:                # nothing found and nothing dropped cannot both be true
            self._add_gap(
                op, "name-collisions-dropped",
                "every row returned was set aside, so this may under-report — resolution is "
                "by symbol name, not by type",
            )
        return (f"## {op.capitalize()} of {target} (0)\n(no {unit} survived name-collision filtering)"
                + notes + self._name_resolution_note(wanted, selected, truncated))

    @staticmethod
    def _evidence_headline(groups: list[_EdgeGroup], unit: str) -> str:
        """The heading's count, broken out by how the rows were resolved — or `""`.

        `_render_edge_answer` already refuses a single number when the rows are different KINDS of
        fact ("48 direct, 2 other reference(s)"). This is the same rule on the other axis, and it is
        the axis that actually misleads. Asking a 1,483-file monorepo for `callers` of
        `StrategyChain.resolve` answers `(48 direct, 2 other reference(s))` when five files in the
        repository mention `StrategyChain` at all and the true answer is two. Both true callers are
        in the list and the note beneath says 43 of 50 rows were name-matched — but the note is
        beneath FIFTY rows, and the first line a reader sees is the one that says 48.

        So the breakdown goes above the rows, where the misleading number is.

        Silent in the two cases where it would be noise rather than news: when every row falls in
        one bucket the plain heading is already honest, and a backend generation that reports no
        confidence column at all stamps every row `unstated`, which is that same single-bucket case
        and not a finding about this answer.
        """
        counts: dict[str, int] = {}
        for g in groups:
            for r in g.rows:
                bucket = str(r.get("_bucket") or "")
                if bucket:
                    counts[bucket] = counts.get(bucket, 0) + 1
        present = [(b, counts[b]) for b in (_RESOLVED, _NAME_MATCHED, _UNSTATED) if counts.get(b)]
        if len(present) < 2:
            return ""
        tally = " · ".join(f"{n} {b}" for b, n in present)
        return (
            f"**{tally}.** The heading counts rows, not confirmed {unit}s — only the `{_RESOLVED}` "
            f"rows followed an import or a language-server binding. Per-row detail below.\n"
        )

    # How many name-matched rows make a command worth printing. Below this the reader can eyeball
    # the badges; at or above it, "go and check" is advice without a method.
    _SETTLE_FLOOR = 3

    @staticmethod
    def _discriminator(wanted: _SymbolTarget, groups: list[_EdgeGroup]) -> tuple[str, str] | None:
        """The token whose presence a real caller cannot avoid, and what to call it. ``None`` when
        the target offers nothing sharper than its own leaf name.

        A name-matched row was bound by the LEAF name, so grepping the leaf name reproduces exactly
        the population that is in doubt — it would return the same 48 files and settle nothing. The
        token that discriminates is the part of the target the match did NOT use:

        * a qualified target carries its own — `StrategyChain` from `StrategyChain.resolve`, and a
          file that never writes `StrategyChain` is not calling that method;
        * a bare target falls back to the stem of the file it is DEFINED in, which any caller must
          name in an import to reach it.

        Returns ``None`` rather than guessing when neither is available, because a command that
        cannot settle anything is worse than no command: it looks like diligence.
        """
        qualified = wanted.qualified or (groups[0].qn_raw if groups else "")
        parts = [p for p in str(qualified).replace("/", ".").split(".") if p]
        # The segment before the leaf, when it is not the leaf itself.
        if len(parts) >= 2 and parts[-1] == wanted.name and parts[-2] != wanted.name:
            return parts[-2], "the name it is qualified by"
        defining = wanted.file_hint or (groups[0].file if groups else "")
        stem = str(defining).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem and stem != wanted.name:
            return stem, "the module it is defined in"
        return None

    def _settle_name_matches(
        self, groups: list[_EdgeGroup], wanted: _SymbolTarget, unit: str, file_key: str,
    ) -> str:
        """The exact command that would settle the name-matched rows, or `""`.

        DISCLOSING doubt and DISCHARGING it are different services, and this project had only been
        doing the first. An agent reading `43 name-matched` is told the answer may be wrong and
        left to design its own check — and it has `rg`, so the check is cheap, but only if it knows
        which string to grep. That string is derivable here and nowhere else: this is the only
        place that holds the target's qualifier, the defining file and the set of rows in doubt at
        the same time.

        On the case that motivated it — `callers StrategyChain.resolve` on a 1,483-file monorepo,
        48 rows of which 43 were name-matched — the command prints the five files that mention
        `StrategyChain` at all, against which 43 of the 48 rows cannot be real. That is the check
        a person ran by hand to establish the finding in `bench/README.md`; printing it is the
        difference between a warning and a next step.

        Deliberately one command, and deliberately not a claim. It narrows; it does not decide. A
        file that appears in the output may still not call the symbol, and the wording says so —
        overstating what a grep proves would be the same defect this file is full of fixes for.
        """
        matched = [r for g in groups for r in g.rows if r.get("_bucket") == _NAME_MATCHED]
        if len(matched) < self._SETTLE_FLOOR:
            return ""
        total = sum(len(g.rows) for g in groups)
        # "Dominate": at least half the answer, or all of it. Below half, the resolved rows carry
        # the answer and a command here would be noise on a result that is mostly evidence.
        if len(matched) * 2 < total:
            return ""
        found = self._discriminator(wanted, groups)
        if found is None:
            return ""
        token, why = found

        root = getattr(self, "_answered_root", None) or "."
        files = sorted({str(r.get(file_key) or "") for r in matched if r.get(file_key)})
        listing = ""
        if files:
            shown = ", ".join(f"`{f}`" for f in files[:10])
            more = f", … (+{len(files) - 10} more)" if len(files) > 10 else ""
            listing = f"_The {len(files)} file(s) to check against: {shown}{more}._\n"
        return (
            f"\n\n_Settle it: `rg -n --fixed-strings '{token}' {root}`_\n"
            f"_`{token}` is {why}, and it is the part of the target the name match did not use. "
            f"A file that never names it cannot be reaching this symbol, so any of the "
            f"{len(matched)} name-matched {unit}s below whose file is absent from that output is "
            f"spurious. Appearing in it is not proof of a call — it narrows the list, it does not "
            f"decide it._\n"
            + listing
        )

    @staticmethod
    def _structured_row(row: dict, name_key: str, qn_key: str, file_key: str,
                        unit: str) -> dict[str, Any]:
        """One rendered row, as fields an agent can branch on without reading the line.

        The badges, the notes and the headline all say the same things in prose, and an integration
        that wants to act on them has to parse markdown that this project reserves the right to
        reword. Everything here is already known at render time; none of it is re-derived, so the
        structured row and the printed row cannot disagree about a fact.

        `verified` is the field to filter on and the only one that is a VERDICT: true exactly when
        the edge was followed through an import or a language-server binding. An unstamped row is
        not verified — silence from the backend is not evidence — and it is not `possible` either,
        which is why `evidence` keeps all three states rather than collapsing to a boolean pair.

        `relation` says which side of the edge this row is, because `impact` renders callers and
        callees into ONE answer and therefore into one `rows` list. Without it a reader filtering
        structurally cannot tell "what calls this" from "what this calls" — the two questions an
        impact answer exists to keep apart — and the list would be filterable but not safely so.

        There is no receiver/type evidence field. The readiness doc asks for one "when available"
        and it is never available: the backend reports no receiver type, so a key here would be
        `null` on every row of every answer — a field that promises a capability nobody has.
        """
        bucket = str(row.get("_bucket") or _UNSTATED)
        strategy = str(row.get("strategy") or "").strip()
        confidence = row.get("_low_confidence")
        scope = row.get("_module_scope")
        edge = str(row.get("type(c)") or "").strip()
        return {
            "relation": unit,
            "name": str(row.get(name_key) or ""),
            "qualified_name": _strip_project_prefix(
                str(row.get(qn_key) or ""), may_be_filename=False),
            "file": str(scope) if scope is not None else str(row.get(file_key) or ""),
            "module_scope": scope is not None,
            "edge": edge or None,
            "verified": bucket == _RESOLVED,
            "evidence": bucket,
            "strategy": strategy or None,
            "confidence": float(confidence) if confidence is not None else None,
            "why": {
                _RESOLVED: "followed an import or a language-server binding",
                _NAME_MATCHED: (f"matched by name ({strategy})" if strategy
                                else "matched by bare symbol name, not by following a binding"),
                _UNSTATED: "the backend reported no provenance for this edge",
            }.get(bucket, "unclassified"),
        }

    def _record_rows(self, rendered: list[dict], row_keys: tuple[str, str, str], unit: str,
                     *, row_cap_hit: bool, withheld: int) -> None:
        """Publish the structured form of the rows this renderer just PRINTED.

        Held on the provider and read by `build_result`, the same way `_pending_gaps` is. That is
        not the shape this should end in — `docs/refactor-graph-provider.md`'s open phase 4 would
        have a renderer RETURN its rows and gaps instead of mutating the provider — but inventing a
        second mechanism beside the existing one would make that eventual change harder rather than
        easier.

        `rendered` is the list that was handed to `_display`, not the list that was retrieved, and
        the difference is the whole correspondence this method exists to keep: `rows` and the `- `
        lines in the body are then the same rows by construction rather than by two derivations
        agreeing. Rows held back by the candidate cap are counted in `withheld`, not published.

        ACCUMULATES rather than assigns, because one answer is not always one render. `impact`
        calls `callers` and `callees` and prints both, so a method that overwrote would leave the
        envelope summarising the second half of a body containing both — a count true of the last
        render and false of the answer, which is this repository's recurring defect with the rows
        in hand.
        """
        self._pending_rows += tuple(
            self._structured_row(r, *row_keys, unit) for r in rendered)
        self._pending_row_cap = self._pending_row_cap or bool(row_cap_hit)
        self._pending_withheld += withheld

    def _settle_evidence(self) -> dict[str, Any] | None:
        """The counts that summarise `_pending_rows` — computed once the answer is whole.

        Deliberately NOT computed in `_record_rows`. Three of the gaps an edge answer can raise are
        recorded after its rows are rendered (`target-ambiguous` and `non-call-relationships` inside
        the renderer, `ancestor-scope` and a backend failure in `build_result`), so a
        `safe_for_destructive` derived at render time reads a gap list that is not yet the answer's.
        Measured on the working tree before this split: an answer over three same-named symbols came
        back `confidence: partial`, `gaps: [target-ambiguous]` and `safe_for_destructive: true` —
        the envelope contradicting itself in the one direction that ends in a deletion.

        `total` is `None` exactly when the BACKEND row cap was hit: we know it had at least
        `returned` and we do not know how many, and reporting `returned` as the total is how a
        capped list comes to read as a complete one. The candidate cap is the other case and is not
        the same case — those rows are in hand and counted, so `total` states them and `truncated`
        still says the printed list is not all of it.
        """
        # One branch prints `- ` lines that are not result rows: `_no_symbol_matched_the_hint`
        # lists the symbols that DO carry the name. A row summary over a body containing those
        # would miscount the moment `impact` composes that half with a half that did have rows, so
        # the summary is withheld rather than stated wrongly.
        if self._pending_nonrow_lines or not self._pending_rows:
            return None
        rows = self._pending_rows
        counts = {bucket: sum(1 for r in rows if r["evidence"] == bucket)
                  for bucket in (_RESOLVED, _NAME_MATCHED, _UNSTATED)}
        withheld = self._pending_withheld
        return {
            "verified": counts[_RESOLVED],
            "possible": counts[_NAME_MATCHED],
            "unstated": counts[_UNSTATED],
            "returned": len(rows),
            "total": None if self._pending_row_cap else len(rows) + withheld,
            "truncated": bool(self._pending_row_cap or withheld),
            # Deliberately conservative, and deliberately a DERIVED field rather than a judgement:
            # every row followed a real binding, the list is whole, and nothing about the answer is
            # disclosed as missing. Anything less and the honest answer to "can I delete this?" is
            # no. A reader who wants a looser rule has the three counts to write it themselves.
            "safe_for_destructive": bool(
                counts[_NAME_MATCHED] == 0 and counts[_UNSTATED] == 0
                and not self._pending_row_cap and not withheld and not self._pending_gaps),
        }

    def _row_noun(self) -> str:
        """What the rows in hand are, for the first screen — `caller`, `callee` or neither.

        `impact` answers two questions in one body, so its rows are not all callers and calling
        them callers would be a summary that is true of most of the list and false of the rest."""
        relations = {str(r.get("relation") or "") for r in self._pending_rows}
        return relations.pop() if len(relations) == 1 and all(relations) else "row"

    def _first_screen(self, ev: dict[str, Any] | None) -> str:
        """The verdict, above everything, in four lines — or `""` when there is nothing to warn of.

        The readiness doc's first Phase 3 item, and the reason it is first: every disclosure this
        engine makes is already correct and most of them are BELOW fifty rows. A reader who acts on
        the heading never reaches them.

        Prepended by `build_result` rather than written into the renderer's own heading, for the
        same reason `_settle_evidence` computes there: this states the whole answer's confidence,
        and inside the renderer neither the gap list nor — for `impact` — the row list is whole yet.
        It also means one banner over an impact answer instead of two, each describing half of it.

        Silent on a clean answer. A banner printed over every result is furniture, and furniture is
        not read — the same argument `_evidence_headline` makes for its own silence on a single
        bucket, and the reason `bench/run.py daycap` had to stay byte-identical when the
        `unresolvable` disclosure landed.

        No line here may begin with `- `: `bench/score.py::graph_answer` reads every such line in
        the body as a result row, so prose in that shape is scored as a fabricated caller. Pinned
        by `test_the_first_screen_can_never_be_read_as_result_rows`.
        """
        if not ev or ev["safe_for_destructive"]:
            return ""
        noun = self._row_noun()
        lines = [f"> **Confidence: {'partial' if self._pending_gaps else 'complete'}**"]
        if ev["verified"] or ev["possible"]:
            lines.append(f"> Verified {noun}s: {ev['verified']} · "
                         f"possible: {ev['possible']} · unstated: {ev['unstated']}")
        if ev["truncated"]:
            total = ev["total"]
            shown = (f"{ev['returned']} shown, {total} in total" if total is not None
                     else f"{ev['returned']} shown, total unknown")
            lines.append(f"> Truncated: yes — {shown}")
        lines.append("> Safe for destructive decisions: **no**")
        return "\n".join(lines) + "\n\n"

    def _render_edge_answer(
        self, op: str, unit: str, target: str, wanted: _SymbolTarget,
        groups: list[_EdgeGroup], row_keys: tuple[str, str, str], truncated: bool,
        extra_notes: str = "",
    ) -> str:
        """Render one edge-op answer from rows already grouped by the symbol they belong to.

        Shared by `callers` and `callees` for the same reason `_render_scan` is shared by the
        repo-scan ops: the drift-prone parts are the heading, the ambiguity disclosure and the
        truncation note, and having two copies of those is how one op ends up honest and the other
        one silent. Each group's heading names the matched TARGET symbol and its rows are the other
        end of the edge, which is the same shape in both directions."""
        name_key, qn_key, file_key = row_keys
        answered = [g for g in groups if g.rows]
        kept = sum(len(g.rows) for g in answered)
        # Rows are ordered so the direct calls come first — they are the answer to the question
        # that was asked — and, within those, the rows that followed a real binding come before the
        # ones matched by name. A reader who stops after the first few should be stopping on the
        # evidence, not on whichever guess the backend happened to return first.
        _ORDER = {_RESOLVED: 0, _UNSTATED: 1, _NAME_MATCHED: 2}
        for g in answered:
            g.rows.sort(key=lambda r: (str(r.get("type(c)") or "") != _DIRECT_KIND,
                                       _ORDER.get(str(r.get("_bucket") or ""), 1)))
        counts = self._kind_counts(answered)
        direct = counts.get(_DIRECT_KIND, 0)
        others = kept - direct
        # A single count is honest only when every row is the same kind of fact.
        head = (f"## {op.capitalize()} of {target} ({kept})\n" if not others
                else f"## {op.capitalize()} of {target} "
                     f"({direct} direct, {others} other reference(s))\n")
        head += self._evidence_headline(answered, unit)
        # Disclosure says the answer may be wrong; this says how to find out. Placed with
        # the headline because that is the number it is about.
        head += self._settle_name_matches(answered, wanted, unit, file_key)

        if len(answered) == 1:
            rendered = list(answered[0].rows)
            withheld = 0
            body = head + "\n".join(self._display(r, name_key, qn_key, file_key)
                                    for r in rendered)
        else:
            # Several distinct symbols share the name. Keep every one of them, each under its own
            # heading — a merged list presented as one symbol's answer is the reading these ops most
            # need to prevent, since they feed "is this safe to change?".
            self._add_gap(
                op, "target-ambiguous",
                f"{len(answered)} distinct symbols named `{wanted.name}` match this target; their "
                f"{unit}s are grouped separately rather than merged into one list. Narrow the target "
                f"with a qualified name or `{wanted.name}@<file>` to answer about one of them",
            )
            shown = answered[:_CANDIDATE_CAP]
            rendered = [r for g in shown for r in g.rows]
            # Rows past the candidate cap are printed by nobody, so they are published by nobody
            # either — but they are counted, because "12 of 17 shown" and "12 shown, total
            # unknown" are different facts and only one of them is true here.
            withheld = sum(len(g.rows) for g in answered[_CANDIDATE_CAP:])
            sections = [
                f"### {g.describe()} — {len(g.rows)} {unit}(s)\n"
                + "\n".join(self._display(r, name_key, qn_key, file_key) for r in g.rows)
                for g in shown
            ]
            if len(answered) > _CANDIDATE_CAP:
                sections.append(f"… (+{len(answered) - _CANDIDATE_CAP} more symbol(s) with this "
                                f"name, not shown)")
            body = (head
                    + f"**{len(answered)} distinct symbols in this index are named `{wanted.name}`** "
                      f"— these are that many separate answers, not one. Ask again as "
                      f"`{answered[0].label or wanted.name}` or "
                      f"`{wanted.name}@{answered[0].file or '<file>'}` for a single symbol.\n"
                    + "\n" + "\n\n".join(sections))
        # Recorded from the list that was displayed, after it was displayed: `rows` and the `- `
        # lines beneath this heading are then the same rows, not two derivations that agree today.
        self._record_rows(rendered, row_keys, unit, row_cap_hit=truncated, withheld=withheld)
        return (body + self._kind_note(op, counts) + extra_notes
                + self._name_resolution_note(wanted, answered, truncated))

    def _name_resolution_note(
        self, wanted: _SymbolTarget, groups: list[_EdgeGroup], truncated: bool
    ) -> str:
        """State how the target was resolved — which is a different fact in each case.

        The old note was one conditional sentence for every answer ("IF more than one symbol is
        called X, their callees are merged here"), which is true but tells the reader to worry
        without saying whether there is anything to worry about. Grouping means the count is now
        known, so this says which of the three situations produced the answer in hand. It does not
        claim uniqueness when the row cap was hit: a symbol whose rows fell past the cap is
        indistinguishable from one that does not exist."""
        if wanted.narrowed:
            named = ", ".join(g.describe() for g in groups) or "nothing"
            return (f"\n\n_Narrowed by the {wanted.describe()} in the target to {named}. Other "
                    f"symbols named `{wanted.name}` are not included._")
        if len(groups) == 1 and groups[0].label and not truncated:
            # Being the only indexed holder of a name is not only a reassurance — it is the exact
            # precondition for the backend's `unique_name` strategy, which binds ANY unresolved call
            # to that bare name here, including calls to symbols this index never saw (an npm or pip
            # package, a test-framework global, a builtin method). So the sentence that used to end
            # in "the only symbol …" now says what that implies, because on the evaluated repository
            # it was the whole cause of a 32-row answer in which no row was a caller.
            # The consequence clause is only true of an answer that HAS such rows. Printing it over
            # a fully import-resolved answer would point at badges that are not there, which is the
            # cry-wolf failure the tiering above exists to avoid, arriving through the legend.
            badged = any(r.get("_low_confidence") is not None
                         for g in groups for r in g.rows)
            because = (
                f" — which is also why any unresolved call to a `{wanted.name}` the index does not "
                f"contain (a library function, a framework global, a builtin method) binds here. "
                f"Rows badged `[?…]` are those bindings."
            ) if badged else "."
            return (f"\n\n_Resolved by symbol NAME, not by type: {groups[0].describe()} is the "
                    f"only symbol named `{wanted.name}` with edges in this index{because}_")
        return (f"\n\n_Resolved by symbol NAME, not by type: if more than one symbol in this "
                f"repository is called `{wanted.name}`, their edges are reported together here. "
                f"Verify before relying on a row you did not expect._")
