"""Build an interactive view of a project's call graph from the graph engine.

Headless-first, in board-differ's data→renderer spirit: `build_graph_payload` returns a plain
``{project, nodes, edges}`` dict (the machine-readable `--format json` shape); `render_html` wraps
that payload in the self-contained interactive viewer (``viewer/graph_template.html``). The engine
produces the *data*; the template is the *renderer*. Both never raise.
"""
from __future__ import annotations

import json
import os
from typing import Any

from codeintel.provider import log_swallowed

_EMPTY = {"project": "", "engine": "graph", "op": "callgraph", "nodes": [], "edges": []}


def build_graph_payload(project_root: Any, *, limit: int = 220, timeout_ms: int = 8000) -> dict:
    """Query the graph engine for a project's internal call graph → ``{project, nodes, edges}``.

    Nodes are the symbols that participate in an internal call edge, enriched with the same
    complexity metrics the ``hotspots`` op surfaces; edges are ``CALLS``/``USAGE`` relations.
    Builtins/synthetic nodes are excluded. Returns an empty payload with a ``reason`` on any
    failure — never raises (mirrors the safe-null contract)."""
    try:
        try:
            limit = max(1, min(2000, int(limit)))
        except Exception:
            limit = 220
        try:
            timeout_ms = int(timeout_ms)
        except Exception:
            timeout_ms = 8000
        from codeintel.providers.graph import (
            GraphProvider,
            _repo_display_name,
            _strip_project_prefix,
        )
        p = GraphProvider()
        if not getattr(p, "available", False):
            return {**_EMPTY, "reason": "engine-unavailable"}
        resolution = p._resolve_project(str(project_root or ""))
        if resolution is None:
            return {**_EMPTY, "reason": "project-not-indexed"}
        # A rendered graph is a whole-repo artifact, so an ancestor match would draw the containing
        # project's call graph under this repo's name — and this one gets exported to HTML/SVG and
        # shared. Refuse, like `map` and the repo-scan ops.
        if resolution.is_ancestor:
            return {**_EMPTY, "reason": "project-not-indexed-standalone"}
        project = resolution.name

        # per-symbol metrics (same search_graph the `hotspots` op uses)
        metrics: dict[str, dict] = {}
        for r in (p._search_symbols({"label": "Function", "min_degree": 1, "limit": 600}, project, timeout_ms) or []):
            qn = _strip_project_prefix(str(r.get("qualified_name") or ""))
            if qn:
                metrics[qn] = r

        # internal call edges — fetch generously and filter builtins/synthetic ('<...>') nodes
        # CLIENT-SIDE (robust across Cypher dialects; mirrors how the hotspots op drops them).
        cypher = (
            "MATCH (a)-[c:CALLS|USAGE]->(b) "
            "RETURN a.qualified_name, a.name, a.file_path, b.qualified_name, b.name, b.file_path, type(c) "
            "LIMIT " + str(min(max(limit * 3, 400), 1500))
        )

        def _synth(fp: str) -> bool:
            return (not fp) or fp.startswith("<")

        nodes_by_id: dict[str, dict] = {}
        edges: list[dict] = []
        seen: set = set()

        def _add(qn: str, name: str, fp: str) -> None:
            if qn and qn not in nodes_by_id:
                m = metrics.get(qn, {})
                nodes_by_id[qn] = {
                    "id": qn, "label": name or qn.rsplit(".", 1)[-1], "file": str(fp or ""),
                    "complexity": m.get("complexity") or 0, "cognitive": m.get("cognitive") or 0,
                    "in_degree": m.get("in_degree") or 0, "out_degree": m.get("out_degree") or 0,
                    "lines": m.get("lines") or 0,
                }

        for e in (p._query_rows(cypher, project, timeout_ms) or []):
            if len(edges) >= limit:
                break
            # Strip here, not only on the metrics side: node ids feed the JSON payload, and 646
            # of them carried `Users-<user>-Documents-...` on one repo. They must also match the
            # already-stripped `metrics` keys, or every node loses its complexity numbers.
            aq = _strip_project_prefix(str(e.get("a.qualified_name") or ""))
            bq = _strip_project_prefix(str(e.get("b.qualified_name") or ""))
            af, bf = str(e.get("a.file_path") or ""), str(e.get("b.file_path") or "")
            if not aq or not bq or aq == bq or _synth(af) or _synth(bf):
                continue
            _add(aq, str(e.get("a.name") or ""), af)
            _add(bq, str(e.get("b.name") or ""), bf)
            if (aq, bq) not in seen:
                seen.add((aq, bq))
                edges.append({"from": aq, "to": bq, "type": str(e.get("type(c)") or "")})

        nodes = list(nodes_by_id.values())
        # The payload names the REPO, not the backend's internal project id — which for a
        # path-slug registration is the author's flattened home directory, written into a file
        # people open in a browser and share.
        return {
            "project": _repo_display_name(str(project_root or "")) or project,
            "engine": "graph", "op": "callgraph", "nodes": nodes, "edges": edges,
        }
    except Exception as exc:
        log_swallowed("grapher.build_graph_payload", exc)
        return {**_EMPTY, "reason": "error"}


def _template_path() -> str:
    return os.path.join(os.path.dirname(__file__), "viewer", "graph_template.html")


def render_html(payload: dict) -> str:
    """Wrap a graph payload in the self-contained interactive viewer template. Never raises."""
    try:
        # default=str → serialization can't raise on an unexpected value; "</" escape keeps the
        # JSON inside the <script> element (the template's viewer JS re-emits every string via
        # textContent, so there is no innerHTML/XSS sink downstream).
        data = json.dumps(payload, default=str).replace("</", "<\\/")
        with open(_template_path(), encoding="utf-8") as f:
            return f.read().replace("__DATA__", data)
    except Exception as exc:
        log_swallowed("grapher.render_html", exc)
        try:
            body = json.dumps(payload, indent=2, default=str).replace("&", "&amp;").replace("<", "&lt;")
        except Exception:
            body = "(graph payload unavailable)"
        return ("<!doctype html><meta charset=utf-8><title>codeintel graph</title>"
                "<pre style='font:13px ui-monospace,monospace;padding:16px'>" + body + "</pre>")
