from __future__ import annotations

import ast
import hashlib
import logging
import os
import sqlite3
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from codeintel.containment import contained_path, real_root
from codeintel.progress import ProgressSink, _Guard
from codeintel.semantic_db import MAX_CHUNK_CHARS, chunk_content_hash
from codeintel.source_kind import (
    CODE_EXTS,
    load_gitattributes_globs,
    looks_generated_file,
    looks_generated_path,
    matches_any_glob,
)

if TYPE_CHECKING:
    from codeintel.semantic_db import SemanticDb

logger = logging.getLogger(__name__)

# Hard ceiling on the characters embedded for ONE chunk. Line-based splitting cannot bound a
# minified or generated single-line file, and the embedder's memory use scales with input size.
# Generous enough that no hand-written function is affected. Defined beside the schema so the
# searcher hashes an oversized chunk exactly the way the indexer stored it; re-exported here
# because this module is where the truncation is actually performed (and warned about).
_MAX_CHUNK_CHARS = MAX_CHUNK_CHARS

# Bytes examined when deciding whether a file is binary. A NUL in the first block is the classic
# signal and is what `git` itself uses.
_BINARY_SNIFF_BYTES = 8192

# Derived from the shared policy rather than repeated: the corpus is "code, plus markdown". Two
# hand-typed copies of one population is how the sign-out bug in the review notes happened — one
# copy drifts and nothing says so.
_INDEXED_EXTS = CODE_EXTS | {".md"}
_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules"})
# Directory names that mean "generated" only at the REPO ROOT. Matching them at any depth hid real
# source: `coverage/` is the coverage.py package, `src/build/` is pypa/build's entire codebase
# (which indexed 0 files), `src/generated/` is a committed API client, and a NestJS `vendor`
# domain module is a first-class part of the app. At the root they are almost always output; one
# level down they are almost always somebody's package.
_ROOT_ONLY_IGNORES = frozenset({
    "out", "vendor", "vendored", "third_party", "thirdparty", "site-packages", "coverage",
    "generated", "dist", "build", "target",
})
# Vendored / regenerable dirs skipped even without a .gitignore entry.
_DEFAULT_IGNORES = frozenset({
    ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", ".cache",
    # Generated output and retired trees. Without these a search for "websocket reconnect logic"
    # on a real repo returned an ARCHIVED markdown file's blank line as the top hit and the actual
    # implementation second: the corpus, not the ranking, was the problem.
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
    ".archive", ".archived", "_archive", "_archived", ".backup", ".backups", ".old",
    ".deprecated", ".trash",
})


def _project_key(project_root_real: str) -> str:
    """A short, stable id for a project root — prefixes every chunk_id so two repos
    with an identically-named file never collide in the shared cache."""
    return hashlib.sha256(project_root_real.encode()).hexdigest()[:12]


def _pos_int(val: object, default: int) -> int:
    """A usable positive int or the default — mirrors ``config._coerce`` so a direct
    ``Indexer(...)`` caller (which bypasses config validation) can't set a zero/negative/non-int
    ``stride`` (would raise inside ``range()``) or ``window`` (would silently drop every region)."""
    try:
        n = int(val)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return default
    return n if n > 0 else default


# ---- tree-sitter chunking (P3): def-aligned chunks for non-Python languages -----------------
# Extends the P1 chunker interface to TS/JS/Go/Rust/Java/C/C++ via tree-sitter, behind the same
# _primary_spans → _cover pipeline. The grammar pack is a normal dependency but never a hard
# requirement: if it (or a grammar) is unavailable, or a file's language config is missing, the
# file falls back to line windowing — exactly like a Python parse failure.

# file extension -> tree_sitter_language_pack language name. Only the *code* exts in
# _INDEXED_EXTS; .md (and anything unmapped) keeps line-windowing.
_TS_LANG_BY_EXT = {
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "cpp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp",
}
# "Leaf" definition node types — each emitted as one whole chunk (like a top-level Python def).
_TS_FUNC_TYPES = {
    "typescript": {"function_declaration", "generator_function_declaration", "method_definition",
                   "method_signature", "abstract_method_signature", "function_signature"},
    "tsx": {"function_declaration", "generator_function_declaration", "method_definition",
            "method_signature", "abstract_method_signature", "function_signature"},
    "javascript": {"function_declaration", "generator_function_declaration", "method_definition"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "function_signature_item", "struct_item", "enum_item",
             "union_item", "type_item", "macro_definition"},
    "java": {"method_declaration", "constructor_declaration"},
    "c": {"function_definition", "struct_specifier", "enum_specifier", "union_specifier"},
    "cpp": {"function_definition"},
}
# "Container" definition types — emitted as a header chunk + one chunk per nested member, exactly
# like a Python class (so per-method chunks are never shadowed by a whole-class chunk).
_TS_CONTAINER_TYPES = {
    "typescript": {"class_declaration", "abstract_class_declaration", "interface_declaration"},
    "tsx": {"class_declaration", "abstract_class_declaration", "interface_declaration"},
    "javascript": {"class_declaration"},
    "go": set(),
    "rust": {"impl_item", "trait_item", "mod_item"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"},
    "c": set(),
    "cpp": {"class_specifier", "struct_specifier", "namespace_definition"},
}
# JS/TS assign functions to bindings — `const C = () => {}`, `export const f = function(){}` — which
# is how React components and hooks (and most modern TS) are written. A lexical/variable declaration
# counts as a def ONLY when a declarator's value is a function, not a plain `const x = 5`.
_TS_ARROW_LANGS = frozenset({"typescript", "tsx", "javascript"})
_TS_DECL_TYPES = frozenset({"lexical_declaration", "variable_declaration"})
_TS_FUNC_VALUE_TYPES = frozenset({"arrow_function", "function_expression", "function",
                                  "generator_function"})


def _ts_decl_is_function(node) -> bool:
    """True if a lexical/variable declaration binds a function (arrow or function expression)."""
    for child in node.children:
        if child.type == "variable_declarator" and any(
                gc.type in _TS_FUNC_VALUE_TYPES for gc in child.children):
            return True
    return False


def _has_substance(text: str) -> bool:
    """Whether *text* carries any word content at all.

    Deliberately the weakest possible test — a single letter passes. The target is chunks made
    only of separators and punctuation (`---`, `===`, a bare `#`, a lone brace), which embed to a
    vector that matches everything weakly and so surface for any query; a `---` front-matter fence
    was the top hit for a real search. Anything stricter starts rejecting real one-line code:
    `x = 1` has two word characters and belongs in the index."""
    return any(ch.isalpha() for ch in text)


def _is_source_package(path) -> bool:
    """Whether a directory announces itself as a package rather than build output.

    `coverage/` at a repo root is ambiguous: it is the coverage.py PACKAGE in that project and a
    report directory in most others. A package carries an `__init__.py` or a manifest; a report
    directory carries HTML and XML."""
    for marker in ("__init__.py", "package.json", "index.ts", "index.js", "go.mod", "Cargo.toml"):
        try:
            if (path / marker).is_file():
                return True
        except OSError:
            return False
    return False


def _looks_binary(path) -> bool:
    """Whether *path* is binary, by the same rule git uses: a NUL byte in the opening block."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True                        # unreadable → treat as unindexable rather than crash


def _nul_byte_line(path) -> int | None:
    """1-based line of the first NUL byte within the sniff window, or None. Turns the bare "looks
    binary" skip into a message that points at the exact spot — a raw NUL in a source file is almost
    always a deliberate separator (`.join('\\0')`, a composite key) saved as a byte instead of an
    escape, and naming the line is the difference between a fixable warning and a puzzle."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return None
    i = head.find(b"\x00")
    return None if i < 0 else head.count(b"\n", 0, i) + 1


class Indexer:
    def __init__(
        self,
        db: SemanticDb,
        model_name: str = "BAAI/bge-small-en-v1.5",
        window: int = 20,
        stride: int = 10,
        max_chunks: int = 500,
        max_total_chunks: int = 100000,
        chunk_strategy: str = "syntax",
        max_chunk_lines: int | None = None,
        progress: ProgressSink | None = None,
    ) -> None:
        self.db = db
        self.model_name = model_name
        # Optional live-progress sink. Wrapped once in _Guard so every emit below is null-safe and
        # never-raise: with the default None it is a no-op (the MCP server and Reindexer get silence
        # and pay nothing), and a misbehaving sink can never abort a pass or change the count.
        self._progress = _Guard(progress)
        # Coerce the numeric knobs defensively: the 4 production call sites pass config-validated
        # values, but a direct caller must not be able to set a stride/window that raises in
        # range() or silently drops regions (mirrors config._coerce's _POSITIVE_INTS clamp).
        self.window = _pos_int(window, 20)
        self.stride = _pos_int(stride, 10)
        self.max_chunks = _pos_int(max_chunks, 500)               # per file
        self.max_total_chunks = _pos_int(max_total_chunks, 100000)  # ceiling per pass (mem backstop)
        # "syntax" chunks Python on def/class boundaries (ast); "lines" is the fixed-window
        # fallback used for every non-.py file, on any parse failure, and as a runtime escape
        # hatch. Case-normalized then range-checked so an unknown value degrades to "syntax"
        # (config already validates; this keeps a direct caller from silently disabling — or
        # accidentally case-swapping — the strategy with a typo).
        strategy = str(chunk_strategy).strip().lower()
        self.chunk_strategy = strategy if strategy in ("syntax", "lines") else "syntax"
        # A def longer than this is window-chunked internally so no single chunk overflows the
        # embedder (~512 tokens). Defaults to 2*window; never <= 0 (would loop / never split).
        self.max_chunk_lines = (
            max_chunk_lines if isinstance(max_chunk_lines, int) and max_chunk_lines > 0
            else 2 * self.window
        )
        self._embedder = None
        self._ts_parsers: dict = {}  # per-instance tree-sitter parser cache (lang -> parser|None)
        # Why the last index() failed, for callers that must SHOW a reason rather than log one.
        self.last_error: str | None = None
        # Rows whose content was unchanged but whose `chunk_end` this pass patched in place
        # (pre-migration NULLs). Counted so the commit can be skipped when there were none.
        self._backfilled = 0

    def _get_embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=self.model_name)
        return self._embedder

    def index(self, project_root: str) -> int:
        """Return count of newly embedded chunks, or -1 on unrecoverable failure.

        The failure REASON is kept on ``self.last_error`` as well as logged. Returning a bare -1
        left every caller able to say only "an unrecoverable failure" — which is what `setup --all`
        printed in its step table, while the actual cause (a blocked model download, an unwritable
        cache directory) sat in a stderr line the user had already scrolled past and had no reason
        to connect to the row in front of them.
        """
        self.last_error = None
        try:
            return self._index(project_root)
        except Exception as exc:
            logger.error("Indexer.index() unrecoverable failure: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return -1

    def _load_gitignore(self, root: Path) -> set[str]:
        """Best-effort ``.gitignore``: collect simple name/dir patterns to skip. This is
        NOT full gitignore semantics (no globs, negations, or nesting) — just enough to
        avoid indexing vendored/build output the user already told git to ignore."""
        patterns: set[str] = set()
        gi = root / ".gitignore"
        try:
            if gi.is_file():
                for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "!")):
                        continue
                    name = line.rstrip("/").lstrip("/")
                    if name and "*" not in name and "/" not in name:
                        patterns.add(name)
        except Exception:
            pass
        return patterns

    def _cleanup_deleted(self, root: Path, project_root_real: str) -> None:
        """Drop rows for THIS project whose file no longer exists — scoped by
        project_root so touching one repo can never purge another's index."""
        conn = self.db.conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM chunk_hashes WHERE project_root = ?",
                (project_root_real,),
            ).fetchall()
            # `.exists()` FOLLOWS symlinks, so a file replaced by a link pointing outside the root
            # answered True and its row survived every reindex — the stale row that made the
            # post-index symlink swap a standing hole rather than a race. Asking the containment
            # module instead means "no longer a readable file genuinely inside this root" is one
            # predicate, and a swapped file is reconciled away like a deleted one.
            root_real = real_root(str(root))
            deleted_paths = [
                row[0] for row in rows if contained_path(root_real, root / row[0]) is None
            ]
            for fp in deleted_paths:
                chunk_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT chunk_id FROM chunk_hashes"
                        " WHERE project_root = ? AND file_path = ?",
                        (project_root_real, fp),
                    ).fetchall()
                ]
                for cid in chunk_ids:
                    # `code_embeddings` is created lazily at the first embed, so it can be absent
                    # here. Letting that raise aborts the whole cleanup pass through the handler
                    # below, leaving every stale row — including one for a file swapped for a
                    # symlink, which is precisely what must not survive.
                    try:
                        conn.execute(
                            "DELETE FROM code_embeddings WHERE chunk_id = ?", (cid,)
                        )
                    except sqlite3.OperationalError:
                        pass
                    conn.execute(
                        "DELETE FROM chunk_hashes WHERE chunk_id = ?", (cid,)
                    )
            conn.commit()
        except Exception as exc:
            logger.warning("Cleanup pass failed: %s", exc)

    @staticmethod
    def _binary_check(path) -> bool:
        return _looks_binary(path)

    def _walk_files(self, root: Path):
        """Indexable files inside *root* — and strictly inside it.

        ``os.walk`` defaults to ``followlinks=False``, which stops recursion into symlinked
        DIRECTORIES but says nothing about symlinked FILES: those still appear in ``filenames``,
        and the later ``open()`` follows them transparently. That made a planted symlink a
        complete bypass of RBAC project scoping — a tenant able to write inside their own allowed
        root could link to any file the server process can read and have it indexed, embedded, and
        returned as a search snippet. Demonstrated before this guard existed.

        So every candidate is resolved and required to land back under the resolved root."""
        ignores = set(_SKIP_DIRS) | set(_DEFAULT_IGNORES) | self._load_gitignore(root)
        # The repository's own declaration of which files are not hand-written. This is the only
        # authoritative signal available — every other check here is inference — and nothing
        # consulted it before. Read once per walk, not per file.
        gitattributes = load_gitattributes_globs(str(root))
        real_root_str = str(root)
        try:
            root_real = real_root(str(root))
        except Exception:
            root_real = str(root)
        for dirpath, dirnames, filenames in os.walk(root):
            at_root = os.path.realpath(dirpath) == os.path.realpath(real_root_str)
            dirnames[:] = [
                d for d in dirnames
                if d not in ignores and not d.endswith(".egg-info")
                and not (at_root and d.lower() in _ROOT_ONLY_IGNORES
                         and not _is_source_package(Path(dirpath) / d))
            ]
            for fname in filenames:
                if fname in ignores:
                    continue
                if Path(fname).suffix.lower() not in _INDEXED_EXTS:
                    continue
                candidate = Path(dirpath) / fname
                # Symlink escape and hard-link aliasing are both decided by `contained_path`, which
                # the searcher and the cleanup pass also call. Keeping the rule in one module is
                # the point: it was reimplemented per-site before, so the query-time read had no
                # check at all and a file swapped after indexing was served from outside the root.
                if contained_path(root_real, candidate) is None:
                    logger.warning("skipping %s — it does not resolve to a file inside the indexed "
                                   "root (symlink out, or extra hard links)", candidate)
                    continue
                # Generated content is excluded from the CORPUS, not merely down-ranked: a search
                # competes chunks against each other, so one checked-in bundle contributes hundreds
                # of chunks that a real implementation then has to outrank. The directory lists
                # above miss generated files that sit beside real source (`*.min.js`, `*_pb2.py`)
                # and any build tree whose name nobody thought to list.
                rel = os.path.relpath(str(candidate), str(root))
                if looks_generated_path(rel) or matches_any_glob(rel, gitattributes):
                    continue
                # A source extension is not a promise of source. A compiled artifact or blob named
                # `.py` was read with errors="replace" and embedded as replacement-character
                # garbage — 196KB of /dev/urandom produced 162 chunks — which then competed for
                # rank against real code in every search.
                if _looks_binary(candidate):
                    line = _nul_byte_line(candidate)
                    where = f" at line {line}" if line else ""
                    logger.warning(
                        "skipping %s — contains a null byte%s, so it reads as binary (the rule git "
                        "uses too). If that null is intentional (e.g. a key/join separator), write "
                        "it as the `\\0` escape instead of a raw byte and re-run index.",
                        candidate, where)
                    continue
                # Last resort, and the only check that catches a bundle whose directory and
                # filename both look ordinary. One 6.7MB minified chunk contributes hundreds of
                # chunks that real implementations then have to outrank — the corpus, not the
                # ranking, was the problem. Bounded to an 8KB sample.
                if looks_generated_file(candidate):
                    logger.info("skipping %s — content looks generated (minified or banner)",
                                candidate)
                    continue
                yield candidate

    # ---- chunk-span computation ------------------------------------------------------------
    # A file is turned into a list of 0-based, half-open ``(start, end)`` line spans; every
    # strategy funnels through the same span list so downstream materialisation (whitespace
    # skip, hash-dedup, caps, orphan reconcile) is shared and identical.

    def _window_spans(self, start: int, end: int) -> list[tuple[int, int]]:
        """Fixed overlapping line windows over ``[start, end)`` — the original chunking, reused to
        fill inter-def gaps and split oversized defs. ``_window_spans(0, len(lines))`` reproduces
        the old ``range(0, n, stride)`` + ``lines[s:s+window]`` output exactly."""
        spans: list[tuple[int, int]] = []
        if end <= start:
            return spans
        spans.extend((s, min(s + self.window, end)) for s in range(start, end, self.stride))
        return spans

    def _maybe_split(self, start: int, end: int) -> list[tuple[int, int]]:
        """A def span kept whole, or window-split when it exceeds ``max_chunk_lines`` so no single
        chunk overflows the embedder."""
        if end - start <= self.max_chunk_lines:
            return [(start, end)]
        return self._window_spans(start, end)

    @staticmethod
    def _node_span(node: ast.AST, n: int) -> tuple[int, int]:
        """0-based half-open ``[start, end)`` span of a def/class node, decorators included
        (``min(decorator linenos, node.lineno)`` … ``end_lineno``), clamped into ``[0, n]``."""
        start = node.lineno  # type: ignore[attr-defined]
        for dec in getattr(node, "decorator_list", None) or []:
            dline = getattr(dec, "lineno", None)
            if isinstance(dline, int):
                start = min(start, dline)
        end = getattr(node, "end_lineno", None)
        if not isinstance(end, int):
            end = node.lineno  # type: ignore[attr-defined]
        start0 = max(0, start - 1)
        end0 = min(n, max(start0 + 1, end))
        return (start0, end0)

    def _primary_spans(self, tree: ast.Module, n: int) -> list[tuple[int, int]]:
        """Def-aligned 'primary' spans: each top-level function, and for each top-level class a
        header span (class line → just before its first method/nested def) plus one span per
        method/nested def. Inter-method and module-level runs are intentionally left uncovered
        here — ``_cover`` window-fills them — so per-method chunks are never double-embedded."""
        spans: list[tuple[int, int]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spans.append(self._node_span(node, n))
            elif isinstance(node, ast.ClassDef):
                cstart, cend = self._node_span(node, n)
                members = [
                    self._node_span(c, n)
                    for c in node.body
                    if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                ]
                if not members:
                    spans.append((cstart, cend))  # no methods → the class is one unit
                    continue
                members.sort()
                header_end = max(cstart + 1, min(members[0][0], cend))
                spans.append((cstart, header_end))  # header: bases + class docstring
                spans.extend(members)
        return spans

    def _cover(self, primary: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
        """Gapless cover of ``[0, n)`` in file order: window-fill every gap between primary spans
        and window-split any oversized def. The def-aligned *primary* spans are whole and mutually
        non-overlapping (no whole-class chunk shadowing its per-method chunks); the window-filled
        gaps and oversized-def splits reuse the existing ``window``/``stride``, so — exactly like
        the legacy line windower — adjacent windows inside one filled run *do* overlap when
        ``stride < window``. Coverage is always complete; chunk starts are always unique."""
        result: list[tuple[int, int]] = []
        cursor = 0
        for s0, e0 in sorted(primary):
            s = max(s0, cursor)   # clamp any (pathological) overlap so nothing is double-covered
            e = min(e0, n)
            if s >= e:
                continue
            if s > cursor:
                result.extend(self._window_spans(cursor, s))  # module-level / inter-def run
            result.extend(self._maybe_split(s, e))
            cursor = e
        if cursor < n:
            result.extend(self._window_spans(cursor, n))
        return result

    def _chunk_python_ast(self, lines: list[str], source: str) -> list[tuple[int, int]]:
        """Parse ``source`` → a complete, non-overlapping, def-aligned cover of the file as
        0-based half-open ``(start, end)`` spans. Raises on parse failure (``SyntaxError`` /
        ``ValueError`` on NUL bytes / ``RecursionError`` / …) so the caller falls back to windows."""
        tree = ast.parse(source)
        n = len(lines)
        return self._cover(self._primary_spans(tree, n), n)

    def _get_ts_parser(self, lang: str):
        """Lazily load + cache (per instance) the tree-sitter parser for a language. Returns None
        when ``tree-sitter-language-pack`` isn't installed or the grammar is unavailable — the
        caller then windows (tree-sitter is a normal dep, never a hard requirement)."""
        cache = self._ts_parsers
        if lang in cache:
            return cache[lang]
        parser = None
        try:
            from tree_sitter_language_pack import get_parser
            parser = get_parser(lang)
        except Exception as exc:
            logger.debug("tree-sitter parser for %s unavailable: %s", lang, exc)
        cache[lang] = parser
        return parser

    @staticmethod
    def _ts_end_line(node, n: int) -> int:
        """0-based half-open end line of a tree-sitter node. ``end_point`` is (row, col); a col of 0
        means the node ends at the start of that row (so the row isn't included). Clamped to
        ``[start+1, n]``."""
        row, col = node.end_point
        end = row if col == 0 else row + 1
        return min(n, max(node.start_point[0] + 1, end))

    def _primary_spans_ts(self, root, lang: str, n: int) -> list[tuple[int, int]]:
        """Def-aligned 'primary' spans from a tree-sitter tree, mirroring ``_primary_spans``: each
        function/method → one span; each container (class/impl/trait/…) → a header span plus one
        span per nested member. Node types the language config doesn't list are simply not treated
        as defs — they fall into ``_cover``'s window-filled gaps, so an incomplete config degrades
        precision, never correctness."""
        func_types = _TS_FUNC_TYPES.get(lang, set())
        cont_types = _TS_CONTAINER_TYPES.get(lang, set())
        all_types = func_types | cont_types
        arrow_ok = lang in _TS_ARROW_LANGS
        if not all_types:
            return []  # unknown language -> caller windows the whole file
        spans: list[tuple[int, int]] = []

        def is_def(c) -> bool:
            if c.type in all_types:
                return True
            # a const/let bound to an arrow/function (React components, hooks, callbacks)
            return arrow_ok and c.type in _TS_DECL_TYPES and _ts_decl_is_function(c)

        def nearest_defs(node):
            """Def nodes reachable without crossing another def — top-level defs from the root, a
            container's direct members from the container. Iterative, so deep nesting can't blow the
            stack. Function-bound `const`/`let` declarations count as defs (is_def)."""
            found = []
            stack = [c for c in reversed(node.children) if c.is_named]
            while stack:
                c = stack.pop()
                if is_def(c):
                    found.append(c)
                else:
                    stack.extend(g for g in reversed(c.children) if g.is_named)
            return found

        def emit(node):
            s = node.start_point[0]
            e = self._ts_end_line(node, n)
            if node.type in cont_types:
                members = sorted(nearest_defs(node), key=lambda m: m.start_point[0])
                if members:
                    header_end = max(s + 1, min(members[0].start_point[0], e))
                    spans.append((s, header_end))  # header: class/impl line -> first member
                    for m in members:
                        emit(m)
                    return
            spans.append((s, e))

        for node in sorted(nearest_defs(root), key=lambda x: x.start_point[0]):
            emit(node)
        return spans

    def _chunk_treesitter(self, lines: list[str], source: str, lang: str):
        """Parse ``source`` with tree-sitter → a complete, def-aligned cover, or ``None`` when the
        parser is unavailable (caller windows). tree-sitter is error-tolerant, so a syntactically
        broken file still yields a partial tree (and thus useful spans) rather than raising."""
        parser = self._get_ts_parser(lang)
        if parser is None:
            return None
        tree = parser.parse(source.encode("utf-8", errors="replace"))
        n = len(lines)
        return self._cover(self._primary_spans_ts(tree.root_node, lang, n), n)

    def _spans_for_file(
        self, filepath: Path, lines: list[str], rel_path: str
    ) -> list[tuple[int, int]]:
        """Choose spans for one file under the syntax strategy: ``ast`` for ``.py``, tree-sitter for
        the mapped languages (TS/JS/Go/Rust/Java/C/C++), fixed windows for everything else and on
        any parse/grammar failure or when tree-sitter isn't installed."""
        if self.chunk_strategy == "syntax":
            suffix = filepath.suffix.lower()
            if suffix == ".py":
                try:
                    return self._chunk_python_ast(lines, "".join(lines))
                except Exception as exc:
                    logger.debug("syntax chunking failed for %s (%s) — windowing", rel_path, exc)
            else:
                lang = _TS_LANG_BY_EXT.get(suffix)
                if lang is not None:
                    try:
                        spans = self._chunk_treesitter(lines, "".join(lines), lang)
                        if spans is not None:
                            return spans
                    except Exception as exc:
                        logger.debug("tree-sitter chunking failed for %s (%s) — windowing",
                                     rel_path, exc)
        return self._window_spans(0, len(lines))

    # ---- materialisation -------------------------------------------------------------------

    def _emit_spans(
        self,
        spans: list[tuple[int, int]],
        lines: list[str],
        rel_path: str,
        project_key: str,
        conn,
        new_chunks: list[tuple[str, str, str, int, int, str]],
    ) -> tuple[set[str], bool]:
        """Materialise spans into new/changed chunk records — shared by both strategies, so the
        whitespace-skip, hash-dedup, and per-file cap behave identically. Returns
        ``(keep_ids, complete)``: ``keep_ids`` is every chunk_id this file legitimately produces
        (drives orphan reconciliation); ``complete`` is False iff the *global* ceiling cut the
        file short, in which case ``keep_ids`` is partial and MUST NOT delete anything."""
        keep_ids: set[str] = set()
        chunk_count = 0
        for start, end in spans:
            if len(new_chunks) >= self.max_total_chunks:
                return keep_ids, False  # global ceiling mid-file — keep_ids is partial
            if chunk_count >= self.max_chunks:
                logger.debug("chunk cap hit for %s, truncating at %d", rel_path, self.max_chunks)
                break  # per-file cap is deterministic (same first-N each pass) → reconcile is safe
            chunk_lines = lines[start:end]
            if not chunk_lines:
                continue
            chunk_text = "".join(chunk_lines)
            if not _has_substance(chunk_text):
                # A chunk of separators, punctuation or a lone `#` embeds to a vector that matches
                # everything weakly and therefore surfaces for anything. `---` from a markdown
                # front-matter fence was the top hit for a real query. Whitespace was already
                # rejected below; this rejects content that is technically non-empty but carries
                # no retrievable meaning.
                chunk_count += 1
                continue
            # Cap chunk BYTES, not just lines. `_maybe_split` splits on line boundaries, so a
            # minified bundle or a generated one-liner is a single unsplittable chunk however
            # large: a 20MB one-line .py peaked at 3.4GB RSS through the embedder, and a 40MB
            # one extrapolates past 8GB. That runs on the reindexer's daemon thread inside the
            # long-lived MCP server, so it can take the agent host down, not just a CLI run.
            # Truncation is the right trade — the head of a chunk carries its identifying
            # content, and an over-long minified line has no retrieval value past that anyway.
            if len(chunk_text) > _MAX_CHUNK_CHARS:
                logger.warning("truncating an oversized chunk in %s (%d chars) to %d",
                               rel_path, len(chunk_text), _MAX_CHUNK_CHARS)
                chunk_text = chunk_text[:_MAX_CHUNK_CHARS]
            if not chunk_text.strip():
                # EC3.4: never embed empty/whitespace-only chunks (zero vectors pollute results).
                chunk_count += 1
                continue
            chunk_id = f"{project_key}:{rel_path}:{start}"
            content_hash = chunk_content_hash(chunk_text)
            keep_ids.add(chunk_id)  # produced this pass — keep even when dedup skips re-embed
            try:
                row = conn.execute(
                    "SELECT content_hash, chunk_end FROM chunk_hashes WHERE chunk_id = ?",
                    (chunk_id,),
                ).fetchone()
                if row and row[0] == content_hash:
                    # Unchanged content: the stored embedding is still correct and must NOT be
                    # recomputed. But a row written before `chunk_end` existed carries NULL, and
                    # the searcher cannot verify staleness without it — so patch the span in
                    # place. This is what lets an existing 85k-chunk cache gain verification from
                    # one ordinary index pass instead of a full re-embed.
                    if row[1] != end:
                        conn.execute(
                            "UPDATE chunk_hashes SET chunk_end = ? WHERE chunk_id = ?",
                            (end, chunk_id),
                        )
                        self._backfilled += 1
                    chunk_count += 1
                    continue
            except Exception as exc:
                logger.debug("hash check failed for %s: %s", chunk_id, exc)
            new_chunks.append((chunk_id, chunk_text, rel_path, start, end, content_hash))
            chunk_count += 1
        return keep_ids, True

    def _collect_new_chunks(
        self, root: Path, project_key: str, project_root_real: str
    ) -> list[tuple[str, str, str, int, int, str]]:
        """Walk files; return (chunk_id, text, rel_path, start, end, hash) for new/changed chunks, and
        reconcile each fully-processed file (dropping rows for chunks it no longer produces)."""
        conn = self.db.conn()
        new_chunks: list[tuple[str, str, str, int, int, str]] = []
        files_seen = 0

        for filepath in self._walk_files(root):
            if len(new_chunks) >= self.max_total_chunks:
                logger.warning(
                    "index: reached max_total_chunks=%d this pass — stopping "
                    "(raise it in .codeintel.toml to embed more of a very large repo)",
                    self.max_total_chunks,
                )
                break
            files_seen += 1
            # A tick per file guarantees liveness even across a run of files that `continue` below
            # (vanished / unreadable), which is exactly when a lone final tick would look stuck.
            self._progress.scan(files_seen, len(new_chunks))
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                logger.debug("file disappeared: %s", filepath)
                continue
            except Exception as exc:
                logger.debug("skipping %s: %s", filepath, exc)
                continue

            rel_path = str(filepath.relative_to(root))
            spans = self._spans_for_file(filepath, lines, rel_path)
            keep_ids, complete = self._emit_spans(
                spans, lines, rel_path, project_key, conn, new_chunks
            )
            if complete:
                # Reconcile only a fully-processed file: drop rows for defs/windows it no longer
                # produces (a moved/deleted function, or a strategy switch). Skipped when the
                # global ceiling truncated the file — its partial keep_ids would delete rows past
                # the cut that are still valid. Scoped by (project_root, file_path) in the db layer.
                # Caveat: two *concurrent* index passes over the same project computed from
                # different point-in-time reads can transiently resurrect a just-deleted stale row
                # (the later pass's INSERT OR REPLACE re-adds what the earlier reconcile removed).
                # This self-heals on the next single-reader pass, and is strictly better than the
                # pre-0.6 behaviour (the stale row persisted forever); cross-pass serialization is
                # out of scope for the indexer.
                self.db.delete_file_orphans(project_root_real, rel_path, keep_ids)

        # Commit any in-place `chunk_end` backfills. These ride outside `_embed_and_write`'s
        # per-batch commits, and a pass that finds nothing new to embed returns before reaching
        # them — so without this the backfill is computed and then dropped on every up-to-date
        # repo, which is precisely the case it exists to serve.
        if self._backfilled:
            try:
                conn.commit()
                logger.debug("backfilled chunk_end for %d existing chunks", self._backfilled)
            except Exception as exc:
                logger.warning("committing chunk_end backfill failed: %s", exc)

        # One final tick with the true totals so the committed scan line is accurate (the per-file
        # ticks lag by the current file's chunks). The renderer records this even when throttled.
        self._progress.scan(files_seen, len(new_chunks))
        return new_chunks

    def _embed_and_write(
        self, new_chunks: list[tuple[str, str, str, int, int, str]], project_root_real: str
    ) -> int:
        total = len(new_chunks)
        # Signal the model-load BEFORE materialising the embedder: on a cold cache _get_embedder()
        # downloads the model (minutes), a distinct stall the scan counter can't cover.
        self._progress.load_model()
        embedder = self._get_embedder()  # may raise → propagates to index() → returns -1
        conn = self.db.conn()
        embedded_count = 0
        batch_size = 32
        self._progress.embed(0, total)  # land on 0% the instant the model is ready

        for i in range(0, total, batch_size):
            batch = new_chunks[i: i + batch_size]
            texts = [c[1] for c in batch]

            try:
                embeddings = list(embedder.embed(texts))
            except Exception as exc:
                logger.warning("embedding batch %d failed: %s", i // batch_size, exc)
                continue

            for j, (chunk_id, _, rel_path, chunk_start, chunk_end, content_hash) in enumerate(
                batch
            ):
                if j >= len(embeddings):
                    break
                try:
                    vec = embeddings[j]
                    # Create code_embeddings lazily, sized to this vector (self-dimensioning). A
                    # returned dim != len(vec) means the file already holds a different dimension
                    # (only reachable on a DEFAULT_MODEL size bump) — skip rather than corrupt/mix.
                    if self.db.ensure_embeddings_table(len(vec)) != len(vec):
                        continue
                    vec_bytes = struct.pack(f"{len(vec)}f", *vec)
                    # sqlite-vec's vec0 virtual table does NOT honor INSERT OR REPLACE — it raises
                    # UNIQUE on an existing chunk_id instead of replacing. So re-embedding a chunk
                    # whose content changed but whose chunk_id (start line) is stable — the common
                    # case under syntax chunking, where a def's chunk_id is its def line — would
                    # silently fail and keep the STALE vector. DELETE-then-INSERT is the supported
                    # upsert for vec0. (chunk_hashes below is a normal table, where REPLACE works.)
                    conn.execute(
                        "DELETE FROM code_embeddings WHERE chunk_id = ?", (chunk_id,)
                    )
                    conn.execute(
                        "INSERT INTO code_embeddings(chunk_id, embedding) VALUES (?, ?)",
                        (chunk_id, vec_bytes),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO chunk_hashes"
                        "(chunk_id, project_root, file_path, chunk_start, chunk_end,"
                        " content_hash) VALUES (?, ?, ?, ?, ?, ?)",
                        (chunk_id, project_root_real, rel_path, chunk_start, chunk_end,
                         content_hash),
                    )
                    embedded_count += 1
                except Exception as exc:
                    logger.warning("writing chunk %s failed: %s", chunk_id, exc)

            try:
                conn.commit()
            except Exception as exc:
                logger.warning("commit failed after batch %d: %s", i // batch_size, exc)

            # Report POSITION, not embedded_count: a skipped/failed batch still advanced the work,
            # and a bar that stalls on a `continue` is a lie. Always lands on total at the last batch.
            self._progress.embed(min(i + batch_size, total), total)

        return embedded_count

    def _index(self, project_root: str) -> int:
        if not project_root:
            return 0

        root = Path(project_root)
        project_root_real = os.path.realpath(project_root)
        if not root.exists():
            # Returning 0 here meant "nothing new to do", which is indistinguishable from a
            # successful no-op pass — so a repository that had been deleted or moved kept every row
            # it ever had, and `doctor`/`status` went on reporting it as indexed and healthy
            # forever. A missing root is not a quiet no-op; it is the one moment we know the
            # project is gone, and the only chance to reconcile it away.
            removed = self.db.forget_project(project_root_real)
            if removed:
                logger.warning("%s no longer exists — dropped %d stale indexed chunks",
                               project_root, removed)
            return 0

        project_key = _project_key(project_root_real)
        self._backfilled = 0  # per-pass, so a reused Indexer can't carry a stale count forward

        self._cleanup_deleted(root, project_root_real)

        new_chunks = self._collect_new_chunks(root, project_key, project_root_real)
        if not new_chunks:
            # A pass that found nothing new still INDEXED this project — the tree was walked and
            # every chunk confirmed current. Recording it is what lets `status` say "checked 2
            # minutes ago" rather than reporting the age of whenever content last changed.
            self.db.mark_indexed(project_root_real)
            return 0

        written = self._embed_and_write(new_chunks, project_root_real)
        self.db.mark_indexed(project_root_real)
        return written
