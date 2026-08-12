from __future__ import annotations

import logging
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeintel.semantic_db import SemanticDb

logger = logging.getLogger(__name__)

_SNIPPET_LINES = 5


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

    def search(
        self,
        query: str,
        project_root: str,
        k: int = 10,
        cosine_floor: float = 0.25,
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        k = max(1, k)
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
                (query_vec, project_root_real, k),
            ).fetchall()
        except Exception as exc:
            logger.warning("KNN query failed: %s", exc)
            return []

        root = Path(project_root)
        results: list[dict] = []

        for row in rows:
            try:
                dist = float(row["dist"])
                score = 1.0 - dist
                if score < cosine_floor:
                    continue

                chunk_start = int(row["chunk_start"])
                rel_path = str(row["file_path"])
                abs_path = root / rel_path

                snippet = self._read_snippet(abs_path, chunk_start)

                results.append({
                    "path": rel_path,
                    "line": chunk_start,
                    "snippet": snippet,
                    "score": round(score, 6),
                })
            except Exception as exc:
                logger.debug("result row processing failed: %s", exc)
                continue

        return results
