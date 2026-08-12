from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Optional

from codeintel.provider import Result, safe_null_result


def _cypher_literal(s: Any) -> str:
    """Escape a value for a double-quoted Cypher string literal — defense against a
    ``target`` containing quotes/backslashes (e.g. content an agent echoed from a repo)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


class GraphProvider:
    """Wraps the codebase-memory-mcp CLI. Never raises."""

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

    def _run(self, method: str, payload: dict, timeout_ms: int) -> Optional[Any]:
        try:
            result = subprocess.run(
                [self._cmd, "cli", method, json.dumps(payload)],
                capture_output=True,
                timeout=timeout_ms / 1000,
            )
            return json.loads(result.stdout)
        except Exception:
            return None

    def _resolve_project(self, project_root: str) -> Optional[str]:
        if project_root in self._project_cache:
            return self._project_cache[project_root]

        raw = self._run("list_projects", {}, 3000)
        # The real codebase-memory-mcp returns {"projects": [...]}; a bare list is the
        # older/mocked shape. Accept both so a backend contract change can't silently
        # make every graph query report "project-not-indexed".
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        name: Optional[str] = None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                rp = entry.get("root_path", "")
                if rp == project_root or (rp and project_root.startswith(rp)):
                    name = entry.get("name")
                    break
        self._project_cache[project_root] = name
        return name

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
                return safe_null_result(op_str, target_str, engine="graph", reason="project-not-indexed")

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

    def _op_callers(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        cypher = (
            f'MATCH (caller)-[:CALLS]->(fn) WHERE fn.name="{_cypher_literal(target)}" '
            "RETURN caller.name, caller.file_path LIMIT 20"
        )
        raw = self._run("query_graph", {"project": project, "query": cypher}, timeout_ms)
        if not isinstance(raw, list) or not raw:
            return None
        lines = [f"- {row.get('caller.name', '?')} ({row.get('caller.file_path', '?')})" for row in raw]
        return f"## Callers of {target}\n" + "\n".join(lines)

    def _op_callees(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        cypher = (
            f'MATCH (fn)-[:CALLS]->(callee) WHERE fn.name="{_cypher_literal(target)}" '
            "RETURN callee.name, callee.file_path LIMIT 20"
        )
        raw = self._run("query_graph", {"project": project, "query": cypher}, timeout_ms)
        if not isinstance(raw, list) or not raw:
            return None
        lines = [f"- {row.get('callee.name', '?')} ({row.get('callee.file_path', '?')})" for row in raw]
        return f"## Callees of {target}\n" + "\n".join(lines)

    def _op_impact(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        callers = self._op_callers(target, project, timeout_ms)
        callees = self._op_callees(target, project, timeout_ms)
        if callers is None and callees is None:
            return None
        parts = [f"## Impact of {target}"]
        parts.append("### Callers")
        parts.append(callers or "(none found)")
        parts.append("### Callees")
        parts.append(callees or "(none found)")
        return "\n".join(parts)

    def _op_chain(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        if "->" in target:
            src = target.split("->")[0].strip()
            raw = self._run(
                "trace_path",
                {"project": project, "function_name": src, "mode": "calls"},
                timeout_ms,
            )
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw
            return json.dumps(raw)
        return self._op_impact(target, project, timeout_ms)

    def _op_pattern(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        try:
            raw = self._run("search_code", {"project": project, "pattern": target}, timeout_ms)
            if raw is None:
                text = "(no matches)"
            elif isinstance(raw, str):
                text = raw.strip() or "(no matches)"
            else:
                text = json.dumps(raw, indent=2) if raw else "(no matches)"
            return f'## Pattern matches for "{target}"\n{text}'
        except Exception:
            return None

    def _op_overview(self, target: str, project: str, timeout_ms: int) -> Optional[str]:
        try:
            raw = self._run("get_architecture", {"project": project}, timeout_ms)
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw
            return json.dumps(raw, indent=2)
        except Exception:
            return None

    def _dispatch(
        self, op: str, target: str, project: str, timeout_ms: int
    ) -> Optional[str]:
        if op == "impact":
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
