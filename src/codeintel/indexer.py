from __future__ import annotations

import ast
import hashlib
import logging
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeintel.semantic_db import SemanticDb

logger = logging.getLogger(__name__)

_INDEXED_EXTS = frozenset({
    ".py", ".md",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",       # TS/JS variants
    ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh",    # C/C++ variants
})
_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules"})
# Vendored / regenerable dirs skipped even without a .gitignore entry.
_DEFAULT_IGNORES = frozenset({
    ".venv", "venv", "env", "dist", "build", "target",
    ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", ".cache",
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
        n = int(val)  # type: ignore[arg-type]
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
    ) -> None:
        self.db = db
        self.model_name = model_name
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

    def _get_embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=self.model_name)
        return self._embedder

    def index(self, project_root: str) -> int:
        """Return count of newly embedded chunks, or -1 on unrecoverable failure."""
        try:
            return self._index(project_root)
        except Exception as exc:
            logger.error("Indexer.index() unrecoverable failure: %s", exc)
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
                    if not line or line.startswith("#") or line.startswith("!"):
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
            deleted_paths = [
                row[0] for row in rows if not (root / row[0]).exists()
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
                    conn.execute(
                        "DELETE FROM code_embeddings WHERE chunk_id = ?", (cid,)
                    )
                    conn.execute(
                        "DELETE FROM chunk_hashes WHERE chunk_id = ?", (cid,)
                    )
            conn.commit()
        except Exception as exc:
            logger.warning("Cleanup pass failed: %s", exc)

    def _walk_files(self, root: Path):
        ignores = set(_SKIP_DIRS) | set(_DEFAULT_IGNORES) | self._load_gitignore(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ignores and not d.endswith(".egg-info")
            ]
            for fname in filenames:
                if fname in ignores:
                    continue
                if Path(fname).suffix.lower() in _INDEXED_EXTS:
                    yield Path(dirpath) / fname

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
        for s in range(start, end, self.stride):
            spans.append((s, min(s + self.window, end)))
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
        if not all_types:
            return []  # unknown language -> caller windows the whole file
        spans: list[tuple[int, int]] = []

        def nearest_defs(node):
            """Def-type nodes reachable without crossing another def-type — top-level defs from the
            root, a container's direct members from the container. Iterative, so deep nesting can't
            blow the stack."""
            found = []
            stack = [c for c in reversed(node.children) if c.is_named]
            while stack:
                c = stack.pop()
                if c.type in all_types:
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
        new_chunks: list[tuple[str, str, str, int, str]],
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
            if not chunk_text.strip():
                # EC3.4: never embed empty/whitespace-only chunks (zero vectors pollute results).
                chunk_count += 1
                continue
            chunk_id = f"{project_key}:{rel_path}:{start}"
            content_hash = hashlib.sha256(chunk_text.encode()).hexdigest()[:16]
            keep_ids.add(chunk_id)  # produced this pass — keep even when dedup skips re-embed
            try:
                row = conn.execute(
                    "SELECT content_hash FROM chunk_hashes WHERE chunk_id = ?",
                    (chunk_id,),
                ).fetchone()
                if row and row[0] == content_hash:
                    chunk_count += 1
                    continue
            except Exception as exc:
                logger.debug("hash check failed for %s: %s", chunk_id, exc)
            new_chunks.append((chunk_id, chunk_text, rel_path, start, content_hash))
            chunk_count += 1
        return keep_ids, True

    def _collect_new_chunks(
        self, root: Path, project_key: str, project_root_real: str
    ) -> list[tuple[str, str, str, int, str]]:
        """Walk files; return (chunk_id, text, rel_path, start, hash) for new/changed chunks, and
        reconcile each fully-processed file (dropping rows for chunks it no longer produces)."""
        conn = self.db.conn()
        new_chunks: list[tuple[str, str, str, int, str]] = []

        for filepath in self._walk_files(root):
            if len(new_chunks) >= self.max_total_chunks:
                logger.warning(
                    "index: reached max_total_chunks=%d this pass — stopping "
                    "(raise it in .codeintel.toml to embed more of a very large repo)",
                    self.max_total_chunks,
                )
                break
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

        return new_chunks

    def _embed_and_write(
        self, new_chunks: list[tuple[str, str, str, int, str]], project_root_real: str
    ) -> int:
        embedder = self._get_embedder()  # may raise → propagates to index() → returns -1
        conn = self.db.conn()
        embedded_count = 0
        batch_size = 32

        for i in range(0, len(new_chunks), batch_size):
            batch = new_chunks[i: i + batch_size]
            texts = [c[1] for c in batch]

            try:
                embeddings = list(embedder.embed(texts))
            except Exception as exc:
                logger.warning("embedding batch %d failed: %s", i // batch_size, exc)
                continue

            for j, (chunk_id, _, rel_path, chunk_start, content_hash) in enumerate(batch):
                if j >= len(embeddings):
                    break
                try:
                    vec = embeddings[j]
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
                        "(chunk_id, project_root, file_path, chunk_start, content_hash)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (chunk_id, project_root_real, rel_path, chunk_start, content_hash),
                    )
                    embedded_count += 1
                except Exception as exc:
                    logger.warning("writing chunk %s failed: %s", chunk_id, exc)

            try:
                conn.commit()
            except Exception as exc:
                logger.warning("commit failed after batch %d: %s", i // batch_size, exc)

        return embedded_count

    def _index(self, project_root: str) -> int:
        if not project_root:
            return 0

        root = Path(project_root)
        if not root.exists():
            return 0

        project_root_real = os.path.realpath(project_root)
        project_key = _project_key(project_root_real)

        self._cleanup_deleted(root, project_root_real)

        new_chunks = self._collect_new_chunks(root, project_key, project_root_real)
        if not new_chunks:
            return 0

        return self._embed_and_write(new_chunks, project_root_real)
