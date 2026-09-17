"""The graph operations: one method per question a caller can ask.

Split out of `providers/graph.py` alongside `graph_answer.py`. What is left in the provider after
these two moves is the part that is genuinely about talking to a backend — transport, project
resolution, the op gate, and the envelope — which is the seam
`docs/refactor-graph-provider.md` drew for phases 2 and 3 and the shape its "Op orchestration
(stays)" column already predicted.

Each method here answers ONE question and returns rendered text or `None`; none of them builds an
envelope. That boundary is why the op gate, the withdrawal list and the never-raise wrapper can
stay in one place: an op that finds nothing returns `None` and `build_result` decides what that
means, so "not indexed", "no edges" and "the backend failed" cannot collapse into one another here
by accident.

A MIXIN, for the reason given at the top of `graph_answer.py`: the ops call `self._run`,
`self._query_rows` and `self._search_symbols`, and those are exactly the seams ~18 test stubs
replace by assignment. Inheritance leaves every stub intercepting the same call it always did.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codeintel.graph_answer import AnswerRendering
from codeintel.graph_confidence import _EDGE_CONFIDENCE_FLOOR, _edge_confidence, _evidence_class
from codeintel.graph_edges import _DIRECT_KIND, _EDGE_ROW_LIMIT, _group_edges
from codeintel.graph_render import (
    _cypher_literal,
    _int_or_zero,
    _label_of,
    _language_coverage_note,
    _repo_display_name,
    _strip_project_prefix,
)
from codeintel.graph_targets import _parse_symbol_target
from codeintel.source_kind import is_code_path


class GraphOps(AnswerRendering):
    """One method per graph op. Mixed into `GraphProvider`.

    Inherits `AnswerRendering` rather than declaring its render methods, because the dependency is
    real: an op's shape is query, filter, then render, and the alternative was fourteen stub
    signatures asserting a relationship the code already has. The MRO `GraphProvider` ends up with
    is unchanged either way.

    What it still needs from the provider is transport and per-query state, declared below.
    """

    if TYPE_CHECKING:
        # Supplied by `GraphProvider`. These are the seams ~18 test stubs replace by assignment,
        # so they must stay overridable METHODS on the provider — not calls into a collaborator
        # object, which is the deviation `docs/refactor-graph-provider.md` records for phase 2.
        _last_failure: Any

        def _run(self, method: str, payload: dict, timeout_ms: int) -> Any | None: ...
        def _query_rows(self, cypher: str, project: str, timeout_ms: int) -> list[dict]: ...
        def _search_symbols(
            self, extra: dict, project: str, timeout_ms: int) -> list[dict] | None: ...
        def _answered_root_mismatch(self, asked_root: str) -> bool: ...

    # ------------------------------------------------------------------ ops

    def _op_callers(self, target: str, project: str, timeout_ms: int) -> str | None:
        """What calls or uses *target*.

        Honours the same disambiguator `callees` does (`_SymbolTarget`), applied to the far end of
        the edge — the symbol being called. Without it, `impact` would narrow one of its two halves
        and not the other, and a blast-radius answer whose callers belong to a DIFFERENT symbol of
        the same name is worse than an un-narrowed one: it reads as precise."""
        wanted = _parse_symbol_target(target)
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE|CALL_REFERENCE]->(b) WHERE b.name="{_cypher_literal(wanted.name)}" '
            "RETURN a.name, a.qualified_name, a.file_path, labels(a), type(c), c.confidence, "
            "c.strategy AS strategy, "
            f"b.name, b.qualified_name, b.file_path LIMIT {_EDGE_ROW_LIMIT}"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        truncated = len(rows) >= _EDGE_ROW_LIMIT

        called = _group_edges(rows, "b.name", "b.qualified_name", "b.file_path")
        selected = [g for g in called if wanted.matches(g.qn_raw, g.file)]
        if wanted.narrowed and not selected:
            return self._no_symbol_matched_the_hint("callers", target, wanted, called)

        # The caller is the displayed side here, so both the collision pollution and the module-scope
        # pseudo-nodes land in the rows a reader sees. Drop the cross-language / non-code collisions
        # (a `.ts` function three files over sharing the bare name is not a caller), relabel the
        # module-scope nodes that remain, and disclose anything dropped — all before the renderer
        # counts or prints a row.
        dropped = self._drop_edge_collisions(selected, "a.file_path", "labels(a)")
        self._collapse_module_scope(selected, "labels(a)", "a.file_path")
        notes = self._confidence_note("callers", selected) + self._collision_note("callers", dropped)
        if truncated:
            notes = self._row_cap_note("callers", target) + notes
        if not any(g.rows for g in selected):
            return self._empty_edge_answer(
                "callers", "caller", target, wanted, selected, notes, truncated, dropped)
        return self._render_edge_answer(
            "callers", "caller", target, wanted, selected,
            ("a.name", "a.qualified_name", "a.file_path"), truncated, notes)

    def _op_callees(
        self,
        target: str,
        project: str,
        timeout_ms: int,
        *,
        include_references: bool = False,
    ) -> str | None:
        """What *target* calls.

        A direct ``callees`` query follows CALLS only. ``impact`` opts into USAGE and
        CALL_REFERENCE as well because changing a symbol can affect non-call references. Keeping
        those two questions distinct prevents a type annotation or constant mention from being
        presented under a heading that promises executable calls.

        This keys on the UNQUALIFIED name, which is the honest limitation of the traversal: every
        node named `write_board_mirror` matches, and so do the edges out of all of them. Worse, the
        extractor emits edges for bare local names, so a function whose body says `f.write(...)`,
        `conn`, `tmp_path` or `snapshot` acquires edges to whatever else in the repository happens
        to carry those names. On one evaluated symbol that produced five wrong rows out of seven —
        including a TypeScript function in an Electron preload reached from a Python file-writer,
        and a JSON file inside an `.archive/` directory reported as a callee.

        Two of those three causes are upstream in the extractor and can only be filtered here. This
        does filter them: a callee in a different language family than the caller, or in a file that
        is not code at all, is not a callee — it is a name collision, and dropping it costs nothing
        real.

        The remaining cause — several distinct symbols sharing the bare name — is not a collision to
        drop but a question to ask. It used to be neither: rows from every matched caller were
        flattened into one list, so the answer was the union of several questions with nothing
        saying which row came from where. Now the rows are GROUPED by their caller, and:

        * a target carrying a disambiguator (`pkg.mod.handle`, `handle@src/mod.py`) selects one
          group and answers only for it — resolution rather than disclosure;
        * without one, every group is rendered under its own heading, the count of same-named
          symbols is stated, and nothing is dropped for being ambiguous. Three symbols named
          `handle` is not a degraded answer, it is a question, and the result can ask it.

        Grouping also makes the language check structural. It is resolved against the group's own
        caller file, so the union-across-callers bug that `0.15.5` fixed — a `.ts` callee reached
        from a *Python* caller surviving because an unrelated TypeScript caller shared the bare name
        contributed `ts-js` to a shared set — is no longer expressible here: there is no shared set
        to compare against.
        """
        wanted = _parse_symbol_target(target)
        relationships = "CALLS|USAGE|CALL_REFERENCE" if include_references else "CALLS"
        cypher = (
            f'MATCH (a)-[c:{relationships}]->(b) WHERE a.name="{_cypher_literal(wanted.name)}" '
            "RETURN b.name, b.qualified_name, b.file_path, labels(b), type(c), c.confidence, "
            "c.strategy AS strategy, "
            f"a.name, a.qualified_name, a.file_path LIMIT {_EDGE_ROW_LIMIT}"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        truncated = len(rows) >= _EDGE_ROW_LIMIT

        callers = _group_edges(rows, "a.name", "a.qualified_name", "a.file_path")
        selected = [g for g in callers if wanted.matches(g.qn_raw, g.file)]
        if wanted.narrowed and not selected:
            return self._no_symbol_matched_the_hint("callees", target, wanted, callers)

        dropped = self._drop_edge_collisions(selected, "b.file_path", "labels(b)")
        # Symmetric with `callers`: relabel any module-scope pseudo-node on the displayed (callee)
        # side. The backend never emits a File/Module node as a callee today — the callee side of
        # every edge is a Function/Method/Class/Variable/Decorator — so this is a no-op now, kept so
        # the two ops treat the pseudo-node population identically and a future callee container is
        # handled without a second fix.
        self._collapse_module_scope(selected, "labels(b)", "b.file_path")
        notes = self._confidence_note("callees", selected) + self._collision_note("callees", dropped)
        if truncated:
            notes = self._row_cap_note("callees", target) + notes
        if not any(g.rows for g in selected):
            return self._empty_edge_answer(
                "callees", "callee", target, wanted, selected, notes, truncated, dropped)
        return self._render_edge_answer(
            "callees", "callee", target, wanted, selected,
            ("b.name", "b.qualified_name", "b.file_path"), truncated, notes)

    def _op_impact(self, target: str, project: str, timeout_ms: int) -> str | None:
        callers = self._op_callers(target, project, timeout_ms)
        callees = self._op_callees(target, project, timeout_ms, include_references=True)
        if callers is None and callees is None:
            return None
        # callers/callees already carry their own "## Callers of X (N)" header — don't wrap them
        # in a second "### Callers" header (that produced a redundant double heading).
        parts = [f"## Impact of {target}"]
        parts.append(callers or f"## Callers of {target} (0)\n(none found)")
        parts.append(callees or f"## Callees of {target} (0)\n(none found)")
        return "\n".join(parts)

    def _op_chain(self, target: str, project: str, timeout_ms: int) -> str | None:
        # Accept an "A->B" form (trace from the source symbol) or a bare symbol.
        src = target.split("->")[0].strip() if "->" in target else target.strip()
        # A `name@file` hint is codeintel's own disambiguator (see `_SymbolTarget`); `trace_path`
        # would look for a function literally called that and report nothing found. A DOTTED target
        # is deliberately left intact — the backend resolves qualified names itself and reports its
        # own ambiguity, which is better information than anything reconstructed from a bare name.
        if "@" in src:
            head, _, tail = src.rpartition("@")
            if head.strip() and tail.strip():
                src = head.strip()
        if not src:
            return None
        raw = self._run(
            "trace_path",
            # `edge_types` widens the walk to the relationship that records a callback being
            # registered, so a chain no longer stops dead at the point a function is handed to
            # something else rather than invoked.
            #
            # `include_evidence` replaces `risk_labels`, which the backend treats as mutually
            # exclusive with it. Nothing is lost: `risk` is a restatement of hop distance
            # (hop 1 = CRITICAL, hop 2 = HIGH, hop 3 = MEDIUM) and the hop is already printed on
            # every row, so it dressed a number this op shows anyway as an assessment it never
            # made. Evidence is the fact it could not previously report — how each hop was
            # resolved.
            {"project": project, "function_name": src, "mode": "calls",
             "direction": "both", "include_evidence": True,
             "edge_types": ["CALLS", "CALL_REFERENCE"]},
            timeout_ms,
        )
        if not isinstance(raw, dict):
            return None
        if raw.get("status") == "ambiguous":
            sugg = raw.get("suggestions") or []
            names = [_label_of(s) for s in sugg if isinstance(s, dict)]
            if not names:
                return None
            body = "\n".join(f"- {n}" for n in names)
            return f"## Ambiguous symbol '{src}' — candidates\n{body}"

        def _fmt(items: Any) -> list[str]:
            out = []
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    nm = str(it.get("name") or "?")
                    qn = _strip_project_prefix(str(it.get("qualified_name") or ""),
                                               may_be_filename=False)
                    hop = it.get("hop")
                    label = qn or nm
                    hop_s = f" [hop {hop}]" if hop is not None else ""
                    # How this hop was resolved, in the same vocabulary `callers` uses, so a reader
                    # does not have to learn two. A guessed hop is the one that makes a whole chain
                    # downstream of it suspect, and it used to be indistinguishable.
                    klass = _evidence_class(str(it.get("strategy") or ""))
                    ev_s = ""
                    if klass == "name-guess":
                        ev_s = " [?name-guess]"
                    elif klass:
                        ev_s = f" [{klass}]"
                    risk = it.get("risk")
                    risk_s = f" [risk: {risk}]" if risk and not klass else ""
                    out.append(f"- {label}{hop_s}{ev_s}{risk_s}")
            return out

        callees = _fmt(raw.get("callees"))
        callers = _fmt(raw.get("callers"))
        if not callees and not callers:
            return None
        parts = [f"## Call chain for {src}"]
        parts.append("### Callees (downstream)")
        parts.extend(callees or ["(none)"])
        parts.append("### Callers (upstream)")
        parts.extend(callers or ["(none)"])
        return "\n".join(parts)

    def _op_pattern(self, target: str, project: str, timeout_ms: int) -> str | None:
        try:
            raw = self._run("search_code", {"project": project, "pattern": target}, timeout_ms)
            results = raw.get("results") if isinstance(raw, dict) else raw
            if not isinstance(results, list) or not results:
                return f'## Pattern matches for "{target}"\n(no matches)'
            lines = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                node = _strip_project_prefix(str(r.get("node") or r.get("qualified_name") or "?"))
                label = str(r.get("label") or "")
                file = str(r.get("file") or "")
                start = r.get("start_line")
                # `codebase-memory-mcp` reports 1-based line numbers (LINE_BASES["graph"] in
                # loc.py), so a usable line here has to be a real int >= 1 — the same policy
                # loc.py:48-49 already applies for the 0-based engines. Rendering anything less
                # (0, -1, or a non-int the backend happened to send) produced `path:0`, a line
                # number that does not exist in any editor.
                usable = isinstance(start, int) and not isinstance(start, bool) and start >= 1
                loc = f"{file}:{start}" if file and usable else file
                ml = r.get("match_lines")
                ml_s = f"  (lines {', '.join(str(x) for x in ml)})" if isinstance(ml, list) and ml else ""
                badge = f" [{label}]" if label else ""
                lines.append(f"- {node}{badge} {loc}{ml_s}".rstrip())
            if not lines:
                return f'## Pattern matches for "{target}"\n(no matches)'
            return f'## Pattern matches for "{target}" ({len(lines)})\n' + "\n".join(lines)
        except Exception:
            return None

    def _op_overview(self, target: str, project: str, timeout_ms: int, root: str = "") -> str | None:
        try:
            raw = self._run("get_architecture", {"project": project}, timeout_ms)
            if not isinstance(raw, dict):
                return None
            # Title with the REPO's own name, not the backend's project id. That id is often a
            # flattened absolute path (`Users-alice-Documents-project-myrepo`), and this heading
            # lands in CODE_INTEL.md — a file that gets committed and pushed, so an internal
            # identifier there leaks the author's home directory layout into the repository.
            #
            # But naming the CALLER's directory unconditionally made the presentation layer assert
            # a provenance the data layer never established. With a project's index file removed
            # from under it, the backend answered from a different tree and this heading still read
            # `## Architecture: <the caller's repo>` — wrong numbers, confidently attributed. Claim the
            # repo's name only when the resolved project actually matched THIS root; otherwise say
            # what the answer is really about.
            answered_elsewhere = self._answered_root_mismatch(root)
            name = "" if answered_elsewhere else _repo_display_name(root)
            name = name or str(raw.get("project") or project)
            parts = [f"## Architecture: {name}"]
            if answered_elsewhere:
                parts.append(
                    "> Provenance: this answer comes from the indexed project that contains "
                    "the directory you asked about, so the counts below describe a different "
                    "tree. Index it standalone for an answer scoped to it."
                )
            tn, te = raw.get("total_nodes"), raw.get("total_edges")
            if tn is not None or te is not None:
                parts.append(f"{tn or 0} nodes, {te or 0} edges")

            def _counts(items: Any, key: str, ckey: str = "count") -> list[str]:
                if not isinstance(items, list):
                    return []
                return [f"- {it.get(key)}: {it.get(ckey)}" for it in items
                        if isinstance(it, dict) and it.get(key) is not None]

            # Route extraction is intentionally high-recall upstream and can turn arbitrary path
            # literals into synthetic ``Route`` nodes.  Daycap exposed the failure clearly: six
            # test fixture paths (``/Users/alice/...``, ``/opt/homebrew/...``) were reported as HTTP
            # routes even though none had a method or an incoming structural edge.  Do not repeat
            # an unverified aggregate as architecture fact. Count only route nodes with incoming
            # production-code evidence: either an HTTP_CALLS edge or a concrete route method. If
            # the validation query itself fails, preserve the backend aggregate rather than
            # silently subtracting unknown data.
            raw_node_items = raw.get("node_labels")
            node_items: list[Any] = raw_node_items if isinstance(raw_node_items, list) else []
            declared_routes = 0
            for item in node_items:
                if isinstance(item, dict) and item.get("label") == "Route":
                    declared_routes = _int_or_zero(item.get("count"))
                    break
            route_note = ""
            if declared_routes:
                route_query = (
                    "MATCH (source)-[edge]->(route:Route) "
                    "RETURN source.file_path, route.name, route.method, type(edge), "
                    "count(DISTINCT source) AS evidence_count LIMIT 200"
                )
                route_rows = self._query_rows(route_query, project, timeout_ms)
                if self._last_failure is None:
                    verified_route_rows = [
                        row for row in route_rows
                        if is_code_path(str(row.get("source.file_path") or ""))
                        and not self._is_noise({
                            "file_path": str(row.get("source.file_path") or ""),
                            "name": str(row.get("route.name") or row.get("name") or ""),
                        })
                        and (
                            str(row.get("type(edge)") or "") == "HTTP_CALLS"
                            or str(row.get("route.method") or "") not in {"", "-", "ANY", "None"}
                        )
                    ]
                    verified_routes = len({
                        str(row.get("route.name") or row.get("name"))
                        for row in verified_route_rows
                        if row.get("route.name") or row.get("name")
                    })
                    verified_http_calls = sum(
                        _int_or_zero(row.get("evidence_count"))
                        for row in verified_route_rows
                        if str(row.get("type(edge)") or "") == "HTTP_CALLS"
                    )
                    if verified_routes != declared_routes:
                        suppressed = max(0, declared_routes - verified_routes)
                        node_items = [
                            ({**item, "count": verified_routes}
                             if isinstance(item, dict) and item.get("label") == "Route" else item)
                            for item in node_items
                            if not (isinstance(item, dict) and item.get("label") == "Route"
                                    and verified_routes == 0)
                        ]
                        route_note = (
                            f"> Route validation: ignored {suppressed} backend route candidate(s) "
                            "without production-code structural evidence."
                        )
                        self._add_gap(
                            "routes", "unverified-routes-dropped",
                            f"{suppressed} backend route candidate(s) were excluded because they "
                            "had no production-code structural evidence",
                        )

            node_labels = _counts(node_items, "label")
            edge_items = raw.get("edge_types")
            if declared_routes and self._last_failure is None and isinstance(edge_items, list):
                edge_items = [
                    ({**item, "count": verified_http_calls}
                     if isinstance(item, dict) and item.get("type") == "HTTP_CALLS" else item)
                    for item in edge_items
                    if not (isinstance(item, dict) and item.get("type") == "HTTP_CALLS"
                            and verified_http_calls == 0)
                ]
            edge_types = _counts(edge_items, "type")
            if node_labels:
                parts.append("### Node types")
                parts.extend(node_labels)
            if route_note:
                parts.append(route_note)
            if edge_types:
                parts.append("### Edge types")
                parts.extend(edge_types)

            langs = raw.get("languages")
            if isinstance(langs, list) and langs:
                lang_lines = []
                for it in langs:
                    if isinstance(it, dict):
                        lang_lines.append("- " + ", ".join(f"{k}: {v}" for k, v in it.items()))
                    else:
                        lang_lines.append(f"- {it}")
                if lang_lines:
                    parts.append("### Languages")
                    parts.extend(lang_lines)

            if len(parts) == 1:  # nothing but the title — treat as no data
                return None
            return "\n".join(parts)
        except Exception:
            return None

    # -------------------------------------------------- repo-scan ops (no target)
    # These key on the whole index / git worktree, not a symbol — `target` is ignored. A clean/empty
    # scan is a TRUE answer ("nothing changed", "no dead code"), not a lookup miss, so they return an
    # informative string; only a backend failure returns None (→ safe-null upstream).

    def _op_changed(self, project: str, timeout_ms: int) -> str | None:
        """Impact of the working tree's UNCOMMITTED changes: changed files → impacted symbols. The
        flagship pre-edit op. detect_changes drives a backend-side reindex of the changed files, so
        it gets a higher timeout floor than a plain read."""
        try:
            raw = self._run("detect_changes", {"project": project}, max(timeout_ms, 15000))
            if not isinstance(raw, dict):
                return None
            files_raw = raw.get("changed_files")
            syms_raw = raw.get("impacted_symbols")
            # Guard against a non-detect_changes dict (e.g. a backend error object): if NEITHER key
            # is a list, this isn't a real response — degrade to safe-null, NOT a false "clean tree".
            if not isinstance(files_raw, list) and not isinstance(syms_raw, list):
                return None
            # The backend returns DUPLICATE changed_files (staged + unstaged views) — dedupe,
            # order-preserving (dogfooding showed 6 real files reported as 11).
            # …and scope to SOURCE. Dogfooding reported "4 files → 28 symbols" where the files were
            # `.gitignore`, two plan JSONs and `CODE_INTEL.md` — codeintel's OWN artifact — and all
            # 28 "impacted symbols" were markdown headings out of it. The indexer's corpus policy
            # cannot be reused here: it admits `.md` on purpose, for semantic search. `dropped`
            # remembers that non-source changes existed, so a tree full of them cannot be reported
            # as "clean" — that would trade a noisy answer for a false one.
            files, seen_f, dropped = [], set(), 0
            for f in files_raw if isinstance(files_raw, list) else []:
                if isinstance(f, str) and f not in seen_f:
                    seen_f.add(f)
                    if is_code_path(f):
                        files.append(f)
                    else:
                        dropped += 1
            # impacted_symbols interleaves real symbols with bare file/module markers whose label IS
            # its own path (name == qualified_name == file_path). Drop those structurally by comparing
            # label to file_path — this catches a root-level marker (`main.py`, no "/") AND avoids
            # dropping a real symbol whose qualified name legitimately contains "/" (e.g. Go's
            # github.com/org/pkg.Func). Files are already listed above; dedupe the rest.
            syms, seen_s = [], set()
            for s in syms_raw if isinstance(syms_raw, list) else []:
                if not isinstance(s, dict):
                    continue
                label = _label_of(s).strip()
                fp = str(s.get("file_path") or s.get("file") or "")
                if not label or label == fp:
                    continue
                # Same source scoping as the file list. A symbol whose file_path is MISSING is kept:
                # an absent path is a backend quirk, not evidence of junk, and dropping it would
                # under-report real impact — the one failure mode worse than over-reporting here.
                if fp and not is_code_path(fp):
                    dropped += 1
                    continue
                key = (label, fp)
                if key in seen_s:
                    continue
                seen_s.add(key)
                try:
                    hop = int(str(s.get("hop") or 0))
                except (TypeError, ValueError):
                    hop = 0
                syms.append((label, fp, hop))
            if not files and not syms:
                if dropped:
                    return ("## Changes impact\n(no source changes — the working tree's "
                            f"{dropped} uncommitted change(s) are all non-source files)")
                return "## Changes impact\n(working tree clean — no uncommitted changes)"
            ripple, ripple_truncated = self._changed_ripple(files, project, timeout_ms)
            walked_hdr = any(h > 0 for _, _, h in syms)
            parts = [f"## Changes impact ({len(files)} files → {len(syms)} "
                     + ("symbols impacted" if walked_hdr else "symbols defined in them")
                     + f" → {len(ripple)} callers elsewhere)"]
            if files:
                parts.append(f"### Changed files ({len(files)})")
                parts.extend(f"- {f}" for f in files[:40])
                if len(files) > 40:
                    parts.append(f"… (+{len(files) - 40} more)")
            if syms:
                # The same backend field means two different things across dialects, so the heading
                # is derived from the data rather than assumed. 0.9.x returns the symbols the edit
                # CONTAINS — everything defined in a touched file, whether or not the edit came near
                # it, which is a much weaker claim than "impacted". 0.10.x already returns a
                # transitive walk and stamps each row with a `hop`. Labelling a walk as containment
                # put symbols from three other files under "defined in the changed files".
                walked = any(h > 0 for _, _, h in syms)
                parts.append(
                    f"### Symbols the backend reports as impacted, up to {max(h for _, _, h in syms)}"
                    f" hop(s) ({len(syms)})" if walked
                    else f"### Symbols defined in the changed files ({len(syms)})")
                for label, fp, hop in syms[:40]:
                    tail = f"  ({fp})" if fp and fp != label else ""
                    hop_s = f" [hop {hop}]" if hop > 0 else ""
                    parts.append(f"- {label}{hop_s}{tail}")
                if len(syms) > 40:
                    parts.append(f"… (+{len(syms) - 40} more)")
            if ripple:
                parts.append(f"### Callers elsewhere that reach into them ({len(ripple)})")
                parts.append("_This is the blast radius: symbols outside the changed files whose "
                             "behaviour can move because of this edit._")
                for label, fp, conf, kind, evidence in ripple[:40]:
                    tail = f"  ({fp})" if fp and fp != label else ""
                    # The relationship comes first: "registered" and "calls" are different things
                    # to check, and a reviewer triages on that before they weigh confidence.
                    kind_s = f" [{kind}]" if kind != _DIRECT_KIND else ""
                    if evidence == "name-guess":
                        mark = f" [?{conf:.2f}]" if conf is not None else " [?name-guess]"
                    elif not evidence and conf is not None and conf < _EDGE_CONFIDENCE_FLOOR:
                        # No strategy reported (0.9.x, or an edge the newer backend left
                        # unlabelled), so the float is the only signal there is. Dropping this
                        # branch silently un-flagged every soft edge on the older backend.
                        mark = f" [?{conf:.2f}]"
                    elif conf is None and not evidence:
                        mark = " [unscored]"
                    else:
                        mark = ""
                    parts.append(f"- {label or fp}{kind_s}{mark}{tail}")
                if len(ripple) > 40:
                    parts.append(f"… (+{len(ripple) - 40} more)")
                guessed = sum(1 for r in ripple
                              if r[4] == "name-guess"
                              or (not r[4] and r[2] is not None and r[2] < _EDGE_CONFIDENCE_FLOOR))
                indirect = sum(1 for r in ripple if r[3] != _DIRECT_KIND)
                notes = []
                if indirect:
                    notes.append(f"{indirect} of {len(ripple)} reach this code without calling it "
                                 f"(registered as a callback, or referenced) — they can still break")
                if guessed:
                    notes.append(f"{guessed} were resolved by name matching and may not reach this "
                                 f"code at all")
                if notes:
                    parts.append("\n_" + "; ".join(notes) + "._")
            elif files:
                # An empty ripple is a real and useful answer — but only if it cannot be confused
                # with one that was never computed.
                parts.append("### Callers elsewhere that reach into them (0)")
                parts.append("_No symbol outside the changed files calls into them, by the graph's "
                             "CALLS edges. Framework dispatch and calls through a value are not "
                             "edges, so this is not proof that nothing else is affected._")
            if ripple_truncated:
                self._add_gap(
                    "changed", "ripple-truncated",
                    "the downstream caller list hit its own cap, so the blast radius shown is a "
                    "lower bound, not the whole of it",
                )
                parts.append("\n_Downstream list truncated at its cap — this is a lower bound._")
            return "\n".join(parts)
        except Exception:
            return None

    _RIPPLE_FILE_CAP = 40

    _RIPPLE_ROW_CAP = 60

    def _changed_ripple(
        self, files: list[str], project: str, timeout_ms: int
    ) -> tuple[list[tuple[str, str, float | None, str, str]], bool]:
        """Symbols OUTSIDE the changed files that call into them — the actual blast radius.

        `changed` used to stop at containment and call it impact. Editing one function in
        `src/domain/budget.ts` reported "1 file -> 7 symbols", and all seven were the symbols DEFINED
        in that file; `runAlerts`, which calls into it from another file and is the one thing a
        reviewer needed to look at, appeared nowhere. The tool's own instructions promise the symbols
        an edit "ripples into", so the op was answering a different question than the one it
        advertised — and the containment answer is the one an agent is least likely to notice is
        wrong, because it is never empty.

        Deliberately NOT filtered by the confidence floor. A pre-commit checklist errs toward
        over-inclusion: `runAlerts -> evaluate` is a genuine ripple edge stamped 0.75, so a floor
        here would drop the very row that motivated the fix. Instead every caller is collapsed to its
        best-scored edge and carries that score, so a name-collision flood (every test file in the
        repo, reached through a framework global) is visible as the low-confidence block it is rather
        than swamping the list.
        """
        if not files:
            return [], False
        listed = files[: self._RIPPLE_FILE_CAP]
        in_list = ", ".join(f'"{_cypher_literal(f)}"' for f in listed)
        # CALLS is not the whole blast radius. A function REGISTERED somewhere
        # (`set_forward_fn(app.forward_released_item)`) breaks just as thoroughly when its signature
        # moves, and the edge recording that is CALL_REFERENCE. USAGE is in for the same reason:
        # this op answers a recall question — "what should I look at before committing" — and the
        # asymmetry matters. Under-reporting impact is how live code gets broken; over-reporting it
        # costs a reader one line, and every row says which kind it is.
        cypher = (
            f"MATCH (a)-[c:CALLS|CALL_REFERENCE|USAGE]->(b) WHERE b.file_path IN [{in_list}] "
            f"AND NOT a.file_path IN [{in_list}] "
            "RETURN a.qualified_name, a.file_path, c.confidence, type(c) AS kind, "
            "c.strategy AS strategy "
            f"LIMIT {self._RIPPLE_ROW_CAP}"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        truncated = len(rows) >= self._RIPPLE_ROW_CAP or len(files) > self._RIPPLE_FILE_CAP
        # One entry per calling symbol, keeping its best-scored edge: a caller that reaches three
        # changed symbols is one thing to review, not three.
        # One row per calling symbol: a caller that reaches three changed symbols is one thing to
        # review, not three. Where the same caller has several kinds of edge, the STRONGEST claim
        # wins — a direct call outranks a registration, which outranks a bare reference — because
        # that is the one a reviewer needs to see first.
        rank = {_DIRECT_KIND: 0, "CALL_REFERENCE": 1, "USAGE": 2}
        best: dict[tuple[str, str], tuple[int, float | None, str, str]] = {}
        for r in rows:
            label = _strip_project_prefix(
                str(r.get("a.qualified_name") or ""), may_be_filename=False)
            fp = str(r.get("a.file_path") or "")
            if not label and not fp:
                continue
            kind = str(r.get("kind") or "").strip() or _DIRECT_KIND
            evidence = _evidence_class(str(r.get("strategy") or ""))
            cand = (rank.get(kind, 3), _edge_confidence(r), kind, evidence)
            key = (label, fp)
            prev = best.get(key)
            if prev is None or (cand[0], -(cand[1] or -1.0)) < (prev[0], -(prev[1] or -1.0)):
                best[key] = cand
        out = [(lbl, fp, c[1], c[2], c[3]) for (lbl, fp), c in best.items()]
        # Strongest kind first, then best-resolved within it.
        out.sort(key=lambda t: (rank.get(t[3], 3), -(t[2] if t[2] is not None else -1.0), t[0]))
        return out, truncated

    def _op_hotspots(self, project: str, timeout_ms: int) -> str | None:
        """Highest complexity / fan-in symbols (refactor-risk hotspots). search_graph returns rows
        UNSORTED (name order) and caps at ``limit``, so we over-request then sort CLIENT-SIDE by
        (complexity, in_degree). Tests/builtins filtered out.

        Two things were wrong with the request itself, and together they made this op report the
        UI layer of every repo it was pointed at:

        - It asked for ``label: "Function"`` only. A class method is a ``Method`` node, so every
          Python method and every TypeScript class method was invisible to it — on one evaluated
          repo that hid 2,381 symbols behind 1,343 that were considered.
        - It capped at 200 rows and then sorted those. The backend returns rows in NAME order, so
          that is an arbitrary alphabetical slice, not the 200 most complex — the client-side sort
          could only ever rank what the truncation happened to admit. On a 4,883-function repo the
          sample was 4% of the candidates and the "top hotspots" were the top of that 4%.
        """
        try:
            rows: list[dict] = []
            saw_any = False
            for label in ("Function", "Method"):
                got = self._search_symbols(
                    # `fields` asks 0.10.x for the per-node metrics this op RANKS on. They are core
                    # columns in 0.9.x and optional ones in 0.10.x, where omitting them yields rows
                    # whose complexity is uniformly zero — a hotspots list sorted by nothing.
                    # 0.9.x ignores the key, so one payload serves both.
                    {"label": label, "min_degree": 1, "limit": 2000,
                     "fields": ["complexity", "cognitive", "is_test"]},
                    project, timeout_ms,
                )
                if got is None:
                    continue
                saw_any = True
                rows.extend(got)
            if not saw_any:
                return None
            kept = [r for r in rows if not self._is_noise(r)]
            if not kept:
                return "## Complexity / fan-in hotspots\n(none found)"
            kept.sort(key=lambda r: (r.get("complexity") or 0, r.get("in_degree") or 0), reverse=True)
            coverage = _language_coverage_note(kept[:30])

            def _meta(r: dict) -> list[str]:
                m = [f"in:{r.get('in_degree') or 0} out:{r.get('out_degree') or 0}",
                     f"cx:{r.get('complexity') or 0} cog:{r.get('cognitive') or 0}"]
                if r.get("lines") is not None:
                    m.append(f"{r.get('lines')} lines")
                return m

            return self._render_scan(kept, "Complexity / fan-in hotspots", 25, _meta) + coverage
        except Exception:
            return None
