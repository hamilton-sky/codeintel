from __future__ import annotations

import itertools
import logging
import os
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from codeintel.containment import ContainmentError, open_contained
from codeintel.semantic_db import chunk_content_hash

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


# NOTE — IDF over the retrieved candidates was tried and REVERTED (0.15.1).
#
# The intent was to stop "the" in "the auth middleware" pulling as hard as "middleware". Measured
# against real queries it did the opposite. Document frequency over a candidate set drawn from ONE
# code repository measures "rare among these results", and in a set that is mostly code the domain
# term is the common one: for "the part that renders dead code candidates", `code` scored the same
# 0.728 as `the`, while `part` and `that` — incidental prose words — scored 2.14 and 0.947. The
# default ranking then put a CHANGELOG paragraph above the function it describes, and
# `rerank="off"` beat `rerank="on"`. 13 of 20 sampled queries changed their top three.
#
# The idea is sound; the population is wrong. Doing this properly needs document frequency over
# the CORPUS, which means counting at index time and a schema to hold it — see docs/semantic.md.
# Shipped on the strength of a three-document synthetic example, which is exactly the sort of
# evidence that should not have been enough.


class Searcher:
    def __init__(
        self,
        db: SemanticDb,
        model_name: str = "BAAI/bge-small-en-v1.5",
    ) -> None:
        self.db = db
        self.model_name = model_name
        self._embedder = None
        # Outcome of the last search's staleness verification, for callers that must REPORT a
        # thinned result rather than quietly serve one. `last_stale` counts hits dropped because
        # the file no longer holds the code they were indexed from; `last_unverifiable` counts
        # legacy rows with no stored span, which cannot be checked either way.
        self.last_stale = 0
        self.last_unverifiable = 0

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

    def _read_snippet(self, root_real: str, file_path: Path, chunk_start: int) -> str:
        try:
            with open_contained(root_real, file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            snippet_lines = lines[chunk_start: chunk_start + _SNIPPET_LINES]
            return "".join(snippet_lines).rstrip()
        except ContainmentError:
            # Distinct from "not found": the row is stale AND the path now escapes the root, so
            # something replaced an indexed file with a link out. Say so rather than reporting the
            # generic missing-file text, which would read as ordinary index drift.
            return "[refused: resolves outside the indexed root]"
        except FileNotFoundError:
            return "[file not found]"
        except Exception as exc:
            logger.debug("snippet read failed for %s:%d: %s", file_path, chunk_start, exc)
            return "[file not found]"

    def _read_chunk(
        self, root_real: str, file_path: Path, chunk_start: int, chunk_end: int | None = None
    ) -> list[str] | None:
        """Re-read a chunk's lines for staleness verification, rerank scoring and the snippet — in
        one read. Uses ``islice`` so a huge multi-line file is not fully materialised.

        With ``chunk_end`` (the stored span) the read is EXACT: the same lines the indexer hashed,
        which is what makes verification possible. ``None`` falls back to the ``_RERANK_READ_LINES``
        window used before the span was recorded — a legacy row, unverifiable by definition.

        ``None`` on any failure — the caller scores that candidate 0 and shows a not-found snippet,
        never crashing (a missing/edited file must degrade, per never-raise)."""
        stop = chunk_start + _RERANK_READ_LINES if chunk_end is None else chunk_end
        try:
            with open_contained(root_real, file_path, encoding="utf-8", errors="replace") as f:
                return list(itertools.islice(f, chunk_start, max(chunk_start, stop)))
        except Exception as exc:
            # `open_contained` logs the escape at WARNING before raising, so a containment refusal
            # is visible even though this handler degrades it to "score this candidate 0" like any
            # other unreadable file — which is the correct never-raise behaviour here.
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
    def _lexical_score(query_tokens: set[str], chunk_text: str) -> float:
        """Token overlap in [0, 1]: the fraction of query (sub)tokens present in the chunk."""
        if not query_tokens:
            return 0.0
        return len(query_tokens & _tokenize(chunk_text)) / len(query_tokens)

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

    def _verify(
        self, root: Path, project_root_real: str, candidates: list[dict]
    ) -> tuple[list[dict], int, int]:
        """Read each candidate's real span and confirm it still hashes to what was indexed.

        This closes the engine's worst failure mode. A row stores a *line number*, and the snippet
        was always re-read from the CURRENT file at that line — so once a file was edited, a hit
        pointed at whatever now occupies those lines. Deleting a `charge_credit_card()` at line 1
        made "charge the credit card" return `app.py:1 | import logging`, ranked first and marked
        `confidence: complete`. That is worse than returning nothing: the agent has no signal to
        doubt it, and the codebase's whole partial/complete contract exists to prevent exactly this.

        Stale candidates are DROPPED rather than annotated — a footnote still puts a wrong
        `path:line` in front of the agent — and counted, so the provider can report the omission
        instead of passing off a thinned list as a complete one. A row whose file is unreadable is
        kept with the not-found sentinel (unchanged behaviour); a row with no stored span is
        unverifiable and kept as-is.

        Returns ``(kept, stale, unverifiable)``. Each kept candidate carries its ``text``, so
        rerank and the snippet reuse this read instead of paying for their own.
        """
        kept: list[dict] = []
        stale = unverifiable = 0
        # Only needed for legacy rows: without a stored end, bound the read at the next chunk start
        # so lexical text can't bleed into an unrelated def. Rows written since carry a real span.
        starts_by_file: dict[str, list[int]] = {}
        for c in candidates:
            if c["end"] is None and c["path"] not in starts_by_file:
                starts_by_file[c["path"]] = self._chunk_starts(project_root_real, c["path"])

        for c in candidates:
            end = c["end"]
            lines = self._read_chunk(project_root_real, root / c["path"], c["line"], end)
            if lines is None:
                c["text"] = None  # unreadable → scored 0, rendered as the not-found sentinel
                kept.append(c)
                continue
            text = "".join(lines)
            if end is None:
                nxt = next((s for s in starts_by_file.get(c["path"], ()) if s > c["line"]), None)
                if nxt is not None:
                    text = "".join(lines[: max(1, nxt - c["line"])])
                unverifiable += 1
            elif chunk_content_hash(text) != c["hash"]:
                stale += 1
                continue
            c["text"] = text
            kept.append(c)
        return kept, stale, unverifiable

    def _rerank(
        self, query: str, root: Path, project_root_real: str, candidates: list[dict]
    ) -> list[dict]:
        """Reorder verified candidates (given in cosine order) by Reciprocal Rank Fusion over the
        semantic rank and a lexical rank, plus a symbol boost. Scores from the text ``_verify``
        already read, so the whole rerank costs ZERO additional reads. When no candidate has any
        lexical overlap the lexical rank mirrors the semantic rank, so the cosine order is returned
        unchanged (rerank only *reorders* on real signal)."""
        query_tokens = _tokenize(query)
        n = len(candidates)

        lex = [0.0] * n
        boost = [0.0] * n
        for i, c in enumerate(candidates):
            text = c.get("text")
            if text is None:
                # Unreadable: re-read through _read_snippet purely to get the RIGHT sentinel. It
                # separates a containment refusal ("resolves outside the indexed root" — someone
                # replaced an indexed file with a link out) from ordinary index drift, and
                # flattening both to "[file not found]" would hide the security-relevant one.
                # Costs a read only for a candidate that is already broken.
                c["snippet"] = self._read_snippet(project_root_real, root / c["path"], c["line"])
                text = ""
            else:
                c["snippet"] = "".join(text.splitlines(keepends=True)[:_SNIPPET_LINES]).rstrip()
            lex[i] = self._lexical_score(query_tokens, text)
            boost[i] = self._symbol_boost(query, text)

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
                    ch.chunk_end,
                    ch.content_hash,
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
                end = row["chunk_end"]
                candidates.append({
                    "path": str(row["file_path"]),
                    "line": int(row["chunk_start"]),
                    "end": None if end is None else int(end),
                    "hash": row["content_hash"],
                    "score": round(score, 6),
                })
            except Exception as exc:
                logger.debug("candidate row processing failed: %s", exc)
                continue

        if not candidates:
            return []

        # Verify BEFORE rerank, so a stale row can neither be ranked nor shown. Never-raise: if
        # verification itself faults, keep every candidate rather than silently returning nothing —
        # degrading to the old (unverified) behaviour is bad, returning an empty result is worse.
        try:
            candidates, stale, unverifiable = self._verify(root, project_root_real, candidates)
        except Exception as exc:
            logger.warning("staleness verification failed, returning unverified: %s", exc)
            stale, unverifiable = 0, len(candidates)
        self.last_stale = stale
        self.last_unverifiable = unverifiable
        if not candidates:
            return []

        # Rerank scores the text verification already read; if anything goes wrong, fall back to
        # the cosine order so a rerank fault can never do worse than today (and never raises).
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
                text = c.get("text")
                snippet = (
                    # See _rerank: an unreadable candidate goes back through _read_snippet so a
                    # containment refusal stays distinguishable from a missing file.
                    self._read_snippet(project_root_real, root / c["path"], c["line"])
                    if text is None
                    else "".join(text.splitlines(keepends=True)[:_SNIPPET_LINES]).rstrip()
                )
            results.append({
                "path": c["path"],
                "line": c["line"],
                "snippet": snippet,
                "score": c["score"],
            })
        return results
