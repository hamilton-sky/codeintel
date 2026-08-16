from __future__ import annotations

import itertools
import logging
import math
import os
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeintel.semantic_db import SemanticDb

logger = logging.getLogger(__name__)

_SNIPPET_LINES = 5
# Hybrid rerank (0.7.0): re-read up to this many lines per candidate for lexical scoring — a hard
# per-candidate cap so one huge file can't blow up a query. The read is further bounded at the next
# stored chunk start (see _rerank); the returned snippet still uses only the first _SNIPPET_LINES.
_RERANK_READ_LINES = 40
# Hard ceiling on the candidate set regardless of rerank_candidates, so a misconfigured value can't
# turn one query into thousands of file reads on the interactive hot path.
_RERANK_CANDIDATES_CAP = 200
_RRF_K = 60          # Reciprocal Rank Fusion constant (standard ≈ 60; damps rank differences)
_SYMBOL_BOOST = 0.1  # additive fusion bonus for an exact def/class-name match (≫ one RRF term)

# `\w` is Unicode-aware in Python 3, so a non-ASCII identifier is kept whole; the sub-splitter is
# Latin-cased on purpose (camelCase only exists there) and just adds bonus pieces for ASCII names.
_IDENT_RE = re.compile(r"\w+")
_SUBTOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_SYMBOL_RE = re.compile(r"[^\W\d]\w*")  # a query that is a single identifier (letter/_ then word)


def _pos_int(val: object, default: int) -> int:
    """A usable positive int or the default — guards the public Searcher.search against a caller
    passing a non-int/zero/negative k or rerank_candidates (mirrors indexer._pos_int)."""
    try:
        n = int(val)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return default
    return n if n > 0 else default


def _tokenize(text: str) -> set[str]:
    """Lexical tokens for overlap scoring: each whole identifier plus its camel/snake sub-pieces,
    lowercased. Deliberately set-based (presence, not frequency) — cheap and enough for ranking."""
    toks: set[str] = set()
    for ident in _IDENT_RE.findall(text):
        toks.add(ident.lower())
        for sub in _SUBTOKEN_RE.findall(ident):
            toks.add(sub.lower())
    return toks


def _idf_weights(query_tokens: set[str], texts: list[str]) -> dict[str, float]:
    """Inverse document frequency for each query token across *texts*.

    `log(1 + N/(1+df))`: a token in every candidate lands near log(1) and stops counting, one in a
    single candidate keeps most of its weight. Smoothed so a token absent from every candidate
    (common — the query word may live in a part of the chunk the reader never sees) cannot divide
    by zero or blow up the score."""
    n = len(texts)
    if not n or not query_tokens:
        return {}
    token_sets = [_tokenize(t) for t in texts]
    weights: dict[str, float] = {}
    for token in query_tokens:
        df = sum(1 for ts in token_sets if token in ts)
        weights[token] = math.log(1.0 + n / (1.0 + df))
    return weights


class Searcher:
    def __init__(
        self,
        db: SemanticDb,
        model_name: str = "BAAI/bge-small-en-v1.5",
    ) -> None:
        self.db = db
        self.model_name = model_name
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=self.model_name)
        return self._embedder

    def _embed_query(self, query: str) -> bytes | None:
        try:
            embedder = self._get_embedder()
            vecs = list(embedder.embed([query]))
            if not vecs:
                return None
            vec = vecs[0]
            return struct.pack(f"{len(vec)}f", *vec)
        except Exception as exc:
            logger.warning("query embedding failed: %s", exc)
            return None

    def _row_count(self, project_root_real: str) -> int:
        try:
            conn = self.db.conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM chunk_hashes WHERE project_root = ?",
                (project_root_real,),
            ).fetchone()
            return row[0] if row else 0
        except Exception as exc:
            logger.warning("rowcount check failed: %s", exc)
            return 0

    def has_index(self, project_root: str) -> bool:
        """True when this project has at least one indexed chunk — lets the provider
        distinguish 'nothing indexed yet' (no-index) from 'matches below floor'."""
        return self._row_count(os.path.realpath(project_root)) > 0

    def _read_snippet(self, file_path: Path, chunk_start: int) -> str:
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            snippet_lines = lines[chunk_start: chunk_start + _SNIPPET_LINES]
            return "".join(snippet_lines).rstrip()
        except FileNotFoundError:
            return "[file not found]"
        except Exception as exc:
            logger.debug("snippet read failed for %s:%d: %s", file_path, chunk_start, exc)
            return "[file not found]"

    def _read_chunk(self, file_path: Path, chunk_start: int) -> list[str] | None:
        """Bounded re-read from ``chunk_start`` for rerank lexical scoring (and the snippet in one
        read). Uses ``islice`` so a huge multi-line file is not fully materialised just to take a
        40-line window. ``None`` on any failure — the caller scores that candidate 0 and shows a
        not-found snippet, never crashing (a missing/edited file must degrade, per never-raise)."""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return list(itertools.islice(f, chunk_start, chunk_start + _RERANK_READ_LINES))
        except Exception as exc:
            logger.debug("chunk re-read failed for %s:%d: %s", file_path, chunk_start, exc)
            return None

    def _chunk_starts(self, project_root_real: str, file_path: str) -> list[int]:
        """All stored chunk start lines for one file, sorted. Used to bound each candidate's rerank
        read at the *next* chunk — the chunk's true end for tiling syntax chunks, its owned region
        for overlapping line windows — so lexical/boost text never bleeds into an unrelated def.
        Uses the (project_root, file_path) composite index. ``[]`` on any error → no bound applied."""
        try:
            rows = self.db.conn().execute(
                "SELECT chunk_start FROM chunk_hashes"
                " WHERE project_root = ? AND file_path = ? ORDER BY chunk_start",
                (project_root_real, file_path),
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception as exc:
            logger.debug("chunk-start lookup failed for %s: %s", file_path, exc)
            return []

    @staticmethod
    def _lexical_score(
        query_tokens: set[str], chunk_text: str, weights: dict[str, float] | None = None
    ) -> float:
        """Weighted term overlap in [0, 1]: the share of the query's *informative* weight present.

        Unweighted coverage treated every query token alike, so "the auth middleware" gave "the"
        the same pull as "middleware" — and in a query of three tokens that is a third of the
        score spent on a word in every chunk of the corpus. *weights* supplies a per-token IDF, so
        a term common across the candidates counts for little and a rare one dominates. With no
        weights (or all-equal ones) this is exactly the previous fraction, which is why the
        existing rerank tests still describe it correctly."""
        if not query_tokens:
            return 0.0
        present = query_tokens & _tokenize(chunk_text)
        if weights is None:
            return len(present) / len(query_tokens)
        total = sum(weights.get(t, 1.0) for t in query_tokens)
        if total <= 0:
            return 0.0
        return sum(weights.get(t, 1.0) for t in present) / total

    @staticmethod
    def _symbol_boost(query: str, chunk_text: str) -> float:
        """Additive fusion bonus for the 'exact symbol' case: when the query is a single identifier
        that appears in the chunk as a ``def``/``class`` name (full boost) or as a standalone word
        (half). Case-insensitive, to match the lexical score. Cosine alone under-ranks these literal
        matches; this is what pulls them to the top. The ``_SYMBOL_RE`` guard runs before any
        ``re.escape`` interpolation, so no query character can ever reach the built pattern."""
        q = (query or "").strip()
        if not _SYMBOL_RE.fullmatch(q):
            return 0.0  # multi-word / non-identifier query → no structural signal
        if re.search(rf"\b(?:def|class)\s+{re.escape(q)}\b", chunk_text, re.IGNORECASE):
            return _SYMBOL_BOOST
        if re.search(rf"\b{re.escape(q)}\b", chunk_text, re.IGNORECASE):
            return _SYMBOL_BOOST * 0.5
        return 0.0

    def _rerank(
        self, query: str, root: Path, project_root_real: str, candidates: list[dict]
    ) -> list[dict]:
        """Reorder floor-gated candidates (given in cosine order) by Reciprocal Rank Fusion over
        the semantic rank and a lexical rank, plus a symbol boost. Reads each candidate's chunk
        once (bounded) — caching its 5-line snippet — so the whole rerank costs ≤ len(candidates)
        reads. When no candidate has any lexical overlap the lexical rank mirrors the semantic
        rank, so the cosine order is returned unchanged (rerank only *reorders* on real signal)."""
        query_tokens = _tokenize(query)
        n = len(candidates)
        # Bound each candidate's lexical/boost text at the NEXT stored chunk start in its file — the
        # chunk's true end for tiling syntax chunks, its owned region for overlapping line windows.
        # A chunk's end line isn't stored, so a fixed 40-line read would otherwise (a) bleed into an
        # unrelated later def, handing this chunk a symbol boost that isn't its own, and/or (b) — if
        # we capped at neighbouring *candidates* — truncate a chunk below its own overlapping span.
        # Bounding at the next real chunk gives each source line to exactly one chunk: the one whose
        # 5-line snippet will actually show it.
        starts_by_file: dict[str, list[int]] = {}
        for path in {c["path"] for c in candidates}:
            starts_by_file[path] = self._chunk_starts(project_root_real, path)

        # Two passes over the candidates: gather their texts, derive IDF from them, then score.
        # The document frequency is measured over the CANDIDATES rather than the whole corpus —
        # no index change, no extra reads, and it is the right population anyway, since the job
        # here is to separate these results from each other rather than from the repo at large.
        texts: list[str] = []
        lex = [0.0] * n
        boost = [0.0] * n
        for i, c in enumerate(candidates):
            lines = self._read_chunk(root / c["path"], c["line"])
            if lines is None:
                c["snippet"] = "[file not found]"
                text = ""
            else:
                # snippet keeps the existing 5-line preview (bleed-tolerant, unchanged); the
                # lexical/boost text is capped at the next stored chunk start in this file.
                c["snippet"] = "".join(lines[:_SNIPPET_LINES]).rstrip()
                nxt = next((s for s in starts_by_file.get(c["path"], ()) if s > c["line"]), None)
                text = "".join(lines if nxt is None else lines[: max(1, nxt - c["line"])])
            texts.append(text)
            boost[i] = self._symbol_boost(query, text)

        weights = _idf_weights(query_tokens, texts)
        for i, text in enumerate(texts):
            lex[i] = self._lexical_score(query_tokens, text, weights)

        # lexical rank: highest lexical score first, ties broken by the semantic rank (index i)
        order_by_lex = sorted(range(n), key=lambda i: (-lex[i], i))
        rank_lex = [0] * n
        for pos, i in enumerate(order_by_lex):
            rank_lex[i] = pos

        # sem rank is the candidate's position i (they arrive in cosine order)
        fused = [1.0 / (_RRF_K + i) + 1.0 / (_RRF_K + rank_lex[i]) + boost[i] for i in range(n)]
        order = sorted(range(n), key=lambda i: (-fused[i], i))  # fused desc, tie → better cosine
        return [candidates[i] for i in order]

    def search(
        self,
        query: str,
        project_root: str,
        k: int = 10,
        cosine_floor: float = 0.25,
        rerank: str = "on",
        rerank_candidates: int = 30,
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        # Guard the public API: a direct caller passing a non-int/zero/negative k or
        # rerank_candidates must degrade, not raise (never-raise). Config callers already pre-cast.
        k = _pos_int(k, 10)
        rerank_candidates = _pos_int(rerank_candidates, 30)
        # Accept the documented "off" plus the obvious falsy spellings (incl. the Python bool
        # ``False`` → ``"false"``), so a direct caller isn't silently left with rerank on.
        do_rerank = str(rerank).strip().lower() not in ("off", "false", "0", "no", "none")
        # With rerank on, retrieve a wider candidate set by cosine, then re-order it; still return
        # the top-k. Cap only the *extra* rerank breadth (rerank_candidates) — the DoS guard — while
        # always honoring k, so enabling rerank never returns fewer results than the pure-cosine
        # path would for the same k (even a large k > cap).
        candidate_limit = max(k, min(rerank_candidates, _RERANK_CANDIDATES_CAP)) if do_rerank else k

        project_root_real = os.path.realpath(project_root)

        # Scope the KNN to THIS project — a search in repo B must never surface repo A's
        # chunks (wrong-file, wrong-content hits) from the shared cache.
        if self._row_count(project_root_real) == 0:
            return []

        query_vec = self._embed_query(query)
        if query_vec is None:
            return []

        try:
            conn = self.db.conn()
            rows = conn.execute(
                """
                SELECT
                    ce.chunk_id,
                    ch.chunk_start,
                    ch.file_path,
                    vec_distance_cosine(ce.embedding, ?) AS dist
                FROM code_embeddings ce
                JOIN chunk_hashes ch ON ce.chunk_id = ch.chunk_id
                WHERE ch.project_root = ?
                ORDER BY dist
                LIMIT ?
                """,
                (query_vec, project_root_real, candidate_limit),
            ).fetchall()
        except Exception as exc:
            logger.warning("KNN query failed: %s", exc)
            return []

        root = Path(project_root)

        # Floor-gated candidate set, in cosine order. The cosine_floor stays on the *semantic*
        # candidates (not the fused score), so rerank can only re-order what pure cosine already
        # judged good enough — quality can't regress below the pre-0.7 path.
        candidates: list[dict] = []
        for row in rows:
            try:
                score = 1.0 - float(row["dist"])
                if score < cosine_floor:
                    continue
                candidates.append({
                    "path": str(row["file_path"]),
                    "line": int(row["chunk_start"]),
                    "score": round(score, 6),
                })
            except Exception as exc:
                logger.debug("candidate row processing failed: %s", exc)
                continue

        if not candidates:
            return []

        # Rerank re-reads chunk text; if anything goes wrong, fall back to the cosine order so a
        # rerank fault can never do worse than today (and never raises).
        if do_rerank and len(candidates) > 1:
            try:
                candidates = self._rerank(query, root, project_root_real, candidates)
            except Exception as exc:
                logger.warning("rerank failed, using cosine order: %s", exc)

        results: list[dict] = []
        for c in candidates[:k]:
            # _rerank caches the snippet on each candidate it read; fill it in otherwise.
            snippet = c.get("snippet")
            if snippet is None:
                snippet = self._read_snippet(root / c["path"], c["line"])
            results.append({
                "path": c["path"],
                "line": c["line"],
                "snippet": snippet,
                "score": c["score"],
            })
        return results
