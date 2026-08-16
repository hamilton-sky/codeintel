from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from codeintel.provider import Result, log_swallowed, safe_null_result


def _cypher_literal(s: Any) -> str:
    """Escape a value for a double-quoted Cypher string literal — defense against a
    ``target`` containing quotes/backslashes (e.g. content an agent echoed from a repo)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


# Every op _dispatch recognizes. Kept beside it so "unsupported op" and "op found nothing" stay
# distinguishable — they were the same `None` before, and the resulting `unsupported-op` on a
# perfectly supported `callers` was the most misleading string the never-raise envelope produced.
_GRAPH_OPS = frozenset({
    "impact", "context", "callers", "callees", "chain", "pattern",
    "overview", "changed", "changes", "deadcode", "hotspots",
})


def _strip_project_prefix(qualified_name: str) -> str:
    """Drop the backend's project id from the head of a qualified name.

    The backend prefixes every qualified name with its own project id, which for a path-slug
    registration is the flattened absolute path — so each result line began
    `Users-alice-Documents-project-myrepo.src.pkg.fn`. That is the author's home directory
    layout repeated on every row: noise for a human, wasted tokens for the agent this tool
    exists to serve, on results that can run to a hundred lines.

    Only a leading path-slug-looking segment is removed. A qualified name that starts with a
    real module (`src.codeintel.gateway.query`) is left exactly as it is.
    """
    head, sep, rest = qualified_name.partition(".")
    if not sep:
        return qualified_name
    # A slug: no spaces, and hyphenated (the backend joins path components with "-"). A genuine
    # Python package name cannot contain a hyphen, so this cannot eat a real module.
    if "-" in head and " " not in head:
        return rest
    return qualified_name


# Files consulted when verifying dead-code candidates, and the extensions worth reading. Bounded
# so `deadcode` on a very large monorepo stays a query rather than a second index pass.
_VERIFY_FILE_CAP = 6000
# Not hand-written source: vendored trees and build output. Only `.venv` was excluded before (as
# a dot-directory), so a plain `venv/`, `vendor/` or `third_party/` both blew the file cap and
# fed generated code into the occurrence counts.
_VERIFY_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "dist", "build", "out", "target", "vendor", "vendored",
    "third_party", "thirdparty", "venv", "env", "site-packages", "coverage", "generated",
})
_VERIFY_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".cc", ".hpp",
})


def _drop_referenced_symbols(rows: list[dict], root: str) -> tuple[list[dict], str]:
    """Remove candidates whose name actually appears somewhere else in the source.

    `deadcode` asks the graph for functions with **in-degree 0**, and a function that is passed as
    a REFERENCE rather than called has in-degree 0 — every React event handler, every
    `addEventListener('keydown', onKeyDown)`, every callback handed to a framework. On a real
    TypeScript repo that made 181 of 181 sampled candidates false, and an agent acting on the
    answer would delete live code. A wrong answer here is worse than no answer.

    The graph cannot see those edges, so verify against the source: one pass over the repo,
    counting word-boundary occurrences of each candidate name. A name appearing anywhere beyond
    its own definition is referenced and drops out. Returns (kept, verified) — `verified` is False
    when the repo exceeded the file cap, so the caller can say the check was partial rather than
    imply a confidence it does not have.
    """
    if not rows:
        return rows, "ok"
    if not root or not os.path.isdir(root):
        # Distinct from the cap: the MCP tool defaults project_root to "", so this is the DEFAULT
        # call path, and blaming the file cap told the user their repo was too big when the real
        # cause was a missing argument.
        return rows, "no-root"

    names = {str(r.get("name") or "") for r in rows}
    names.discard("")
    if not names:
        return rows, "ok"

    # How many candidates share each name. Compared against a GLOBAL occurrence count, a fixed
    # allowance of 1 meant two dead functions with the same name each counted as the other's
    # "use" and both vanished.
    definitions: dict[str, int] = {}
    for r in rows:
        key = str(r.get("name") or "")
        definitions[key] = definitions.get(key, 0) + 1
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")

    seen: dict[str, int] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip what is NOT hand-written source. Generated bundles were the worst offender: a
        # 6.7MB minified `out/` bundle supplied 46 occurrences of a `toJSON` that appears zero
        # times in real source, hiding it. Dot-directories use the archive list, so `.github`
        # scripts and `.claude/hooks` are scanned — they were being skipped here while
        # `_is_archived_path` counted them as live, so a live CI helper still read as dead.
        dirnames[:] = [d for d in dirnames if d.lower() not in _VERIFY_SKIP_DIRS
                       and d.lower() not in _ARCHIVE_DIRS]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in _VERIFY_EXTS:
                continue
            scanned += 1
            if scanned > _VERIFY_FILE_CAP:
                return rows, "capped"           # too big to verify — report unfiltered, and say so
            try:
                with open(os.path.join(dirpath, fname), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for match in pattern.findall(text):
                seen[match] = seen.get(match, 0) + 1

    # A name's own definitions account for that many occurrences; anything beyond them is a use
    # the graph could not see.
    kept = [r for r in rows
            if seen.get(str(r.get("name") or ""), 0) <= definitions.get(str(r.get("name") or ""), 1)]
    return kept, "ok"


# Directories whose contents are retired or generated, named explicitly. The first version of
# this excluded EVERY dot-directory, which swept up a great deal of live code: `.claude/hooks`,
# `.storybook`, `.husky`, `.server`, `src/.internal`. Naming what is actually an archive is a
# smaller claim and a safer one — an unknown dot-directory is now assumed to be source.
_ARCHIVE_DIRS = frozenset({
    ".archive", ".archived", ".backup", ".backups", ".bak", ".old", ".deprecated", ".trash",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".gradle", ".terraform",
})


def _is_archived_path(file_path: str) -> bool:
    """Whether *file_path* lives under a retired or generated directory.

    A repo-scan op ranks by complexity and fan-in, and archived code scores well on both — an
    8MB `.archive/` tree put a retired 507-line component third in a repo's refactor hotspots, a
    near-duplicate of the live one. Pointing an agent at dead code as the thing most worth
    refactoring is worse than returning nothing.
    """
    parts = file_path.replace("\\", "/").split("/")
    return any(p.lower() in _ARCHIVE_DIRS for p in parts[:-1])


# What the source-verification pass can and cannot claim, stated per outcome. The single old note
# blamed the file cap unconditionally, so the DEFAULT MCP call path — which omits project_root —
# told users their repo was too big when the real cause was a missing argument.
_RAW_CAVEAT = (" — these are raw call-graph results, so a function used only as a callback "
               "reference (a React handler, an `addEventListener` argument) will appear here. "
               "Confirm before deleting.")
_VERIFY_NOTES = {
    "ok": ("\n\n_Verified against the source: a candidate whose name appears anywhere beyond its "
           "own definition was dropped. A name scan still cannot see a symbol reached only "
           "through a framework — an object-literal property a library calls, a decorator "
           "registry, `getattr` dispatch, or a name in a template, YAML or TOML. Treat these as "
           "candidates and confirm before deleting._"),
    "no-root": ("\n\n_Unverified: no `project_root` was given, so the source could not be checked"
                + _RAW_CAVEAT + "_"),
    "capped": ("\n\n_Unverified: the repo exceeded the source-scan cap" + _RAW_CAVEAT + "_"),
}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _collapse_repeats(label: str) -> str:
    """`A.EditorHeader.EditorHeader.EditorHeader` -> `A.EditorHeader`.

    The backend emits a segment per nesting level, so a component in a file of the same name in a
    directory of the same name repeats three times — and the file path is printed right beside it
    anyway. Across 200 rows that is real token cost for the agent this output exists to serve."""
    out: list[str] = []
    for seg in label.split("."):
        # Only collapse identifiers. Splitting on "." also splits version numbers and dotted
        # quads, where consecutive equal parts are meaningful: `CHANGELOG.1.1.0` became
        # `CHANGELOG.1.0` (a different real release) and `127.0.0.1` became `127.0.1`.
        if out and out[-1] == seg and _IDENTIFIER_RE.match(seg):
            continue
        out.append(seg)
    return ".".join(out)


def _repo_display_name(root: str) -> str:
    """The repo's own directory name, for headings a human will read.

    Resolves first, because callers routinely pass "." (`codeintel map .`) — the basename of which
    is "." and would title the committed map file with a dot."""
    if not root:
        return ""
    try:
        return os.path.basename(os.path.realpath(root).rstrip(os.sep))
    except Exception:
        return ""


def _int_or_zero(value: Any) -> int:
    """A node count from an untrusted backend payload, or 0 when it is missing/not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
      * ``search_graph``   → ``{"total": N, "results": [{name, qualified_name, file_path, in_degree,
                             out_degree, complexity, cognitive, lines, is_test, is_entry_point}, ...]}``
                             — degree filters (max_degree/min_degree/exclude_entry_points) + metrics.
      * ``detect_changes`` → ``{"changed_files": [path, ...], "impacted_symbols": [{qualified_name,
                             name, file_path}, ...], "changed_count": N, "depth": D}``. changed_files
                             come DUPLICATED (staged+unstaged); impacted_symbols interleaves bare file
                             markers (label == file_path) with real symbols.

    Call graph: module-level function calls are recorded as ``USAGE`` edges from the calling
    ``Module`` node; method/function-to-method calls are ``CALLS`` edges. "Who calls X" therefore
    needs BOTH edge types (``[:CALLS|USAGE]``) — ``CALLS`` alone misses every module-level callee
    (that is why the old ``(caller)-[:CALLS]->(fn)`` query returned zero rows for real symbols).
    """

    def __init__(self) -> None:
        self._project_cache: dict[str, str | None] = {}          # resolved names (stable, kept)
        self._negative_until: dict[str, float] = {}                 # failed lookups, short TTL only
        self._project_cache_lock = threading.Lock()  # concurrent HTTP requests share one provider
        self._detect_backend()

    def _detect_backend(self) -> None:
        path = shutil.which("codebase-memory-mcp")
        if path:
            self.available = True
            self._cmd: str | None = path
        else:
            self.available = False
            self._cmd = None

    # Sentinel: distinguishes "the subprocess call failed" from "it succeeded and returned JSON
    # null". Overloading None for both would make a legit null result wrongly trigger the fallback.
    _FAIL = object()

    def _run(self, method: str, payload: dict, timeout_ms: int) -> Any | None:
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
        if self._cmd is None:                  # backend not on PATH — nothing to exec
            return self._FAIL
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
        if self._cmd is None:
            return self._FAIL
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
    def _match_project(raw: Any, project_root: str) -> str | None:
        """Resolve a list_projects response to the project name for ``project_root``.

        The real codebase-memory-mcp returns ``{"projects": [...]}``; a bare list is the
        older/mocked shape — accept both. Prefer an exact ``root_path`` match; otherwise the
        LONGEST prefix match (so ``.../project/codeintel`` resolves to codeintel, not its
        parent ``.../project``). Static so ``_resolve_project`` and ``probe`` share it.

        The backend can hold MORE THAN ONE project for the same root — typically one registered
        under a short name and one under a path slug — and the two drift apart independently.
        Returning the first match meant a query could be answered from a months-stale index while
        a complete one sat beside it: observed on this repo as 1475 nodes vs 2631 for the same
        path, which is how `callers` reported a function's pre-refactor shape hours after the
        refactor. Among exact matches, prefer the most complete index."""
        # Normalize the input to an absolute realpath: the backend stores absolute root_paths, so a
        # relative ``project_root`` (e.g. `codeintel map .` passing ".") would otherwise never match
        # — the bug where the map/query silently reported "not indexed" from inside the repo.
        try:
            project_root = os.path.realpath(project_root)
        except Exception:
            pass
        entries = raw.get("projects", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return None
        exact: list[dict] = []
        best_prefix_len = -1
        best_prefix_name: str | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rp = entry.get("root_path", "")
            if not rp:
                continue
            if rp == project_root:
                exact.append(entry)
                continue
            if project_root.startswith(rp.rstrip("/") + "/") and len(rp) > best_prefix_len:
                best_prefix_len = len(rp)
                best_prefix_name = entry.get("name")

        if exact:
            # Node count is the available completeness signal — list_projects carries no indexed-at
            # timestamp, and head_sha is recorded per registration rather than per index pass, so
            # duplicates routinely report the same SHA with wildly different graphs. `max` keeps
            # the FIRST maximal entry, so with no completeness signal to go on (ties, or a backend
            # that omits `nodes`) this falls back to the original first-listed rule rather than
            # inventing an ordering.
            best = max(exact, key=lambda e: _int_or_zero(e.get("nodes")))
            return best.get("name")
        return best_prefix_name

    def _resolve_project(self, project_root: str) -> str | None:
        with self._project_cache_lock:
            if project_root in self._project_cache:
                return self._project_cache[project_root]  # positive: a repo's name is stable
            neg_until = self._negative_until.get(project_root)
            if neg_until is not None and time.monotonic() < neg_until:
                return None  # a recently-failed lookup, still within its short TTL
        # list_projects shells out — resolve it OUTSIDE the lock so a slow backend can't serialize
        # every concurrent request. A rare duplicate lookup on first contact is harmless.
        raw = self._run("list_projects", {}, 3000)
        name = self._match_project(raw, project_root)
        with self._project_cache_lock:
            if name is not None:
                self._project_cache[project_root] = name
                self._negative_until.pop(project_root, None)
            else:
                # Cache the MISS only briefly, so a repo indexed into the graph AFTER this failed
                # lookup is picked up within the TTL rather than staying stuck until a restart.
                self._negative_until[project_root] = time.monotonic() + 30.0
        return name

    def probe(self, project_root: str, timeout_ms: int = 3000) -> dict:
        """Cheap, never-raise, single-subprocess health check for the doctor.

        Returns ``{installed, runnable, repo_indexed, project, detail, remediation}`` — one
        ``list_projects`` call, bounded by ``timeout_ms`` (``_run`` returns None on timeout)."""
        if not self.available:
            return {
                "installed": False, "runnable": False, "repo_indexed": False, "project": None,
                "detail": "codebase-memory-mcp not found on PATH",
                "remediation": "put the codebase-memory-mcp binary on PATH — it's an external "
                               "native backend (see docs/graph.md); once present it self-updates "
                               "via `codebase-memory-mcp update`",
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
        qn = _strip_project_prefix(str(row.get(qn_key) or ""))
        file = str(row.get(file_key) or "")
        edge = str(row.get("type(c)") or "").strip()
        label = qn or name
        tail = f" ({file})" if file and file != qn else ""
        badge = f" [{edge}]" if edge else ""
        return f"- {label}{badge}{tail}"

    def _search_symbols(self, extra: dict, project: str, timeout_ms: int) -> list[dict] | None:
        """``search_graph`` → parsed result dicts. ``None`` = backend failed/malformed (→ safe-null
        upstream); ``[]`` = backend answered but nothing matched (→ an informative empty render).
        Preserving that distinction is why the repo-scan ops return a string on empty-success but
        ``None`` on can't-answer. Never raises."""
        raw = self._run("search_graph", {"project": project, **extra}, timeout_ms)
        if raw is None:
            return None
        results = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return None
        return [r for r in results if isinstance(r, dict)]

    @staticmethod
    def _looks_like_test(fp: str, name: str) -> bool:
        """Heuristic test detection. The backend's own ``is_test`` flag comes back False for pytest
        functions (verified by dogfooding), so dead-code / hotspot scans must filter by path+name
        or drown in test noise — this is the single most load-bearing renderer detail."""
        f = (fp or "").lower()
        if f.startswith(("tests/", "test/")) or "/tests/" in f or "/test/" in f:
            return True
        base = f.rsplit("/", 1)[-1]
        return (base.startswith("test_") or base.endswith("_test.py")
                or base == "conftest.py" or (name or "").startswith("test_"))

    @staticmethod
    def _is_synthetic(fp: str) -> bool:
        """Builtins / generated nodes carry an empty or ``<...>`` file_path (e.g. <python-builtins>)."""
        return (not fp) or fp.startswith("<")

    @classmethod
    def _is_noise(cls, r: dict) -> bool:
        """Rows a code-quality scan should hide: builtins/generated nodes and test code (the backend's
        own ``is_test`` is unreliable — see ``_looks_like_test``). Shared by deadcode + hotspots."""
        fp = str(r.get("file_path") or "")
        return (cls._is_synthetic(fp)
                or cls._looks_like_test(fp, str(r.get("name") or ""))
                or _is_archived_path(fp))

    def _render_scan(self, kept: list[dict], title: str, cap: int, meta_fn) -> str:
        """Render a repo-scan op's markdown from filtered+sorted rows: ``## title (count)`` + one
        ``- label (file)  [meta]`` line per row (top ``cap``) + a ``+N more`` note when truncated.
        ``meta_fn(row) -> list[str]`` supplies the per-op metric badge, so deadcode/hotspots share
        the row format and truncation note (the drift-prone parts) and differ only in their metrics."""
        lines = []
        for r in kept[:cap]:
            label = _collapse_repeats(
                _strip_project_prefix(str(r.get("qualified_name") or r.get("name") or "?")))
            fp = str(r.get("file_path") or "")
            meta = meta_fn(r)
            badge = f"  [{', '.join(meta)}]" if meta else ""
            tail = f"  ({fp})" if fp else ""
            lines.append(f"- {label}{tail}{badge}")
        body = "\n".join(lines)
        if len(kept) > cap:
            body += f"\n… (+{len(kept) - cap} more)"
        return f"## {title} ({len(kept)})\n" + body

    # ------------------------------------------------------------------ ops

    def _op_callers(self, target: str, project: str, timeout_ms: int) -> str | None:
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE b.name="{_cypher_literal(target)}" '
            "RETURN a.name, a.qualified_name, a.file_path, type(c) LIMIT 50"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        lines = [self._display(r, "a.name", "a.qualified_name", "a.file_path") for r in rows]
        return f"## Callers of {target} ({len(lines)})\n" + "\n".join(lines)

    def _op_callees(self, target: str, project: str, timeout_ms: int) -> str | None:
        cypher = (
            f'MATCH (a)-[c:CALLS|USAGE]->(b) WHERE a.name="{_cypher_literal(target)}" '
            "RETURN b.name, b.qualified_name, b.file_path, type(c) LIMIT 50"
        )
        rows = self._query_rows(cypher, project, timeout_ms)
        if not rows:
            return None
        lines = [self._display(r, "b.name", "b.qualified_name", "b.file_path") for r in rows]
        return f"## Callees of {target} ({len(lines)})\n" + "\n".join(lines)

    def _op_impact(self, target: str, project: str, timeout_ms: int) -> str | None:
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

    def _op_chain(self, target: str, project: str, timeout_ms: int) -> str | None:
        # Accept an "A->B" form (trace from the source symbol) or a bare symbol.
        src = target.split("->")[0].strip() if "->" in target else target.strip()
        if not src:
            return None
        raw = self._run(
            "trace_path",
            {"project": project, "function_name": src, "mode": "calls", "risk_labels": True},
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
                    risk = it.get("risk")
                    label = qn or nm
                    hop_s = f" [hop {hop}]" if hop is not None else ""
                    risk_s = f" [risk: {risk}]" if risk else ""
                    out.append(f"- {label}{hop_s}{risk_s}")
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

    def _op_overview(self, target: str, project: str, timeout_ms: int, root: str = "") -> str | None:
        try:
            raw = self._run("get_architecture", {"project": project}, timeout_ms)
            if not isinstance(raw, dict):
                return None
            # Title with the REPO's own name, not the backend's project id. That id is often a
            # flattened absolute path (`Users-alice-Documents-project-myrepo`), and this heading
            # lands in CODE_INTEL.md — a file that gets committed and pushed, so an internal
            # identifier there leaks the author's home directory layout into the repository.
            name = _repo_display_name(root)
            name = name or str(raw.get("project") or project)
            parts = [f"## Architecture: {name}"]
            tn, te = raw.get("total_nodes"), raw.get("total_edges")
            if tn is not None or te is not None:
                parts.append(f"{tn or 0} nodes, {te or 0} edges")

            def _counts(items: Any, key: str, ckey: str = "count") -> list[str]:
                if not isinstance(items, list):
                    return []
                return [f"- {it.get(key)}: {it.get(ckey)}" for it in items
                        if isinstance(it, dict) and it.get(key) is not None]

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
            files, seen_f = [], set()
            for f in files_raw if isinstance(files_raw, list) else []:
                if isinstance(f, str) and f not in seen_f:
                    seen_f.add(f)
                    files.append(f)
            # impacted_symbols interleaves real symbols with bare file/module markers whose label IS
            # its own path (name == qualified_name == file_path). Drop those structurally by comparing
            # label to file_path — this catches a root-level marker (`main.py`, no "/") AND avoids
            # dropping a real symbol whose qualified name legitimately contains "/" (e.g. Go's
            # github.com/org/pkg.Func). Files are already listed above; dedupe the rest.
            syms, seen_s = [], set()
            for s in syms_raw if isinstance(syms_raw, list) else []:
                if not isinstance(s, dict):
                    continue
                label = str(s.get("qualified_name") or s.get("name") or "").strip()
                fp = str(s.get("file_path") or s.get("file") or "")
                if not label or label == fp:
                    continue
                key = (label, fp)
                if key in seen_s:
                    continue
                seen_s.add(key)
                syms.append((label, fp))
            if not files and not syms:
                return "## Changes impact\n(working tree clean — no uncommitted changes)"
            parts = [f"## Changes impact ({len(files)} files → {len(syms)} symbols)"]
            if files:
                parts.append(f"### Changed files ({len(files)})")
                parts.extend(f"- {f}" for f in files[:40])
                if len(files) > 40:
                    parts.append(f"… (+{len(files) - 40} more)")
            if syms:
                parts.append(f"### Impacted symbols ({len(syms)})")
                for label, fp in syms[:40]:
                    tail = f"  ({fp})" if fp and fp != label else ""
                    parts.append(f"- {label}{tail}")
                if len(syms) > 40:
                    parts.append(f"… (+{len(syms) - 40} more)")
            return "\n".join(parts)
        except Exception:
            return None

    def _op_deadcode(self, project: str, timeout_ms: int, root: str = "") -> str | None:
        """Unreferenced non-test symbols (dead-code candidates): in-degree 0 Functions, entry points
        excluded server-side, tests/builtins filtered client-side, biggest first."""
        try:
            rows = self._search_symbols(
                {"label": "Function", "max_degree": 0, "exclude_entry_points": True, "limit": 200},
                project, timeout_ms,
            )
            if rows is None:
                return None
            # `is_entry_point` is deliberate belt-and-suspenders on top of the server-side
            # exclude_entry_points flag: it's an external-backend flag we don't independently verify,
            # and main() shown as "dead" would be a visible embarrassment — one cheap client check.
            kept = [r for r in rows if not self._is_noise(r) and not r.get("is_entry_point")]
            kept, verify_state = _drop_referenced_symbols(kept, root)
            if not kept:
                return "## Dead-code candidates\n(none found)"
            kept.sort(key=lambda r: r.get("lines") or 0, reverse=True)

            def _meta(r: dict) -> list[str]:
                m = []
                if r.get("out_degree") is not None:
                    m.append(f"out:{r.get('out_degree')}")
                if r.get("lines") is not None:
                    m.append(f"{r.get('lines')} lines")
                return m

            rendered = self._render_scan(kept, "Dead-code candidates", 30, _meta)
            return rendered + _VERIFY_NOTES.get(verify_state, "")
        except Exception:
            return None

    def _op_hotspots(self, project: str, timeout_ms: int) -> str | None:
        """Highest complexity / fan-in symbols (refactor-risk hotspots). search_graph returns rows
        UNSORTED (name order) and caps at ``limit``, so we over-request then sort CLIENT-SIDE by
        (complexity, in_degree). Tests/builtins filtered out."""
        try:
            rows = self._search_symbols(
                {"label": "Function", "min_degree": 1, "limit": 200}, project, timeout_ms,
            )
            if rows is None:
                return None
            kept = [r for r in rows if not self._is_noise(r)]
            if not kept:
                return "## Complexity / fan-in hotspots\n(none found)"
            kept.sort(key=lambda r: (r.get("complexity") or 0, r.get("in_degree") or 0), reverse=True)

            def _meta(r: dict) -> list[str]:
                m = [f"in:{r.get('in_degree') or 0} out:{r.get('out_degree') or 0}",
                     f"cx:{r.get('complexity') or 0} cog:{r.get('cognitive') or 0}"]
                if r.get("lines") is not None:
                    m.append(f"{r.get('lines')} lines")
                return m

            return self._render_scan(kept, "Complexity / fan-in hotspots", 25, _meta)
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

            if op_str not in _GRAPH_OPS:
                return safe_null_result(op_str, target_str, engine="graph", reason="unsupported-op")

            result_text = self._dispatch(op_str, target_str, project, timeout_ms, root_str)
            if result_text is None:
                # A supported op that matched nothing is NOT an unsupported op, and saying so sends
                # the agent looking for a different tool when the real answer is almost always a
                # stale index. Name the cause and the one command that fixes it.
                return safe_null_result(
                    op_str, target_str, engine="graph", reason="not-in-graph",
                    hint=f"`{target_str}` is not in the graph index for project {project!r} — if "
                         f"you just added or renamed it, refresh with: codeintel index {root_str}",
                )

            return {
                "ok": True,
                "op": op_str,
                "target": target_str,
                "result": result_text,
                "engine": "graph",
                "cached": False,
            }
        except Exception as exc:
            log_swallowed("GraphProvider.build_result", exc)
            return safe_null_result(op, target, engine="graph", reason="error")

    def _dispatch(
        self, op: str, target: str, project: str, timeout_ms: int, root: str = ""
    ) -> str | None:
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
            return self._op_overview(target, project, timeout_ms, root)
        if op == "changed" or op == "changes":
            return self._op_changed(project, timeout_ms)
        if op == "deadcode":
            return self._op_deadcode(project, timeout_ms, root)
        if op == "hotspots":
            return self._op_hotspots(project, timeout_ms)
        return None
