from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeintel.providers.graph import GraphProvider

logger = logging.getLogger(__name__)

# A map with any of these sections carries real, indexed orientation content. Anything without
# them is a degraded stub: `_minimal_map` (graph unavailable / repo unindexed / generation failed),
# or a render over an empty graph. Note the ranked marker includes "(by caller count)" — the
# empty-graph render emits a differently-worded "## Ranked Symbols" heading that is NOT this.
_POPULATED_MARKERS = (
    "## Architecture Overview",
    "## Ranked Symbols (by caller count)",
    "## Entry Points",
)


def _is_populated_map(text: str) -> bool:
    """True when the map has real content (architecture overview, a ranked-symbol table, or entry
    points) rather than a degraded stub — used to avoid overwriting a good CODE_INTEL.md."""
    return any(marker in text for marker in _POPULATED_MARKERS)

# Real codebase-memory-mcp contract (verified live): query_graph returns {columns, rows}
# value-arrays, module-level calls are USAGE (not only CALLS), and nodes carry `is_entry_point`
# (there is no `fn.type` property — that filter matched nothing, so the old queries were dead).
_FAN_IN_CYPHER = (
    "MATCH (caller)-[:CALLS|USAGE]->(fn) WHERE fn.name IS NOT NULL "
    "RETURN fn.name, fn.qualified_name, fn.file_path, count(caller) AS in_degree "
    "ORDER BY in_degree DESC LIMIT 40"
)

_ENTRY_CYPHER = (
    "MATCH (fn) WHERE fn.is_entry_point = true "
    "RETURN fn.name, fn.file_path LIMIT 10"
)

# Builtins and the synthetic project/config node are high-fan-in but useless as "symbols".
_RANK_SKIP_PATHS = frozenset({"<python-builtins>", "pyproject.toml", ""})


def _rows_from(raw: object) -> list[dict]:
    """Normalize a query_graph response to a list of column->value dicts.

    Accepts the real ``{"columns": [...], "rows": [[...]]}`` shape AND the legacy list-of-dicts
    shape (used by older mocks); anything else yields ``[]``. Mirrors GraphProvider._query_rows
    so the map generator reads the same reality every other graph op now does."""
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if not isinstance(raw, dict):
        return []
    cols, rows = raw.get("columns"), raw.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, list):
            out.append({str(cols[i]): row[i] for i in range(min(len(cols), len(row)))})
        elif isinstance(row, dict):
            out.append(row)
    return out


class MapGenerator:
    """Generates a ranked, byte-bounded CODE_INTEL.md from the graph index."""

    def __init__(self, provider: GraphProvider | None = None) -> None:
        self._provider = provider

    def generate(self, project_root: str, budget_bytes: int = 32768) -> str:
        provider = self._provider
        if not provider or not getattr(provider, "available", False):
            return _minimal_map(project_root, note="graph engine not available — install "
                                                   "codebase-memory-mcp and run `codeintel index`")

        # Top-level guard: the map is best-effort orientation — a malformed backend
        # response must degrade to a minimal map, never crash the caller (never-raise).
        try:
            resolution = provider._resolve_project(project_root)
            if resolution is None:
                return _minimal_map(project_root, note="project not yet indexed — run `codeintel index` first")

            # An ancestor match is refused here UNCONDITIONALLY, unlike the query path which still
            # answers symbol-scoped ops with a caveat. Everything a map contains — node/edge counts,
            # ranked symbols, entry points — is defined by the repo boundary, and this output gets
            # WRITTEN to CODE_INTEL.md and committed. Describing a sibling repository's architecture
            # in a file that lives in this one is the worst outcome in the codebase: it is wrong,
            # persistent, reviewed by people who trust it, and pushed.
            if resolution.is_ancestor:
                return _minimal_map(
                    project_root,
                    note=(f"`{project_root}` is not indexed on its own — it resolves to the indexed "
                          f"project that contains it, whose architecture is not this repo's. "
                          f"Run `codeintel index {project_root}` and re-run `codeintel map`."),
                )
            project = resolution.name

            # Query 1: architecture overview
            arch_result = provider.build_result("overview", "", [], 5000, project_root)
            arch_text: str | None = None
            if arch_result and arch_result.get("ok") and arch_result.get("result"):
                arch_text = str(arch_result["result"])

            # Query 2: ranked symbols by fan-in
            ranked_rows = _query_ranked_symbols(provider, project)

            # Query 3: entry points
            entry_rows = _query_entry_points(provider, project)

            content = _render(project_root, arch_text, ranked_rows, entry_rows)
            content = _enforce_budget(content, budget_bytes, project_root, arch_text, ranked_rows, entry_rows)
            return content
        except Exception as exc:
            return _minimal_map(project_root, note=f"map generation failed: {exc}")

    def write(self, project_root: str, content: str) -> tuple[str, bool]:
        """Write CODE_INTEL.md; return ``(path, wrote)``. ``wrote`` is False when an existing
        populated map was preserved rather than clobbered by a stub (or the write failed), so
        callers can report the outcome honestly.

        Don't clobber a good map with a degraded stub: `generate` returns a stub when the graph
        backend is unavailable or hasn't indexed this repo yet — a common transient during
        `codeintel index`'s best-effort refresh. Overwriting a previously-populated CODE_INTEL.md
        with that stub silently destroys real orientation content, so when the new content is a
        stub and an existing map is populated, preserve the existing file."""
        path = os.path.join(project_root, "CODE_INTEL.md")
        if not _is_populated_map(content):
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as f:
                        existing = f.read()
                    if _is_populated_map(existing):
                        logger.warning(
                            "CODE_INTEL.md: new map is a stub (graph empty/unavailable) — keeping "
                            "the existing populated map at %s (run `codeintel index` to refresh)",
                            path,
                        )
                        return path, False
            except Exception:
                pass  # can't read the existing file — fall through and write (best-effort)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return path, True
        except Exception as exc:
            logger.warning("CODE_INTEL.md write failed: %s", exc)
            return path, False


def _query_ranked_symbols(provider: GraphProvider, project: str) -> list[dict]:
    raw = provider._run("query_graph", {"project": project, "query": _FAN_IN_CYPHER}, 8000)
    rows = []
    for row in _rows_from(raw):
        name = row.get("fn.name") or row.get("name") or "?"
        path = str(row.get("fn.file_path") or row.get("file_path") or "")
        deg = row.get("in_degree", 0)
        if path in _RANK_SKIP_PATHS:  # drop builtins / project node noise
            continue
        # `hotspots` was specifically hardened against archived and generated content after a
        # minified bundle took its top two slots; this ranking is its sibling and had none of that
        # filtering — three skip paths and nothing else. It is also the one that gets WRITTEN to
        # CODE_INTEL.md and committed, so a webpack chunk listed as the repo's most load-bearing
        # symbol is a wrong answer that survives in the tree and gets reviewed as fact.
        # Imported here rather than at module scope: `GraphProvider` is a TYPE_CHECKING-only import
        # above, and the mapper is constructed with a provider instance rather than importing one.
        from codeintel.providers.graph import GraphProvider as _GP
        if _GP._is_noise({"file_path": path, "name": str(name)}):
            continue
        rows.append({"name": name, "file_path": path, "in_degree": deg})
        if len(rows) >= 30:
            break
    return rows


def _query_entry_points(provider: GraphProvider, project: str) -> list[dict]:
    raw = provider._run("query_graph", {"project": project, "query": _ENTRY_CYPHER}, 5000)
    rows = []
    for row in _rows_from(raw):
        name = row.get("fn.name") or row.get("name") or "?"
        path = str(row.get("fn.file_path") or row.get("file_path") or "")
        rows.append({"name": name, "file_path": path})
    return rows


def _render(
    project_root: str,
    arch_text: str | None,
    ranked_rows: list[dict],
    entry_rows: list[dict],
) -> str:
    repo_name = os.path.basename(os.path.abspath(project_root)) if project_root else "repo"
    parts: list[str] = [f"# CODE_INTEL.md — {repo_name}\n"]
    parts.append(
        "> Auto-generated by `codeintel map`. Re-run after `codeintel index` to refresh.\n"
    )

    if arch_text:
        parts.append("## Architecture Overview\n")
        parts.append(arch_text.strip())
        parts.append("")

    if ranked_rows:
        parts.append("## Ranked Symbols (by caller count)\n")
        parts.append("| Symbol | File | Callers |")
        parts.append("|--------|------|---------|")
        parts.extend(f"| `{row['name']}` | {row['file_path']} | {row['in_degree']} |"
                     for row in ranked_rows)
        parts.append("")
    else:
        parts.append("## Ranked Symbols\n")
        parts.append("_(no symbols found — run `codeintel index` first)_\n")

    if entry_rows:
        parts.append("## Entry Points\n")
        parts.extend(f"- `{row['name']}` ({row['file_path']})" for row in entry_rows)
        parts.append("")

    return "\n".join(parts)


def _enforce_budget(
    content: str,
    budget_bytes: int,
    project_root: str,
    arch_text: str | None,
    ranked_rows: list[dict],
    entry_rows: list[dict],
) -> str:
    if budget_bytes <= 0:
        budget_bytes = 32768

    if len(content.encode("utf-8")) <= budget_bytes:
        return content

    # Binary-search-style: drop rows from ranked table until we fit, then append notice
    rows = list(ranked_rows)
    notice = f"\n> Content truncated to fit {budget_bytes} byte budget.\n"
    while rows:
        candidate = _render(project_root, arch_text, rows, entry_rows) + notice
        if len(candidate.encode("utf-8")) <= budget_bytes:
            return candidate
        rows = rows[:-1]

    # Even with no ranked rows the header+notice must still be returned
    candidate = _render(project_root, arch_text, [], entry_rows) + notice
    return candidate


def _minimal_map(project_root: str, note: str) -> str:
    repo_name = os.path.basename(os.path.abspath(project_root)) if project_root else "repo"
    return (
        f"# CODE_INTEL.md — {repo_name}\n\n"
        f"> Auto-generated by `codeintel map`.\n\n"
        f"**Note**: {note}\n"
    )
