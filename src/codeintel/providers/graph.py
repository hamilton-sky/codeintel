from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Optional

from codeintel.provider import Result, safe_null_result


def _cypher_literal(s: Any) -> str:
    """Escape a value for a double-quoted Cypher string literal — defense against a
    ``target`` containing quotes/backslashes (e.g. content an agent echoed from a repo)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


class GraphProvider:
    """Wraps the codebase-memory-mcp CLI. Never raises.

    Backend contract (verified against codebase-memory-mcp 0.9.0 by dogfooding, not assumed):
      * ``list_projects``  → ``{"projects": [{name, root_path, ...}]}``
      * ``query_graph``    → ``{"columns": [...], "rows": [[...], ...], "total": N}``  — rows are
                             value-arrays aligned to ``columns``, NOT a list of dicts.
      * ``trace_path``     → ``{function, callees: [{name, qualified_name, hop}], callers: [...]}``
                             or ``{"status": "ambiguous", "suggestions": [...]}``.
      * ``search_code``    → ``{"results": [{node, qualified_name, label, file, match_lines}]}``.
      * ``get_architecture`` → ``{project, total_nodes, total_edges, node_labels, edge_types, languages}``.

    Call graph: module-level function calls are recorded as ``USAGE`` edges from the calling
    ``Module`` node; method/function-to-method calls are ``CALLS`` edges. "Who calls X" therefore
    needs BOTH edge types (``[:CALLS|USAGE]``) — ``CALLS`` alone misses every module-level callee
    (that is why the old ``(caller)-[:CALLS]->(fn)`` query returned zero rows for real symbols).
    """

    def __init__(self) -> None:
        self._project_cache: dict[str, Optional[str]] = {}
        self._detect_backend()

    def _detect_backend(self) -> None:
        path = shutil.which("codebase-memory-mcp")
        if path:
            self.available = True
            self._cmd: Optional[str] = path
        else:
            self.available = False
            self._cmd = None

    # Sentinel: distinguishes "the subprocess call failed" from "it succeeded and returned JSON
    # null". Overloading None for both would make a legit null result wrongly trigger the fallback.
    _FAIL = object()

    def _run(self, method: str, payload: dict, timeout_ms: int) -> Optional[Any]:
        # Prefer PIPED STDIN — the stable, non-deprecated form the backend documents
        # (`echo '<json>' | codebase-memory-mcp cli <method>`; no deprecation warning). Fall back
        # to the deprecated raw-JSON positional arg for one release so an older backend still
        # works. The two attempts SHARE one deadline (the caller's timeout_ms) so total wall time
        # can't double. Never raises. `_run` stays the single seam existing tests patch.
        body = json.dumps(payload)
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
        out = self._run_stdin(method, body, timeout_ms)
        if out is not self._FAIL:
            return out  # success (including a legit null) → no fallback
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return None
        out = self._run_rawjson(method, body, remaining_ms)
        return None if out is self._FAIL else out

    def _run_stdin(self, method: str, body: str, timeout_ms: int) -> Any:
        try:
            result = subprocess.run(
                [self._cmd, "cli", method],
                input=body.encode(),
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            if result.returncode != 0:
                return self._FAIL  # unsupported / error → let the raw-JSON fallback try
            return json.loads(result.stdout)
        except Exception:
            return self._FAIL

    def _run_rawjson(self, method: str, body: str, timeout_ms: int) -> Any:
        # Deprecated-but-working bridge for older backends; remove once the live stdin test
        # (tests/test_graph_stdin.py::test_live_stdin_list_projects) is green in CI.
        try:
            result = subprocess.run(
                [self._cmd, "cli", method, body],
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            if result.returncode != 0:
                return self._FAIL
            return json.loads(result.stdout)
        except Exception:
            return self._FAIL

    @staticmethod
    def _match_project(raw: Any, project_root: str) -> Optional[str]:
        """Resolve a list_projects response to the project name for ``project_root``.

        The real codebase-memory-mcp returns ``{"projects": [...]}``; a bare list is the
        older/mocked shape — accept both. Prefer an exact ``root_path`` match; otherwise the
        LONGEST prefix match (so ``.../project/codeintel`` resolves to codeintel, not its
        parent ``.../project``). Pure + static so ``_resolve_project`` and ``probe`` share it."""
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return None
        exact: Optional[str] = None
        best_prefix_len = -1
        best_prefix_name: Optional[str] = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rp = entry.get("root_path", "")
            if not rp:
                continue
            if rp == project_root:
                exact = entry.get("name")
                break
            if project_root.startswith(rp.rstrip("/") + "/") and len(rp) > best_prefix_len:
                best_prefix_len = len(rp)
                best_prefix_name = entry.get("name")
        return exact if exact is not None else best_prefix_name

    def _resolve_project(self, project_root: str) -> Optional[str]:
        if project_root in self._project_cache:
            return self._project_cache[project_root]
        raw = self._run("list_projects", {}, 3000)
        name = self._match_project(raw, project_root)
        self._project_cache[project_root] = name
        return name

    def probe(self, project_root: str, timeout_ms: int = 3000) -> dict:
        """Cheap, never-raise, single-subprocess health check for the doctor.

        Returns ``{installed, runnable, repo_indexed, project, detail, remediation}`` — one
        ``list_projects`` call, bounded by ``timeout_ms`` (``_run`` returns None on timeout)."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": False, "project": None,
                "detail": "codebase-memory-mcp not found on PATH",
                "remediation": "install codebase-memory-mcp (the graph backend)",
            }
        raw = self._run("list_projects", {}, timeout_ms)
        if raw is None:
            return {
                "installed": True, "runnable": False, "repo_indexed": False, "project": None,
                "detail": "codebase-memory-mcp is installed but list_projects failed/timed out",
                "remediation": "check `codebase-memory-mcp cli list_projects '{}'` works",
            }
        project = self._match_project(raw, project_root)
        if project is None:
            return {
                "installed": True, "runnable": True, "repo_indexed": False, "project": None,
                "detail": "backend OK but this repo is not indexed in the graph",
                "remediation": f"codeintel index {project_root}",
            }
        return {
            "installed": True, "runnable": True, "repo_indexed": True, "project": project,
            "detail": f"resolved project '{project}' in codebase-memory-mcp",
            "remediation": None,
        }

    # ------------------------------------------------------------------ helpers

    def _query_rows(self, cypher: str, project: str, timeout_ms: int) -> list[dict]:
        """Run a Cypher query and return rows as column→value dicts.

        The real backend returns ``{"columns": [...], "rows": [[v, ...], ...]}`` where each row is
        a value-array aligned to ``columns``. Tolerates the legacy/mocked list-of-dicts shape and
        any malformed response by returning ``[]`` (never raises)."""
        raw = self._run("query_graph", {"project": project, "query": cypher}, timeout_ms)
        if isinstance(raw, list):
            # Legacy/mocked shape: already a list of dicts.
            return [r for r in raw if isinstance(r, dict)]
        if not isinstance(raw, dict):
            return []
        cols = raw.get("columns")
        rows = raw.get("rows")
        if not isinstance(cols, list) or not isinstance(rows, list):
            return []
        out: list[dict] = []
        for row in rows:
            if isinstance(row, list):
                out.append({str(cols[i]): row[i] for i in range(min(len(cols), len(row)))})
            elif isinstance(row, dict):
                out.append(row)
        return out

    @staticmethod
    def _display(row: dict, name_key: str, qn_key: str, file_key: str) -> str:
        name = str(row.get(name_key) or "?")
        qn = str(row.get(qn_key) or "")
        file = str(row.get(file_key) or "")
        edge = str(row.get("type(c)") or "").strip()
        label = qn or name
        tail = f" ({file})" if file and file != qn else ""
        badge = f" [{edge}]" if edge else ""
        return f"- {label}{badge}{tail}"

    # ------------------------------------------------------------------ ops

    def _op_callers(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE b.name="{_cypher_literal(target)}" '
            "RETURN a.name, a.qualified_name, a.file_path, type(c) LIMIT 50"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        lines = [self._display(r, "a.name", "a.qualified_name", "a.file_path") for r in rows]
        return f"## Callers of {target} ({len(lines)})\n" + "\n".join(lines)

    def _op_callees(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE a.name="{_cypher_literal(target)}" '
            "RETURN b.name, b.qualified_name, b.file_path, type(c) LIMIT 50"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        lines = [self._display(r, "b.name", "b.qualified_name", "b.file_path") for r in rows]
        return f"## Callees of {target} ({len(lines)})\n" + "\n".join(lines)

    def _op_impact(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        callers = self._op_callers(target, project, timeout_ms)
        callees = self._op_callees(target, project, timeout_ms)
        if callers is None and callees is None:
            return None
        # callers/callees already carry their own "## Callers of X (N)" header — don't wrap them
        # in a second "### Callers" header (that produced a redundant double heading).
        parts = [f"## Impact of {target}"]
        parts.append(callers or f"## Callers of {target} (0)\n(none found)")
        parts.append(callees or f"## Callees of {target} (0)\n(none found)")
        return "\n".join(parts)

    def _op_chain(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        # Accept an "A->B" form (trace from the source symbol) or a bare symbol.
        src = target.split("->")[0].strip() if "->" in target else target.strip()
        if not src:
            return None
        raw = self._run(
            "trace_path",
            {"project": project, "function_name": src, "mode": "calls"},
            timeout_ms,
        )
        if not isinstance(raw, dict):
            return None
        if raw.get("status") == "ambiguous":
            sugg = raw.get("suggestions") or []
            names = [str(s.get("qualified_name") or s.get("name") or "?") for s in sugg if isinstance(s, dict)]
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
                    qn = str(it.get("qualified_name") or "")
                    hop = it.get("hop")
                    label = qn or nm
                    hop_s = f" [hop {hop}]" if hop is not None else ""
                    out.append(f"- {label}{hop_s}")
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

    def _op_pattern(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        try:
            raw = self._run("search_code", {"project": project, "pattern": target}, timeout_ms)
            results = raw.get("results") if isinstance(raw, dict) else raw
            if not isinstance(results, list) or not results:
                return f'## Pattern matches for "{target}"\n(no matches)'
            lines = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                node = str(r.get("node") or r.get("qualified_name") or "?")
                label = str(r.get("label") or "")
                file = str(r.get("file") or "")
                start = r.get("start_line")
                loc = f"{file}:{start}" if file and start is not None else file
                ml = r.get("match_lines")
                ml_s = f"  (lines {', '.join(str(x) for x in ml)})" if isinstance(ml, list) and ml else ""
                badge = f" [{label}]" if label else ""
                lines.append(f"- {node}{badge} {loc}{ml_s}".rstrip())
            if not lines:
                return f'## Pattern matches for "{target}"\n(no matches)'
            return f'## Pattern matches for "{target}" ({len(lines)})\n' + "\n".join(lines)
        except Exception:
            return None

    def _op_overview(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        try:
            raw = self._run("get_architecture", {"project": project}, timeout_ms)
            if not isinstance(raw, dict):
                return None
            name = str(raw.get("project") or project)
            parts = [f"## Architecture: {name}"]
            tn, te = raw.get("total_nodes"), raw.get("total_edges")
            if tn is not None or te is not None:
                parts.append(f"{tn or 0} nodes, {te or 0} edges")

            def _counts(items: Any, key: str, ckey: str = "count") -> list[str]:
                out = []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get(key) is not None:
                            out.append(f"- {it.get(key)}: {it.get(ckey)}")
                return out

            node_labels = _counts(raw.get("node_labels"), "label")
            edge_types = _counts(raw.get("edge_types"), "type")
            if node_labels:
                parts.append("### Node types")
                parts.extend(node_labels)
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

    def build_result(
        self,
        op: Any,
        target: Any,
        files: Any,
        budget: Any,
        project_root: Any,
    ) -> Result:
        try:
            op_str = str(op or "")
            target_str = str(target or "")
            root_str = str(project_root or "")

            if not self.available:
                return safe_null_result(op_str, target_str, engine="graph", reason="engine-unavailable")

            try:
                budget_ms = int(budget) if budget else 0
            except Exception:
                budget_ms = 0
            timeout_ms = budget_ms if budget_ms > 0 else 5000

            project = self._resolve_project(root_str)
            if project is None:
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="project-not-indexed",
                    hint=f"run: codeintel index {root_str}  (or: codeintel doctor)",
                )

            result_text = self._dispatch(op_str, target_str, project, timeout_ms)
            if result_text is None:
                return safe_null_result(op_str, target_str, engine="graph", reason="unsupported-op")

            return {
                "ok": True,
                "op": op_str,
                "target": target_str,
                "result": result_text,
                "engine": "graph",
                "cached": False,
            }
        except Exception:
            return safe_null_result(op, target, engine="graph", reason="error")

    def _dispatch(
        self, op: str, target: str, project: str, timeout_ms: int
    ) -> Optional[str]:
        if op == "impact" or op == "context":
            # `context` (fan-out op) → the graph's richest single-symbol view: callers + callees.
            return self._op_impact(target, project, timeout_ms)
        if op == "callers":
            return self._op_callers(target, project, timeout_ms)
        if op == "callees":
            return self._op_callees(target, project, timeout_ms)
        if op == "chain":
            return self._op_chain(target, project, timeout_ms)
        if op == "pattern":
            return self._op_pattern(target, project, timeout_ms)
        if op == "overview":
            return self._op_overview(target, project, timeout_ms)
        return None
